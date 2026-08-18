/**
 * workflow/work.ts — the Work Ledger backend: loopback v1 service commands.
 *
 * The service is authoritative for every gate; this adapter only assembles
 * envelopes, mirrors read projections into the host's shapes, and runs the
 * git steps (freeze/push) the service cannot. The provisional planned-candidate
 * sha mixes in the fresh candidate_id so a re-approved plan after a negative
 * audit never collides on UNIQUE(work_id, revision_id, candidate_sha256).
 */
import { randomUUID } from "node:crypto";
import {
	candidateSha256,
	type Command,
	type CommandEnvelope,
	type CommandResult,
	type EvidenceKind as ServiceEvidenceKind,
	type EvidenceReceipt,
	type Fetch,
	payloadHash,
	type ProjectHealth,
	type UUID,
	WorkClient,
	WorkError,
	type WorkItemView,
	type WorkflowView,
	type WorkspaceTree,
} from "@oh-my-pi/pi-work-client";

import type {
	BackendHooks,
	BatchOutcome,
	CreateBatchInput,
	EvidenceKind,
	EvidenceMeta,
	GoalTree,
	IssueDetail,
	MapSurface,
	NowRef,
	PlanStamp,
	SummaryGateBlocked,
	SummaryGateOk,
	TreeItem,
	WorkStateCarrier,
	WorkflowBackend,
	WorkflowCheckpoint,
} from "./backend";
import { pendingOpsDir, type WorkClientConfig } from "./config";
import { freezeCandidateCommit, pushCandidate } from "./git";
import { ackOps as ackClaimOps, claimPendingOp, dropPendingOp, intentFingerprint, resolvePendingOp } from "./pending-ops";
import { bounded, healthWord, oneRecovery, redactSecrets } from "./status";

const DRAIN_MAX_QUEUE = 8;
const DRAIN_MAX_AGE_DAYS = 14;
const ISSUER = "session-system/work-now";
const NO_SURFACE = "(no surface)";

/** Contract error codes raised inside the store transaction — rolled back,
 *  provably never applied, so their claims may be released for a fresh retry.
 *  5xx, unknown codes, and idempotency_conflict are NOT here: their mutation
 *  state is unknown or already recorded. */
const NON_APPLYING_CODES = new Set([
	"invalid_request",
	"relation_cycle",
	"forbidden",
	"unauthenticated",
	"revision_conflict",
	"focus_conflict",
	"stale_evidence",
	"completion_blocked",
]);

/** Model-facing kinds map one-for-one onto the service's receipt kinds
 *  (`plan`/`push` are minted internally by stampPlan/closeWithVerdict). */
const SERVICE_KIND: Record<EvidenceKind, ServiceEvidenceKind> = {
	handoff: "handoff",
	verification: "verification",
	audit: "audit",
	closeout: "closeout",
};

/** RFC-4122-shaped deterministic id over the canonical payload hash: the same
 *  logical content mints the same id regardless of key insertion order, so a
 *  crash/restart retry reconstructs the SAME intent fingerprint and finds its
 *  pending-operation claim. */
function stableId(...parts: unknown[]): UUID {
	const hex = payloadHash(parts);
	const variant = ((parseInt(hex[16]!, 16) & 0x3) | 0x8).toString(16);
	return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

/** Intent fingerprints exclude volatile fields (timestamps) so a retry
 *  reconstructs the same intent; the persisted envelope keeps the original
 *  bytes, so a resend is a byte-identical replay. */
function scrubVolatile(type: Command["type"], payload: unknown): unknown {
	if (type === "append_evidence") {
		const { receipt } = payload as { receipt: Record<string, unknown> };
		const { issued_at: _issuedAt, ...rest } = receipt;
		return { receipt: rest };
	}
	if (type === "revise_work") {
		const p = payload as Record<string, unknown> & { revision: Record<string, unknown> };
		const { created_at: _createdAt, ...revision } = p.revision;
		return { ...p, revision };
	}
	return payload;
}

export function createWorkBackend(
	config: WorkClientConfig,
	token: () => string | null,
	fetchImpl?: Fetch,
	pendingDir = pendingOpsDir(),
): WorkflowBackend {
	const client = new WorkClient(config.baseUrl, config.workspaceId, token, fetchImpl);
	const correlationId = randomUUID();
	/** Operation ids handed to the host since the last ack — drained by ackOps. */
	const deliveredOps: UUID[] = [];

	/** Every mutation goes through a durable per-intent claim (plan §3):
	 *  persist the exact envelope before transport; on a lost response or
	 *  restart the SAME intent resends those bytes or returns the stored
	 *  result — never a second operation. */
	async function run<T extends Command["type"]>(type: T, payload: Extract<Command, { type: T }>["payload"]) {
		// Scoped by workspace AND principal: two configured actors in the same
		// workspace issuing identical payloads must never share a claim.
		const intent = intentFingerprint("intent", config.workspaceId, config.ownerId, type, scrubVolatile(type, payload));
		const claim = await claimPendingOp(pendingDir, intent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: config.workspaceId,
			operation_id: randomUUID(),
			request_id: randomUUID(),
			correlation_id: correlationId,
			command: { type, payload } as Command,
		}));
		if (!claim.record || typeof (claim.record.envelope as CommandEnvelope | undefined)?.operation_id !== "string") {
			throw new Error(`pending-operation claim ${claim.path} is unreadable — repair or remove it manually; refusing to risk a duplicate ${type}`);
		}
		const record = claim.record;
		const envelope = record.envelope as CommandEnvelope;
		if (record.result !== undefined) {
			// Resolved before a crash/restart: hand back the same result, never re-POST.
			deliveredOps.push(envelope.operation_id);
			return record.result as Extract<CommandResult, { type: T }>;
		}
		try {
			const response = await client.execute(envelope);
			await resolvePendingOp(claim.path, record, response.result);
			deliveredOps.push(envelope.operation_id);
			return response.result as Extract<CommandResult, { type: T }>;
		} catch (error) {
			if (error instanceof WorkError && error.status > 0 && NON_APPLYING_CODES.has(error.code)) {
				// Raised inside the store transaction and rolled back — the
				// command provably did not apply, so a corrected retry may
				// claim fresh. Everything else keeps the claim: 5xx/unknown
				// codes may still have committed, and idempotency_conflict
				// means this operation id exists under DIFFERENT bytes — that
				// stored result can never satisfy this call.
				await dropPendingOp(claim.path);
			}
			throw error;
		}
	}

	function toRef(item: WorkItemView, projects: ReadonlyMap<string, string>): NowRef {
		return {
			id: item.work_id,
			key: item.alias.key,
			title: item.revision.title,
			...(item.project_id && projects.has(item.project_id) ? { project: projects.get(item.project_id) } : {}),
		};
	}

	function projectNames(tree: WorkspaceTree): Map<string, string> {
		return new Map(tree.projects.map(p => [p.project_id, p.name]));
	}

	async function projectIdFor(name: string): Promise<UUID> {
		const tree = await client.tree();
		const hit = tree.projects.find(p => p.name === name);
		if (!hit) throw new Error(`Work Ledger has no project named "${name}" — create it first`);
		return hit.project_id;
	}

	async function receipt(kind: ServiceEvidenceKind, issue: NowRef, body: string, meta?: EvidenceMeta): Promise<EvidenceReceipt> {
		const item = await client.workItem(issue.key);
		const candidate = item.candidate;
		if (!candidate) throw new Error(`${item.alias.key} has no current candidate — run /plan first`);
		if ((kind === "verification" || kind === "audit") && !candidate.commit_sha) {
			throw new Error(`${item.alias.key}'s candidate is not finalized — run /summary first`);
		}
		const payload: Record<string, unknown> = { body };
		const payloadSha = payloadHash(payload);
		const base = {
			// Deterministic per logical write: a crash/restart retry rebuilds the
			// same receipt, so the intent fingerprint matches its claim.
			receipt_id: stableId("receipt", item.work_id, item.revision.revision_id, candidate.candidate_id, kind, payloadSha),
			work_id: item.work_id,
			revision_id: item.revision.revision_id,
			candidate_id: candidate.candidate_id,
			kind,
			payload,
			payload_sha256: payloadSha,
			issuer: ISSUER,
			issued_at: new Date().toISOString(),
			independent: false,
		};
		if (kind === "audit") {
			if (!meta?.verdict) throw new Error("audit evidence requires a bridge-authenticated verdict");
			return {
				...base,
				verdict: meta.verdict,
				independent: meta.independent === true,
				candidate_sha256: candidate.candidate_sha256,
				...(candidate.commit_sha ? { candidate_commit: candidate.commit_sha } : {}),
			};
		}
		if (kind === "verification") {
			return { ...base, candidate_sha256: candidate.candidate_sha256, ...(candidate.commit_sha ? { candidate_commit: candidate.commit_sha } : {}) };
		}
		return base;
	}

	function linesOf(view: WorkflowView): string {
		return view.receipts
			.map(r => `${r.kind} ${r.issued_at.slice(0, 19)} ${r.verdict ? `${r.verdict} ` : ""}${r.payload_sha256.slice(0, 12)}`)
			.join("\n");
	}

	/** The approved-plan identity lives in the payload (stamp.hash), not the
	 *  receipt hash; imported/pre-147 receipts fall back to the payload hash. */
	function planHashOf(receiptRow: EvidenceReceipt): string {
		const stamped = receiptRow.payload.plan_sha256;
		return typeof stamped === "string" && /^[0-9a-f]{64}$/.test(stamped) ? stamped : receiptRow.payload_sha256;
	}

	/** Preflight mirror of semantics.completion_blockers, minus push_unverified —
	 *  closeWithVerdict creates the push receipt before complete_work. The service
	 *  remains the authority; this only produces the owner-facing line early. */
	function completionPreflight(view: WorkflowView): string | null {
		const candidate = view.item.candidate;
		if (!candidate || candidate.kind !== "final" || !candidate.commit_sha) {
			return "no finalized candidate — run /summary to freeze this session's work first";
		}
		const fresh = view.receipts.filter(
			r => r.revision_id === view.item.revision.revision_id && r.candidate_id === candidate.candidate_id,
		);
		const kinds = new Set(fresh.map(r => r.kind));
		if (!kinds.has("closeout")) return "the close ritual receipt is missing — /summary must complete its close review";
		if (!kinds.has("plan")) return "no plan evidence on this candidate — run /plan first";
		if (!kinds.has("verification")) return "no verification evidence on this candidate";
		const audits = fresh
			.filter(r => r.kind === "audit")
			.sort((a, b) => a.issued_at.localeCompare(b.issued_at) || a.receipt_id.localeCompare(b.receipt_id));
		const latest = audits.at(-1);
		if (!latest || !latest.independent || latest.verdict !== "PASS") {
			return "the latest audit is not an independent PASS — run /summary again after fixing its findings";
		}
		if (!view.closeout.some(c => c.state === "pending" && c.candidate_id === candidate.candidate_id)) {
			return "no approved closeout request — complete /summary before /done";
		}
		return null;
	}

	const backend: WorkflowBackend = {
		name: "work",
		serviceLabel: "Work Ledger",
		markerFile: ".work-project",
		scopeFix: 'echo "<Exact Project Name>" > .work-project at the repo root',
		cacheFile: "work-now.json",
		queueNoun: "TRIAGE",
		reviewKind: "closeout",
		evidenceKinds: ["handoff", "verification", "audit", "closeout"],
		bookendTitle: "── Work Ledger bookend (work.omp.dev/v1) ──",

		readCarrier(raw: unknown): WorkStateCarrier {
			const c = (raw ?? {}) as Record<string, unknown>;
			return {
				...(typeof c.revisionId === "string" ? { revisionId: c.revisionId } : {}),
				...(typeof c.plannedCandidateId === "string" ? { plannedCandidateId: c.plannedCandidateId } : {}),
				...(typeof c.candidateId === "string" ? { candidateId: c.candidateId } : {}),
				...(typeof c.candidateSha === "string" ? { candidateSha: c.candidateSha } : {}),
				...(typeof c.commitSha === "string" ? { commitSha: c.commitSha } : {}),
				...(c.closeoutRequested === true ? { closeoutRequested: true } : {}),
			};
		},

		async healthProbe(): Promise<void> {
			const ready = await client.healthReady();
			if (!ready.ready) throw new Error(`not ready${ready.alerts.length ? `: ${ready.alerts.join("; ")}` : ""}`);
		},

		async projectScopeExists(project: string): Promise<boolean> {
			return (await client.tree()).projects.some(p => p.name === project);
		},

		async mapData(nowKey?: string, projectFilter?: string): Promise<{ surfaces: MapSurface[]; capped: boolean }> {
			const tree = await client.tree();
			const names = projectNames(tree);
			const byProject = new Map<string, WorkItemView[]>();
			for (const item of tree.items) {
				if (item.archived || item.state === "DONE" || item.state === "CANCELED") continue;
				const key = (item.project_id && names.get(item.project_id)) || NO_SURFACE;
				const list = byProject.get(key) ?? [];
				list.push(item);
				byProject.set(key, list);
			}
			const wanted = projectFilter ?? null;
			const surfaces: MapSurface[] = [...byProject.keys()]
				.filter(name => !wanted || name === wanted)
				.map(name => {
					const project = tree.projects.find(p => p.name === name);
					const issues = (byProject.get(name) ?? []).map(item => ({
						id: item.work_id,
						key: item.alias.key,
						title: item.revision.title,
						state: item.state,
						updatedAt: item.revision.created_at,
						waiting: item.state === "TRIAGE",
						isNow: item.alias.key === nowKey,
						description: item.revision.description,
						labels: [] as string[],
						...(name === NO_SURFACE ? {} : { project: name }),
					}));
					return {
						name,
						...(project?.health ? { health: project.health } : {}),
						state: "active",
						issues,
						waiting: issues.filter(i => i.waiting).length,
					};
				})
				.sort((a, b) => a.name.localeCompare(b.name));
			return { surfaces, capped: false };
		},

		async issueDetail(key: string): Promise<IssueDetail> {
			const view = await client.workflow(key);
			const keyOf = new Map<UUID, string>();
			const tree = await client.tree();
			for (const item of tree.items) keyOf.set(item.work_id, item.alias.key);
			const rel = (kind: string, mine: "source" | "target"): string[] =>
				view.relations
					.filter(e => e.active && e.kind === kind && (mine === "source" ? e.source_work_id : e.target_work_id) === view.item.work_id)
					.map(e => keyOf.get(mine === "source" ? e.target_work_id : e.source_work_id) ?? "?");
			return {
				title: view.item.revision.title,
				state: view.item.state,
				...(view.project ? { project: view.project.name } : {}),
				labels: [],
				...(view.item.revision.description ? { description: view.item.revision.description } : {}),
				blockedBy: rel("blocks", "target"),
				blocks: rel("blocks", "source"),
				related: rel("related", "source"),
				comments: [],
				commentsTotal: view.receipts.length,
				commentsLast7d: view.receipts.filter(r => Date.now() - Date.parse(r.issued_at) < 7 * 86_400_000).length,
				digestPacket: linesOf(view) || "no receipts yet",
			};
		},

		async findIssue(key: string): Promise<NowRef> {
			const item = await client.workItem(key);
			return toRef(item, projectNames(await client.tree()));
		},

		async currentNow(): Promise<NowRef | null> {
			const slot = await client.focus(config.ownerId);
			if (!slot.work_id) return null;
			const tree = await client.tree();
			const item = tree.items.find(i => i.work_id === slot.work_id);
			return item ? toRef(item, projectNames(tree)) : null;
		},

		async goalTree(now: NowRef): Promise<GoalTree | null> {
			const tree = await client.tree();
			const names = projectNames(tree);
			const me = tree.items.find(i => i.work_id === now.id);
			const project = me?.project_id ? tree.projects.find(p => p.project_id === me.project_id) : undefined;
			const inGoal = tree.items.filter(i => !i.archived && i.project_id && i.project_id === me?.project_id);
			if (!me || !project) return null;
			const bucket = (item: WorkItemView): TreeItem["bucket"] =>
				item.state === "DONE" || item.state === "CANCELED"
					? "done"
					: item.state === "TRIAGE"
						? "onyou"
						// NOW forces working even from Backlog.
						: item.state === "IN_PROGRESS" || item.work_id === now.id
							? "working"
							: "next";
			const items: TreeItem[] = inGoal.map(item => ({
				key: item.alias.key,
				title: item.revision.title,
				bucket: bucket(item),
				isNow: item.work_id === now.id,
			}));
			const counts = {
				done: items.filter(i => i.bucket === "done").length,
				total: items.length,
				stuck: items.filter(i => i.bucket === "stuck").length,
				onyou: items.filter(i => i.bucket === "onyou").length,
			};
			return { goal: project.name, ...(project.health ? { health: project.health } : {}), items, counts };
		},

		async digestExtras(): Promise<string[]> {
			const [tree, slot] = await Promise.all([client.tree(), client.focus(config.ownerId)]);
			const names = projectNames(tree);
			const inflight = tree.items.find(i => i.work_id === slot.work_id);
			const queue = tree.items.filter(i => !i.archived && i.state === "TRIAGE");
			const oldestDays = queue.length
				? Math.max(...queue.map(i => Math.floor((Date.now() - Date.parse(i.revision.created_at)) / 86_400_000)))
				: 0;
			const listed = queue.slice(0, 8).map(i => i.alias.key).join(" ");
			return [
				`IN FLIGHT: ${inflight ? `${inflight.alias.key} ${inflight.revision.title}` : "none"}`,
				`NEEDS CHRIS (${queue.length}${queue.length ? `, oldest ${oldestDays}d` : ""}): ${listed || "empty"}`,
				...(queue.length > DRAIN_MAX_QUEUE || oldestDays > DRAIN_MAX_AGE_DAYS
					? [`DRAIN RULE TRIPPED: queue ${queue.length} deep / oldest ${oldestDays}d — surface the 3 oldest to Chris for rulings this session.`]
					: []),
			];
		},

		async statusLines(now: NowRef | null): Promise<string[]> {
			const lines: string[] = [];
			try {
				const [live, ready, authority] = await Promise.all([client.healthLive(), client.healthReady(), client.authority()]);
				lines.push(`service: ${ready.ready ? "ready" : live.live ? "live, not ready" : "down"}${ready.alerts.length ? ` (${ready.alerts.join("; ")})` : ""}`);
				lines.push(`authority: ${authority.authority}${authority.epoch_id ? ` (epoch ${authority.epoch_id.slice(0, 8)}…, ${authority.epoch_state}${authority.first_work_mutation_at ? ", writes active" : ", no writes yet"})` : ""}`);
			} catch (e) {
				lines.push(`service: unreachable — ${oneRecovery(redactSecrets(String(e)))}`);
				return bounded(lines);
			}
			try {
				const slot = await client.focus(config.ownerId);
				lines.push(`focus: ${slot.work_id ? (now && slot.work_id === now.id ? `${now.key} (in sync)` : "set elsewhere — drift") : "none"} v${slot.version}`);
			} catch (e) {
				lines.push(`focus: unreadable — ${oneRecovery(redactSecrets(String(e)))}`);
			}
			return bounded(lines);
		},

		async workflowState(key: string): Promise<WorkflowCheckpoint> {
			const view = await client.workflow(key);
			const issue = toRef(view.item, view.project ? new Map([[view.project.project_id, view.project.name]]) : new Map());
			// Only receipts on the CURRENT revision + candidate count — anything older
			// is stale by the service's own freshness rule and must not rebuild a checkpoint.
			const fresh = view.receipts.filter(
				r => r.revision_id === view.item.revision.revision_id && r.candidate_id === view.item.candidate?.candidate_id,
			);
			const latest = (kind: ServiceEvidenceKind) => fresh.filter(r => r.kind === kind).at(-1);
			const plan = latest("plan");
			const handoff = latest("handoff");
			const audit = latest("audit");
			return {
				issue,
				...(plan ? { plan: { hash: planHashOf(plan), at: plan.issued_at } } : {}),
				...(handoff ? { handoff: { at: handoff.issued_at } } : {}),
				...(audit ? { review: { hash: audit.payload_sha256.slice(0, 12), at: audit.issued_at } } : {}),
			};
		},

		async waitingLines(): Promise<string[]> {
			const tree = await client.tree();
			return tree.items
				.filter(i => !i.archived && i.state === "TRIAGE")
				.map(i => `${i.alias.key} — ${i.revision.title}`);
		},

		async projectTreeLines(): Promise<string[]> {
			const tree = await client.tree();
			return tree.projects.map(p => {
				const items = tree.items.filter(i => i.project_id === p.project_id && !i.archived);
				const open = items.filter(i => i.state !== "DONE" && i.state !== "CANCELED").length;
				return `${p.name}${p.health ? ` (${healthWord(p.health)})` : ""} — ${open} open / ${items.length} total`;
			});
		},

		async setNowRemote(issue: NowRef): Promise<void> {
			const slot = await client.focus(config.ownerId);
			await run("set_focus", {
				slot: { workspace_id: config.workspaceId, owner_id: config.ownerId, work_id: issue.id, version: slot.version + 1 },
				expected_version: slot.version,
			});
		},

		async clearNowRemote(issueId: string | undefined): Promise<void> {
			if (!issueId) return;
			const slot = await client.focus(config.ownerId);
			// Never erase focus that drifted elsewhere — closing A must not clear B.
			if (!slot.work_id || slot.work_id !== issueId) return;
			await run("clear_focus", { workspace_id: config.workspaceId, owner_id: config.ownerId, expected_version: slot.version });
		},

		async stampPlan(target: NowRef, stamp: PlanStamp): Promise<{ issue: NowRef; plannedCandidateId: UUID }> {
			const item = await client.workItem(target.key);
			// Deterministic per (work, revision, plan hash): an identical retry
			// replays; a revised plan or revision mints a fresh candidate.
			const candidateId = stableId("planned-candidate", item.work_id, item.revision.revision_id, stamp.hash);
			const payload: Record<string, unknown> = {
				title: stamp.title,
				body: stamp.body,
				plan_file: stamp.planFilePath,
				plan_sha256: stamp.hash,
				approach: stamp.approach,
				verification: stamp.verification,
			};
			const planReceipt: EvidenceReceipt = {
				receipt_id: stableId("receipt", candidateId, "plan", payloadHash(payload)),
				work_id: item.work_id,
				revision_id: item.revision.revision_id,
				candidate_id: candidateId,
				kind: "plan",
				payload,
				payload_sha256: payloadHash(payload),
				issuer: ISSUER,
				issued_at: new Date().toISOString(),
				candidate_sha256: payloadHash({ planned_attempt: candidateId, ...payload }),
				independent: false,
			};
			await run("append_evidence", { receipt: planReceipt });
			return { issue: target, plannedCandidateId: candidateId };
		},

		async appendEvidence(issue: NowRef, kind: EvidenceKind, body: string, meta: EvidenceMeta): Promise<void> {
			await run("append_evidence", { receipt: await receipt(SERVICE_KIND[kind], issue, body, meta) });
		},

		async createIssue(input: { title: string; description?: string; project?: string; queue?: boolean }): Promise<NowRef> {
			const result = await run("create_work_batch", {
				items: [
					{
						client_ref: "p",
						title: input.title,
						description: input.description ?? "",
						scope: "",
						acceptance_criteria: [],
						state: input.queue ? "TRIAGE" : "BACKLOG",
						...(input.project ? { project_id: await projectIdFor(input.project) } : {}),
					},
				],
			});
			const created = result.items[0];
			return { id: created.work_id, key: created.key, title: input.title, ...(input.project ? { project: input.project } : {}) };
		},

		async createBatch(input: CreateBatchInput): Promise<BatchOutcome> {
			const projectId = input.parent.project ? await projectIdFor(input.parent.project) : undefined;
			const items = [
				{
					client_ref: "p",
					title: input.parent.title,
					description: input.parent.description ?? "",
					scope: "",
					acceptance_criteria: [] as string[],
					state: input.parent.queue ? "TRIAGE" : "BACKLOG",
					...(projectId ? { project_id: projectId } : {}),
				},
				...input.entries.map((entry, i) => ({
					client_ref: `c${i + 1}`,
					title: entry.title,
					description: entry.description ?? "",
					scope: "",
					acceptance_criteria: [] as string[],
					state: "BACKLOG",
					...(projectId ? { project_id: projectId } : {}),
				})),
			];
			const relations = [
				...input.entries.map((_, i) => ({ source_ref: `c${i + 1}`, target_ref: "p", kind: "parent" as const })),
				...input.entries.flatMap((entry, i) =>
					(entry.blocks ?? []).map(target => ({ source_ref: `c${i + 1}`, target_ref: `c${target + 1}`, kind: "blocks" as const })),
				),
			];
			const result = await run("create_work_batch", { items, ...(relations.length ? { relations } : {}) });
			const byRef = new Map(result.items.map(item => [item.client_ref, item]));
			const refOf = (ref: string): NowRef => {
				const created = byRef.get(ref);
				if (!created) throw new Error("service result missed a batch item");
				const source = ref === "p" ? input.parent : input.entries[Number(ref.slice(1)) - 1];
				return { id: created.work_id, key: created.key, title: source.title, ...(input.parent.project ? { project: input.parent.project } : {}) };
			};
			const parent = refOf("p");
			const children = input.entries.map((_, i) => refOf(`c${i + 1}`));
			const edges = [
				...children.map(child => `${child.key} ⟶ parent ${parent.key}`),
				...input.entries.flatMap((entry, i) => (entry.blocks ?? []).map(target => `${children[i].key} blocks ${children[target]?.key ?? "?"}`)),
			];
			return {
				parent,
				children,
				edges,
				text: `${parent.key} + ${children.length} child(ren)${edges.length ? ` — ${edges.join(", ")}` : ""}`,
			};
		},

		async queueIssue(issue: NowRef): Promise<void> {
			await run("set_work_state", { work_id: issue.id, state: "TRIAGE" });
		},

		async proposeClose(issue: NowRef): Promise<void> {
			await run("request_closeout", { work_id: issue.id });
		},

		async reviseWork(issue: NowRef, fields: { title?: string; description?: string }): Promise<void> {
			const item = await client.workItem(issue.key);
			const previous = item.revision;
			const title = (fields.title ?? previous.title).trim();
			const description = fields.description ?? previous.description;
			const contentSha = payloadHash({ title, description, scope: previous.scope, acceptance_criteria: previous.acceptance_criteria });
			const revision = {
				revision_id: stableId("revision", item.work_id, previous.revision_id, contentSha),
				work_id: item.work_id,
				revision_number: previous.revision_number + 1,
				title,
				description,
				scope: previous.scope,
				acceptance_criteria: previous.acceptance_criteria,
				content_sha256: contentSha,
				created_by: ISSUER,
				created_at: new Date().toISOString(),
			};
			await run("revise_work", { work_id: item.work_id, expected_revision_id: previous.revision_id, revision });
		},

		async recordHealth(project: string, health: ProjectHealth, _body: string): Promise<void> {
			await run("record_project_health", { project_id: await projectIdFor(project), health });
		},

		async closeBlocker(now: NowRef): Promise<string | null> {
			return completionPreflight(await client.workflow(now.key));
		},

		async closeWithVerdict(
			now: NowRef,
			outcome: "done" | "canceled",
			reason: string | undefined,
			carrier: WorkStateCarrier,
			hooks: BackendHooks,
		): Promise<string> {
			if (outcome === "canceled") {
				await run("set_work_state", { work_id: now.id, state: "CANCELED" });
				await backend.clearNowRemote(now.id);
				return `${now.key} canceled${reason ? ` — ${reason}` : ""}`;
			}
			const view = await client.workflow(now.key);
			const candidate = view.item.candidate;
			const commitSha = carrier.commitSha ?? candidate?.commit_sha;
			if (!candidate || candidate.kind !== "final" || !commitSha) {
				throw new Error("no finalized candidate — run /summary first");
			}
			const push = pushCandidate(hooks.cwd, commitSha);
			if (push.status === "not_pushed") {
				throw new Error(`push unverified — ${push.detail ?? "remote refused"}; the closeout intent stays pending`);
			}
			await run("append_evidence", {
				receipt: {
					...(await receipt("push", now, push.detail ?? `pushed ${push.remoteRef}`)),
					...(push.remoteRef ? { remote_ref: push.remoteRef } : {}),
					...(push.remoteCommit ? { remote_commit: push.remoteCommit } : {}),
				},
			});
			const refreshed = await client.workflow(now.key);
			const receipts = refreshed.receipts.filter(
				r => r.revision_id === refreshed.item.revision.revision_id && r.candidate_id === refreshed.item.candidate?.candidate_id,
			);
			const finalCandidate = refreshed.item.candidate;
			if (!finalCandidate) throw new Error("candidate vanished mid-close");
			await run("complete_work", {
				input: {
					work_id: now.id,
					current_revision_id: refreshed.item.revision.revision_id,
					candidate: finalCandidate,
					receipts,
					closeout_requested: true,
				},
			});
			await backend.clearNowRemote(now.id);
			return `${now.key} done — ${finalCandidate.candidate_sha256.slice(0, 12)} pushed${push.status === "remote_commit" ? " (verified on remote)" : ""}`;
		},

		async summaryGate(now: NowRef, carrier: WorkStateCarrier, hooks: BackendHooks): Promise<SummaryGateOk | SummaryGateBlocked> {
			const item = await client.workItem(now.key);
			const current = item.candidate;
			if (current?.kind === "final" && current.commit_sha) {
				const view = await client.workflow(now.key);
				const plan = view.receipts.filter(r => r.kind === "plan" && r.candidate_id === current.candidate_id).at(-1);
				return {
					ok: true,
					issue: now,
					...(plan ? { planHash: planHashOf(plan) } : {}),
					warning: plan ? undefined : "no plan evidence on this candidate — /done will refuse",
				};
			}
			// The item's CURRENT planned candidate is authoritative — the local
			// carrier is only a drift signal (cache loss/restart must not block recovery).
			if (!current) {
				return { ok: true, issue: now, warning: "No plan is stamped on this work — review may run, but /done will refuse until /plan stamps one." };
			}
			if (carrier.plannedCandidateId && carrier.plannedCandidateId !== current.candidate_id) {
				hooks.notices.push(`candidate drift: local cache names ${carrier.plannedCandidateId.slice(0, 8)}, ledger has ${current.candidate_id.slice(0, 8)} — following the ledger`);
			}
			const gate = await client.workflow(now.key);
			const planned = gate.receipts.some(r => r.kind === "plan" && r.candidate_id === current.candidate_id);
			if (!planned) {
				return { ok: true, issue: now, warning: "No plan is stamped on this work — review may run, but /done will refuse until /plan stamps one." };
			}
			const freeze = await freezeCandidateCommit(hooks.ui, hooks.cwd, now.key, current.candidate_id, hooks.preExistingDirtyPaths);
			if (!freeze) return { ok: false, reason: "candidate freeze refused — nothing frozen, /summary stays blocked" };
			const candidateId = stableId("final-candidate", item.work_id, item.revision.revision_id, current.candidate_id, freeze.commitSha);
			await run("finalize_candidate", {
				work_id: item.work_id,
				revision_id: item.revision.revision_id,
				planned_candidate_id: current.candidate_id,
				candidate_id: candidateId,
				candidate_sha256: candidateSha256(freeze.commitSha, freeze.paths),
				commit_sha: freeze.commitSha,
			});
			const view = await client.workflow(now.key);
			const plan = view.receipts.filter(r => r.kind === "plan" && r.candidate_id === candidateId).at(-1);
			return { ok: true, issue: now, ...(plan ? { planHash: planHashOf(plan) } : {}) };
		},
		deliveredOps(): UUID[] {
			return [...deliveredOps];
		},

		async ackOps(delivered: UUID[]): Promise<void> {
			await ackClaimOps(pendingDir, new Set(delivered));
			const acked = new Set(delivered);
			for (let i = deliveredOps.length - 1; i >= 0; i--) {
				if (acked.has(deliveredOps[i]!)) deliveredOps.splice(i, 1);
			}
		},
	};
	return backend;
}
