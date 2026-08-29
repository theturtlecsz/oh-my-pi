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
import { basename } from "node:path";
import {
	type Candidate,
	candidateSha256,
	type CloseAttempt,
	type CloseAttemptEvent,
	type Command,
	type CommandEnvelope,
	type CommandResult,
	type EvidenceKind as ServiceEvidenceKind,
	type EvidenceReceipt,
	type Fetch,
	payloadHash,
	type ProjectHealth,
	sha256Hex,
	type UUID,
	WorkClient,
	WorkError,
	type WorkItemView,
	type WorkflowView,
	type WorkspaceTree,
	type ExecutionGrantItemClaim,
	type ExecutionGrantItemView,
	type ExecutionGrantView,
	type ExecutionJudgeManifest,
	type ExecutionMode,
	type ExecutionProvenanceEnvelope,
	type ExecutionView,
} from "@oh-my-pi/pi-work-client";
import {
	type BackendHooks,
	type BatchOutcome,
	CandidateDriftError,
	type CenterCommandRecommendation,
	type CenterHiddenRow,
	type CenterSnapshot,
	type CenterWaitingRow,
	type CloseAttemptOutcome,
	type CloseAttemptSession,
	type CloseAttemptSnapshot,
	type CloseEventView,
	type CreateBatchInput,
	type EvidenceKind,
	extractOwnerQuestion,
	setOwnerQuestion,
	type EvidenceMeta,
	type GoalTree,
	type IssueDetail,
	type MapSurface,
	type NowRef,
	nowRefusal,
	sameSessionSections,
	PLAN_PACKET_MAX_BYTES,
	type PlanPacket,
	type CancellationProof,
	type RiderProof,
	type PlanStamp,
	type SealedAuditTask,
	type SummaryGateBlocked,
	type SummaryGateOk,
	type TreeItem,
	type WorkStateCarrier,
	type WorkflowBackend,
	type WorkflowCheckpoint,
	type ExecutionSnapshot,
} from "./backend";
import { pendingOpsDir, type WorkClientConfig } from "./config";
import { candidateDrift, type CandidateDriftShape, freezeCandidateCommit, headCommit, pushCandidate } from "./git";
import { ackOps as ackClaimOps, claimPendingOp, dropPendingOp, intentFingerprint, readPendingClaims, resolvePendingOp } from "./pending-ops";
import { bounded, healthWord, oneRecovery, redactSecrets } from "./status";

const DRAIN_MAX_QUEUE = 8;
const DRAIN_MAX_AGE_DAYS = 14;
/** /center row bound — matches the existing queue digest bound (DRAIN_MAX_QUEUE). */
const CENTER_MAX_ROWS = 8;

function terminalNextAction(shape?: CandidateDriftShape): string {
	switch (shape) {
		case "unchanged":
			return "code still matches the reviewed snapshot — enter /summary for a fresh attempt";
		case "fixes-on-top":
			return "code changed since the reviewed snapshot — enter /plan, then /summary";
		case "unrelated":
			return "current code is not based on the reviewed snapshot — restore the reviewed snapshot or enter /plan, then /summary";
		default:
			return "if code changed since the reviewed snapshot, enter /plan then /summary; otherwise enter /summary";
	}
}

function parseKeyNumber(key: string): number {
	const match = key.match(/-(\d+)$/);
	return match ? parseInt(match[1], 10) : 0;
}

function attemptNextAction(state: string, shape?: CandidateDriftShape): string {
	switch (state) {
		case "active":
			return 'append verification evidence (work action:"append_evidence", kind:"verification")';
		case "audit_ready":
			return 'run native audit (work action:"run_audit")';
		case "auditor_in_flight":
			return "auditor launch in flight — wait for settlement";
		case "audited":
			return 'append closeout review (work action:"append_evidence", kind:"closeout")';
		case "closeout_requested":
			return "owner /done closes";
		case "remediation_required": {
			const action = terminalNextAction(shape);
			return shape ? action : `fix the findings; ${action}`;
		}
		case "blocked": {
			const action = terminalNextAction(shape);
			return shape ? action : `resolve the blocker; ${action}`;
		}
		case "budget_exhausted":
		case "superseded":
			return terminalNextAction(shape);
		case "completed":
			return "attempt completed";
		default:
			return "continue workflow";
	}
}
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
 *  (`plan`/`push` are minted internally by stampPlan/closeWithVerdict; `audit`
 *  is minted ONLY by the service's settle transaction — OMP-47). */
const SERVICE_KIND: Record<string, ServiceEvidenceKind> = {
	handoff: "handoff",
	verification: "verification",
	closeout: "closeout",
	same_session_found_fixed: "same_session_found_fixed",
	push: "push",
	plan: "plan",
};

/** Attempt states that own the single live slot per work item (OMP-47). */
const LIVE_ATTEMPT_STATES: Record<string, true> = { active: true, audit_ready: true, auditor_in_flight: true, audited: true, closeout_requested: true };

export function liveAttempt(view: WorkflowView): CloseAttempt | undefined {
	return view.close_attempts.find(attempt => LIVE_ATTEMPT_STATES[attempt.state] === true);
}

function eventView(event: CloseAttemptEvent): CloseEventView {
	return {
		eventId: event.event_id,
		eventType: event.event_type,
		reasonCode: event.reason_code,
		renderedText: event.rendered_text,
		renderedSha256: event.rendered_sha256,
		requiresDelivery: event.requires_delivery,
		requiresFreshAuthorization: event.requires_fresh_authorization,
	};
}

function outcomeOf(result: { status: "applied" | "refused"; attempt?: CloseAttempt | null; verdict?: "PASS" | "NEEDS_FIX" | "BLOCKED" | null; launch?: { launch_id: UUID } | null; event: CloseAttemptEvent }): CloseAttemptOutcome {
	return {
		status: result.status,
		...(result.attempt ? { attemptId: result.attempt.attempt_id, attemptState: result.attempt.state } : {}),
		...(result.verdict ? { verdict: result.verdict } : {}),
		...(result.launch ? { launchId: result.launch.launch_id } : {}),
		event: eventView(result.event),
	};
}

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

/** Fallback AC source for imported/thin revisions: bullet/numbered items of
 *  the `## Acceptance criteria` section only — nothing else in the description. */
export function acceptanceFromDescription(description: string): string[] {
	const lines = description.split("\n");
	const start = lines.findIndex(line => /^##\s+Acceptance criteria\b/i.test(line));
	if (start < 0) return [];
	const criteria: string[] = [];
	for (const line of lines.slice(start + 1)) {
		if (/^#{1,6}\s/.test(line)) break;
		const item = /^\s*(?:[-*]|\d+[.)])\s+(.*\S)/.exec(line);
		if (item?.[1]) criteria.push(item[1]);
	}
	return criteria;
}

/** The approved-plan identity lives in the payload (stamp.hash), not the
 *  receipt hash; imported/pre-147 receipts fall back to the payload hash. */
export function planHash(receiptRow: EvidenceReceipt): string {
	const stamped = receiptRow.payload.plan_sha256;
	return typeof stamped === "string" && /^[0-9a-f]{64}$/.test(stamped) ? stamped : receiptRow.payload_sha256;
}

export { sameSessionSections };

/** Render cost of one packet line as get_work emits it ("- " + text + "\n") —
 *  the cap sizes what the owner-facing render actually costs, so a flood of
 *  tiny/empty criteria cannot slip under a bytes-only sum. */
const CRITERION_RENDER_OVERHEAD = 3;

/** OMP-155: one packet price for both the read cap and the pre-write gate —
 *  UTF-8 body bytes plus each criterion's rendered cost. stampPlan refuses
 *  what buildPlanPacket would cap, so a stored plan receipt always renders. */
function planPacketBytes(body: string, criteria: readonly string[]): number {
	return (
		Buffer.byteLength(body, "utf8")
		+ criteria.reduce((total, criterion) => total + Buffer.byteLength(criterion, "utf8") + CRITERION_RENDER_OVERHEAD, 0)
	);
}

/** OMP-38 bounded audit-reconstruction packet: NEWEST plan receipt on the
 *  CURRENT candidate — deterministically newest by issued_at then receipt_id,
 *  independent of service row order — or undefined when no candidate/plan
 *  receipt exists. Over the byte ceiling the packet says so and withholds the
 *  body — bytes are never silently truncated. */
export function buildPlanPacket(view: WorkflowView): PlanPacket | undefined {
	const candidate = view.item.candidate;
	if (!candidate) return undefined;
	const plan = view.receipts
		.filter(r => r.kind === "plan" && r.candidate_id === candidate.candidate_id)
		.sort((a, b) => a.issued_at.localeCompare(b.issued_at) || a.receipt_id.localeCompare(b.receipt_id))
		.at(-1);
	if (!plan) return undefined;
	const body = typeof plan.payload.body === "string" ? plan.payload.body : "";
	const revision = view.item.revision;
	const criteria =
		revision.acceptance_criteria.length > 0 ? revision.acceptance_criteria : acceptanceFromDescription(revision.description);
	const bytes = planPacketBytes(body, criteria);
	const base = {
		candidateId: candidate.candidate_id,
		candidateSha256: candidate.candidate_sha256,
		...(candidate.commit_sha ? { commitSha: candidate.commit_sha } : {}),
		planReceiptSha256: plan.payload_sha256,
		planSha256: planHash(plan),
		baseCommit:
			typeof plan.payload.base_commit === "string" && /^[0-9a-f]{40,64}$/.test(plan.payload.base_commit)
				? plan.payload.base_commit
				: undefined,
		baseDirtyPaths:
			Array.isArray(plan.payload.base_dirty_paths) && plan.payload.base_dirty_paths.every(path => typeof path === "string")
				? [...plan.payload.base_dirty_paths]
				: undefined,
	};
	if (bytes > PLAN_PACKET_MAX_BYTES) {
		return { ...base, acceptanceCriteria: [], capped: { bytes, max: PLAN_PACKET_MAX_BYTES } };
	}
	return { ...base, planBody: body, acceptanceCriteria: criteria };
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
			state: item.state,
			archived: item.archived,
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

	async function receipt(kind: ServiceEvidenceKind, issue: NowRef, body: string, _meta?: EvidenceMeta & { priorTip?: string | null }): Promise<EvidenceReceipt> {
		const item = await client.workItem(issue.key);
		if (kind === "same_session_found_fixed") {
			// OMP-52: the child's receipt binds the PARENT attempt's audited candidate.
			const sections = sameSessionSections(body);
			if (!sections) {
				throw new Error("a same_session_found_fixed body needs non-empty `## Finding` and `## Verification` sections");
			}
			const view = await client.workflow(issue.key);
			const parentEdge = view.relations.find(edge => edge.active && edge.kind === "parent" && edge.source_work_id === item.work_id);
			if (!parentEdge) throw new Error(`${item.alias.key} has no active parent — same-session receipts ride the parent's close attempt`);
			const tree = await client.tree();
			const parentKey = tree.items.find(candidate => candidate.work_id === parentEdge.target_work_id)?.alias.key;
			if (!parentKey) throw new Error("parent work item not found");
			const attempt = liveAttempt(await client.workflow(parentKey));
			if (!attempt || !attempt.candidate_sha256 || !attempt.candidate_commit || !attempt.owner_session_id || !attempt.owner_session_start_commit) {
				throw new Error(`${parentKey} has no live summary close attempt — run /summary on the parent first`);
			}
			const payload: Record<string, unknown> = {
				attempt_id: attempt.attempt_id,
				owner_session_id: attempt.owner_session_id,
				base_commit: attempt.owner_session_start_commit,
				fix_commit: attempt.candidate_commit,
				candidate_sha256: attempt.candidate_sha256,
				finding: sections.finding,
				verification: sections.verification,
			};
			const payloadSha = payloadHash(payload);
			return {
				receipt_id: stableId("receipt", item.work_id, item.revision.revision_id, attempt.candidate_id, kind, payloadSha),
				work_id: item.work_id,
				revision_id: item.revision.revision_id,
				candidate_id: attempt.candidate_id,
				kind,
				payload,
				payload_sha256: payloadSha,
				issuer: ISSUER,
				issued_at: new Date().toISOString(),
				independent: false,
			};
		}
		const candidate = item.candidate;
		if (!candidate) throw new Error(`${item.alias.key} has no current candidate — run /plan first`);
		if (kind === "verification" && !candidate.commit_sha) {
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
		if (kind === "verification") {
			return { ...base, candidate_sha256: _meta?.candidateSha256 ?? candidate.candidate_sha256, ...((_meta?.candidateCommit ?? candidate.commit_sha) ? { candidate_commit: _meta?.candidateCommit ?? candidate.commit_sha } : {}) };
		}
		if (kind === "push") {
			const pushPayload = {
				repository: basename(process.cwd()),
				remote_url: "git@github.com:owner/oh-my-pi.git",
				remote_ref: _meta?.remoteRef ?? "refs/heads/main",
				prior_tip: _meta?.priorTip !== undefined ? _meta.priorTip : (item.candidate?.commit_sha ?? null),
				candidate_commit: _meta?.candidateCommit ?? candidate.commit_sha,
				result_tip: _meta?.remoteCommit ?? candidate.commit_sha,
				detail: body,
			};
			const pSha = payloadHash(pushPayload);
			return {
				...base,
				payload: pushPayload,
				payload_sha256: pSha,
				candidate_sha256: _meta?.candidateSha256 ?? candidate.candidate_sha256,
				...((_meta?.candidateCommit ?? candidate.commit_sha) ? { candidate_commit: _meta?.candidateCommit ?? candidate.commit_sha } : {}),
				...(_meta?.remoteRef ? { remote_ref: _meta.remoteRef } : {}),
				...(_meta?.remoteCommit ? { remote_commit: _meta.remoteCommit } : {}),
			};
		}
		return base;
	}

	async function pushAndRecordCandidate(now: NowRef, commitSha: string, hooks: BackendHooks) {
		const push = pushCandidate(hooks.cwd, commitSha);
		if (push.status === "not_pushed") return push;
		const view = await client.workflow(now.key);
		const candidate = view.item.candidate;
		const alreadyRecorded =
			candidate?.kind === "final" &&
			candidate.commit_sha === commitSha &&
			view.receipts.some(
				receipt =>
					receipt.kind === "push" &&
					receipt.revision_id === view.item.revision.revision_id &&
					receipt.candidate_id === candidate.candidate_id &&
					receipt.remote_ref === push.remoteRef &&
					receipt.remote_commit === push.remoteCommit,
			);
		if (!alreadyRecorded) {
			const r = await receipt("push", now, push.detail ?? `pushed ${push.remoteRef}`, {
				candidateCommit: commitSha,
				remoteRef: push.remoteRef,
				remoteCommit: push.remoteCommit,
			});
			const pushPayload = {
				repository: basename(hooks.cwd),
				remote_url: push.remoteUrl,
				remote_ref: push.remoteRef,
				prior_tip: push.priorTip ?? null,
				candidate_commit: commitSha,
				result_tip: push.remoteCommit ?? commitSha,
				detail: push.detail,
			};
			await run("append_evidence", {
				receipt: {
					...r,
					payload: pushPayload,
					payload_sha256: payloadHash(pushPayload),
					...(push.remoteRef ? { remote_ref: push.remoteRef } : {}),
					...(push.remoteCommit ? { remote_commit: push.remoteCommit } : {}),
					...(push.status === "contained" ? { candidate_commit: commitSha } : {}),
				},
			});
		}
		return push;
	}

	function linesOf(view: WorkflowView): string {
		return view.receipts
			.map(r => `${r.kind} ${r.issued_at.slice(0, 19)} candidate ${r.candidate_id ?? "none"} revision ${r.revision_id} ${r.verdict ? `${r.verdict} ` : ""}${r.payload_sha256.slice(0, 12)}`)
			.join("\n");
	}


	/** Preflight mirror of semantics.completion_blockers. /summary records the
	 *  first verified push; closeWithVerdict repeats it as an idempotent remote
	 *  check before complete_work. The service remains the authority. */
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
		const attempt = liveAttempt(view);
		if (!attempt || attempt.state !== "closeout_requested" || attempt.candidate_id !== candidate.candidate_id) {
			return "no requested close attempt on this candidate — complete /summary close review before /done";
		}
		return null;
	}

	const backend: WorkflowBackend = {
		name: "work",
		serviceLabel: "Work Ledger",
		workClient: client,
		workspaceId: config.workspaceId,
		markerFile: ".work-project",
		scopeFix: 'echo "<Exact Project Name>" > .work-project at the repo root',
		cacheFile: "work-now.json",
		queueNoun: "TRIAGE",
		reviewKind: "closeout",
		evidenceKinds: ["handoff", "verification", "closeout", "same_session_found_fixed"],
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
			const packet = buildPlanPacket(view);
			const attempt = liveAttempt(view) ?? (view.close_attempts ?? []).slice().sort((a, b) => b.requested_at.localeCompare(a.requested_at))[0];
			const manifest = view.audit_manifest;
			const hasManifest = !!(attempt && manifest && manifest.attempt_id === attempt.attempt_id);
			const isLaunchable = !!(attempt && attempt.state === "audit_ready" && hasManifest);
			const auditTask: SealedAuditTask | undefined =
				attempt && manifest && manifest.attempt_id === attempt.attempt_id
					? { attemptId: attempt.attempt_id, attemptState: attempt.state, taskBody: manifest.task_body, taskSha256: manifest.task_sha256 }
					: undefined;
			const remainingLaunches = attempt ? 3 - (attempt.launch_count - attempt.cancelled_launch_count) : 3;
			const remainingReports = attempt ? 2 - attempt.accepted_report_count : 2;
			const attemptSnapshot: CloseAttemptSnapshot | undefined = attempt
				? {
						attemptId: attempt.attempt_id,
						state: attempt.state,
						...(attempt.candidate_id ? { candidateId: attempt.candidate_id } : {}),
						...(attempt.candidate_sha256 ? { candidateSha: attempt.candidate_sha256 } : {}),
						...(attempt.candidate_commit ? { candidateCommit: attempt.candidate_commit } : {}),
						remainingLaunches,
						remainingReports,
						hasManifest,
						isLaunchable,
						nextAction: attemptNextAction(attempt.state),
					}
				: undefined;
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
				...(packet ? { planPacket: packet } : {}),
				...(auditTask ? { auditTask } : {}),
				...(attemptSnapshot ? { attemptSnapshot } : {}),
			};
		},

		async findIssue(key: string): Promise<NowRef> {
			try {
				const item = await client.workItem(key);
				return toRef(item, projectNames(await client.tree()));
			} catch {
				const tree = await client.tree();
				const item = tree.items.find(i => i.work_id === key || i.alias.key === key);
				if (item) return toRef(item, projectNames(tree));
				throw new Error(`issue ${key} not found`);
			}
		},

		async currentNow(): Promise<NowRef | null> {
			const slot = await client.focus(config.ownerId);
			if (!slot.work_id) return null;
			const tree = await client.tree();
			const item = tree.items.find(i => i.work_id === slot.work_id);
			if (!item) return null;
			const ref = toRef(item, projectNames(tree));
			// A focus slot left pointing at closed work (canceled/done outside the
			// canonical flows) is stale — never resurrect it as NOW (owner ruling 2026-08-25).
			return nowRefusal(ref) ? null : ref;
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

		async digestExtras(cwd: string): Promise<string[]> {
			const [tree, slot] = await Promise.all([client.tree(), client.focus(config.ownerId)]);
			const names = projectNames(tree);
			const inflight = tree.items.find(i => i.work_id === slot.work_id);
			const queue = tree.items.filter(i => !i.archived && i.state === "TRIAGE");
			const oldestDays = queue.length
				? Math.max(...queue.map(i => Math.floor((Date.now() - Date.parse(i.revision.created_at)) / 86_400_000)))
				: 0;
			const listed = queue.slice(0, 8).map(i => i.alias.key).join(" ");
			const extraLines: string[] = [
				`IN FLIGHT: ${inflight ? `${inflight.alias.key} ${inflight.revision.title}` : "none"}`,
				`NEEDS CHRIS (${queue.length}${queue.length ? `, oldest ${oldestDays}d` : ""}): ${listed || "empty"}`,
				...(queue.length > DRAIN_MAX_QUEUE || oldestDays > DRAIN_MAX_AGE_DAYS
					? [`DRAIN RULE TRIPPED: queue ${queue.length} deep / oldest ${oldestDays}d — surface the 3 oldest to Chris for rulings this session.`]
					: []),
			];
			if (inflight) {
				const view = await client.workflow(inflight.alias.key);
				const attempt = liveAttempt(view);
				const pending = await backend.pendingDeliveries(inflight.alias.key);
				if (pending.length > 0) {
					extraLines.push(`CHECKPOINT DELIVERY PENDING (${pending.length}): /done remains blocked until delivered or owner-waived.`);
				}
				if (attempt) {
					switch (attempt.state) {
						case "active":
							extraLines.push("CLOSE ATTEMPT: active — close ritual started; enter /summary to resume verification/sealing");
							break;
						case "audit_ready":
							extraLines.push("CLOSE ATTEMPT: audit_ready — sealed audit ready; enter /summary to resume the auditor step");
							break;
						case "auditor_in_flight":
							extraLines.push("CLOSE ATTEMPT: auditor_in_flight — auditor launch is in flight; do not launch another; recover/settle that launch");
							break;
						case "audited":
							extraLines.push("CLOSE ATTEMPT: audited — PASS audit saved; enter /summary to resume close review—nothing will be erased");
							break;
						case "closeout_requested":
							extraLines.push("CLOSE ATTEMPT: closeout_requested — close review saved; enter /done");
							break;
					}
				} else {
					const terminal = (view.close_attempts ?? []).slice().sort((a, b) => b.requested_at.localeCompare(a.requested_at))[0];
					if (terminal && ["remediation_required", "blocked", "budget_exhausted", "superseded"].includes(terminal.state)) {
						const drift = candidateDrift(cwd, terminal.candidate_commit);
						const nextAction = attemptNextAction(terminal.state, drift.shape);
						extraLines.push(`CLOSE ATTEMPT: ${terminal.state} (${terminal.terminal_reason ?? "terminal"}) — ${nextAction}`);
					}
				}
			}
			return extraLines;
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
			// OMP-134/OMP-137: project the live attempt + its newest service event so
			// the host gates closeout continuations on real ledger state.
			const attempt = liveAttempt(view);
			const attemptEvent = attempt
				? (view.close_attempt_events ?? [])
						.filter(e => e.attempt_id === attempt.attempt_id)
						.sort((a, b) => (b.sequence ?? 0) - (a.sequence ?? 0))[0]
				: undefined;
			return {
				issue,
				...(plan ? { plan: { hash: planHash(plan), at: plan.issued_at } } : {}),
				...(handoff ? { handoff: { at: handoff.issued_at } } : {}),
				...(audit ? { review: { hash: audit.payload_sha256.slice(0, 12), at: audit.issued_at } } : {}),
				...(attempt
					? { closeAttempt: { state: attempt.state, ...(attemptEvent ? { latestEventText: attemptEvent.rendered_text } : {}) } }
					: {}),
			};
		},

		async waitingLines(): Promise<string[]> {
			const tree = await client.tree();
			return tree.items
				.filter(i => !i.archived && i.state === "TRIAGE")
				.map(i => {
					const q = extractOwnerQuestion(i.revision.description);
					return `${i.alias.key} — ${q ?? "question not recorded"}`;
				});
		},

		async projectTreeLines(): Promise<string[]> {
			const tree = await client.tree();
			return tree.projects.map(p => {
				const items = tree.items.filter(i => i.project_id === p.project_id && !i.archived);
				const open = items.filter(i => i.state !== "DONE" && i.state !== "CANCELED").length;
				return `${p.name}${p.health ? ` (${healthWord(p.health)})` : ""} — ${open} open / ${items.length} total`;
			});
		},

		async centerSnapshot(projectFilter?: string): Promise<CenterSnapshot> {
			// ONE concurrent batch of read-only WorkService calls
			const treePromise = client.tree();
			const focusPromise = client.focus(config.ownerId);
			const activityPromise = (async (): Promise<CenterSnapshot["activity"] | undefined> => {
				if (projectFilter) {
					const tree = await treePromise;
					const scopeId = tree.projects.find(p => p.name === projectFilter)?.project_id;
					if (!scopeId) return undefined;
					try {
						const view = await client.activity({ projectId: scopeId, limit: 1 });
						return {
							rows: view.events.map(e => `${e.occurred_at.slice(0, 16).replace("T", " ")} ${e.kind} — ${e.key} ${e.title}`),
							total: view.total,
						};
					} catch (error) {
						return { unavailable: redactSecrets(String(error)) };
					}
				}
				try {
					const view = await client.activity({ limit: 1 });
					return {
						rows: view.events.map(e => `${e.occurred_at.slice(0, 16).replace("T", " ")} ${e.kind} — ${e.key} ${e.title}`),
						total: view.total,
					};
				} catch (error) {
					return { unavailable: redactSecrets(String(error)) };
				}
			})();

			const [tree, slot, activityResult] = await Promise.all([treePromise, focusPromise, activityPromise]);
			const names = projectNames(tree);
			const nowItem = slot.work_id ? tree.items.find(i => i.work_id === slot.work_id) : undefined;
			const now = nowItem ? toRef(nowItem, names) : null;
			const scopeId = projectFilter ? tree.projects.find(p => p.name === projectFilter)?.project_id : undefined;
			if (projectFilter && !scopeId) {
				// FAIL CLOSED: a stale marker must never silently widen the
				// orientation to the whole workspace under a project-scope banner.
				throw new Error(`project "${projectFilter}" from ${backend.markerFile} does not exist in the Work Ledger — fix the marker or create the project`);
			}
			const activity: CenterSnapshot["activity"] = activityResult ?? { unavailable: "activity unavailable" };
			let progress: CenterSnapshot["progress"];
			const targetProjectId = scopeId ?? nowItem?.project_id;
			if (targetProjectId) {
				const goal = tree.items.filter(i => !i.archived && i.project_id === targetProjectId);
				progress = {
					done: goal.filter(i => i.state === "DONE" || i.state === "CANCELED").length,
					total: goal.length,
					onyou: goal.filter(i => i.state === "TRIAGE").length,
				};
			}

			const openById = new Map(tree.items.filter(i => !i.archived && i.state !== "DONE" && i.state !== "CANCELED").map(i => [i.work_id, i]));
			const blockedByMap = new Map<string, string>();
			for (const edge of tree.relations) {
				if (edge.active && edge.kind === "blocks" && openById.has(edge.source_work_id) && openById.has(edge.target_work_id)) {
					const blocker = openById.get(edge.source_work_id);
					if (blocker) blockedByMap.set(edge.target_work_id, blocker.alias.key);
				}
			}

			const scopedOpen = tree.items.filter(i => !i.archived && i.state !== "DONE" && i.state !== "CANCELED" && (!scopeId || i.project_id === scopeId));
			const waitingRows: CenterWaitingRow[] = [];
			const hiddenRows: CenterHiddenRow[] = [];
			const readyUnblocked: WorkItemView[] = [];

			let nowEligibleForRecommendation = false;
			if (nowItem) {
				const isTerminal = nowItem.state === "DONE" || nowItem.state === "CANCELED" || nowItem.archived;
				if (!isTerminal) {
					const blockerKey = blockedByMap.get(nowItem.work_id);
					const inScope = !scopeId || nowItem.project_id === scopeId;
					if (blockerKey) {
						if (inScope) hiddenRows.push({ key: nowItem.alias.key, reason: `blocked by ${blockerKey}` });
					} else if (nowItem.state === "TRIAGE") {
						const q = extractOwnerQuestion(nowItem.revision.description);
						if (inScope) {
							if (q) {
								waitingRows.push({ key: nowItem.alias.key, question: q });
							} else {
								hiddenRows.push({ key: nowItem.alias.key, reason: "question not recorded" });
							}
						}
					} else {
						nowEligibleForRecommendation = true;
					}
				}
			}

			for (const item of scopedOpen) {
				if (item.work_id === slot.work_id) continue;
				const blockerKey = blockedByMap.get(item.work_id);
				if (blockerKey) {
					hiddenRows.push({ key: item.alias.key, reason: `blocked by ${blockerKey}` });
					continue;
				}
				if (item.state === "TRIAGE") {
					const q = extractOwnerQuestion(item.revision.description);
					if (q) {
						waitingRows.push({ key: item.alias.key, question: q });
					} else {
						hiddenRows.push({ key: item.alias.key, reason: "question not recorded" });
					}
				} else {
					readyUnblocked.push(item);
				}
			}

			const recommendations: CenterCommandRecommendation[] = [];
			if (nowItem && nowEligibleForRecommendation) {
				const view = await client.workflow(nowItem.alias.key);
				const hasPlan = view.receipts.some(r => r.kind === "plan" && r.candidate_id === view.item.candidate?.candidate_id);
				if (!hasPlan || !view.item.candidate) {
					recommendations.push({ command: "/plan", reason: "no approved plan stamped on current work" });
				} else {
					const live = liveAttempt(view);
					if (live && live.state === "closeout_requested") {
						recommendations.push({ command: "/done", reason: "closeout review complete — ready to close" });
					} else if (live && live.state === "audited") {
						recommendations.push({ command: "/summary", reason: "PASS audit saved — resume close review" });
					} else if (live && (live.state === "audit_ready" || live.state === "active" || live.state === "auditor_in_flight")) {
						recommendations.push({ command: "/summary", reason: "audited review in progress" });
					} else if (view.receipts.some(r => r.kind === "handoff" || r.kind === "verification") || view.item.candidate?.kind === "final") {
						recommendations.push({ command: "/summary", reason: "ready for review" });
					} else {
						recommendations.push({ command: `continue ${nowItem.alias.key}`, reason: "plan approved — finish execution" });
					}
				}
			}

			for (const item of readyUnblocked) {
				if (recommendations.length >= 3) break;
				const projectLabel = item.project_id ? (tree.projects.find(p => p.project_id === item.project_id)?.name ?? "") : "";
				const reason = projectLabel ? `next unblocked piece in ${projectLabel}` : "next unblocked piece";
				recommendations.push({ command: `/now ${item.alias.key}`, reason });
			}

			return {
				now,
				...(progress ? { progress } : {}),
				recommendations,
				waiting: { rows: waitingRows.slice(0, 8), total: waitingRows.length },
				hidden: { rows: hiddenRows.slice(0, 8), total: hiddenRows.length },
				activity,
			};
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

		async getFocusVersion(): Promise<number> {
			const slot = await client.focus(config.ownerId);
			return slot.version;
		},

		async stampPlan(target: NowRef, stamp: PlanStamp): Promise<{ issue: NowRef; plannedCandidateId: UUID }> {
			const item = await client.workItem(target.key);
			// OMP-155: price the packet BEFORE any write — same math as
			// buildPlanPacket, so a stamp the packet would cap is refused here
			// instead of stranding /summary behind an unrenderable receipt.
			const criteria =
				item.revision.acceptance_criteria.length > 0
					? item.revision.acceptance_criteria
					: acceptanceFromDescription(item.revision.description);
			const bytes = planPacketBytes(stamp.body, criteria);
			if (bytes > PLAN_PACKET_MAX_BYTES) {
				throw new Error(`Approved plan packet is ${bytes} bytes, over the ${PLAN_PACKET_MAX_BYTES}-byte limit; shorten the plan or acceptance criteria.`);
			}
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
				...(stamp.baseCommit ? { base_commit: stamp.baseCommit } : {}),
				...(stamp.baseDirtyPaths ? { base_dirty_paths: stamp.baseDirtyPaths } : {}),
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

		async appendEvidence(issue: NowRef, kind: EvidenceKind, body: string, meta: EvidenceMeta, authorizationRef?: string): Promise<CloseAttemptOutcome | EvidenceReceipt | void> {
			const r = await receipt(SERVICE_KIND[kind], issue, body, meta);
			if (kind === "closeout") {
				const view = await client.workflow(issue.key);
				const attempt = liveAttempt(view);
				if (!attempt) throw new Error(`${issue.key} has no live close attempt — run /summary first`);
				if (!authorizationRef) throw new Error("closeout review requires summary authorization reference");
				const result = await run("record_closeout_review", {
					receipt: r,
					attempt_id: attempt.attempt_id,
					authorization_ref: authorizationRef,
				});
				if (result.status === "refused") {
					throw new Error(result.event.rendered_text);
				}
				return outcomeOf(result);
			}
			await run("append_evidence", { receipt: r });
			return r;
		},

		async createIssue(input: { title: string; description?: string; project?: string; queue?: boolean; question?: string }): Promise<NowRef> {
			const description = input.question ? setOwnerQuestion(input.description, input.question) : (input.description ?? "");
			const result = await run("create_work_batch", {
				items: [
					{
						client_ref: "p",
						title: input.title,
						description,
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

		async createSameSessionChild(input: { parentKey: string; ownerSessionId: string; title: string; description?: string; finding: string; verification: string }): Promise<NowRef> {
			// OMP-139: bind the filing to the parent's LIVE attempt; the service
			// re-validates parent, attempt, session, and candidate atomically.
			const view = await client.workflow(input.parentKey);
			const attempt = liveAttempt(view);
			if (!attempt) throw new Error(`${input.parentKey} has no live summary close attempt — run /summary on the parent first`);
			const result = await run("create_same_session_child", {
				parent_work_id: view.item.work_id,
				attempt_id: attempt.attempt_id,
				owner_session_id: input.ownerSessionId,
				item: {
					client_ref: "c",
					title: input.title,
					description: input.description ?? "",
					scope: "",
					acceptance_criteria: [],
					state: "BACKLOG",
				},
				finding: input.finding,
				verification: input.verification,
			});
			const created = result.item;
			return { id: created.work_id, key: created.key, title: input.title, ...(view.project ? { project: view.project.name } : {}) };
		},

		async createBatch(input: CreateBatchInput): Promise<BatchOutcome> {
			const projectId = input.parent.project ? await projectIdFor(input.parent.project) : undefined;
			const parentDescription = input.parent.question
				? setOwnerQuestion(input.parent.description, input.parent.question)
				: (input.parent.description ?? "");
			const items = [
				{
					client_ref: "p",
					title: input.parent.title,
					description: parentDescription,
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

		async queueIssue(issue: NowRef, question?: string): Promise<void> {
			if (question) {
				const item = await client.workItem(issue.key);
				const updatedDesc = setOwnerQuestion(item.revision.description, question);
				if (updatedDesc !== item.revision.description) {
					await backend.reviseWork(issue, { description: updatedDesc });
				}
			}
			await run("set_work_state", { work_id: issue.id, state: "TRIAGE" });
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

		async recordHealth(project: string, health: ProjectHealth): Promise<void> {
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
			doneAuthorizationRef?: string,
			cancellations?: CancellationProof[],
		): Promise<string> {
			if (outcome === "canceled") {
				await run("set_work_state", { work_id: now.id, state: "CANCELED" });
				await backend.clearNowRemote(now.id);
				return `${now.key} canceled${reason ? ` — ${reason}` : ""}`;
			}
			if (!doneAuthorizationRef) throw new Error("a done close needs the host-minted /done authorization reference");
			const view = await client.workflow(now.key);
			const candidate = view.item.candidate;
			const commitSha = carrier.commitSha ?? candidate?.commit_sha;
			if (!candidate || candidate.kind !== "final" || !commitSha) {
				throw new Error("no finalized candidate — run /summary first");
			}
			const push = await pushAndRecordCandidate(now, commitSha, hooks);
			if (push.status === "not_pushed") {
				throw new Error(`push unverified — ${push.detail ?? "remote refused"}; the close attempt stays requested`);
			}
			const refreshed = await client.workflow(now.key);
			const receipts = refreshed.receipts.filter(
				r => r.revision_id === refreshed.item.revision.revision_id && r.candidate_id === refreshed.item.candidate?.candidate_id,
			);
			const finalCandidate = refreshed.item.candidate;
			if (!finalCandidate) throw new Error("candidate vanished mid-close");
			const attempt = liveAttempt(refreshed);
			if (!attempt) throw new Error("no live close attempt — run /summary first");
			// OMP-52: children found+fixed in this owner session ride the parent's
			// audited candidate — include every child whose current revision carries
			// a same_session receipt bound to THIS attempt; the service revalidates
			// each one transactionally and refuses the whole close on any mismatch.
			const satisfied: UUID[] = [];
			const childEdges = refreshed.relations.filter(edge => edge.active && edge.kind === "parent" && edge.target_work_id === now.id);
			const treeItems = childEdges.length > 0 ? (await client.tree()).items : [];
			for (const edge of childEdges) {
				const child = treeItems.find(candidateItem => candidateItem.work_id === edge.source_work_id);
				if (!child || child.state === "DONE" || child.state === "CANCELED") continue;
				const childView = await client.workflow(child.alias.key);
				const bound = childView.receipts.some(
					r => r.kind === "same_session_found_fixed" && r.revision_id === child.revision.revision_id && r.payload.attempt_id === attempt.attempt_id,
				);
				if (bound) satisfied.push(child.work_id);
			}
			const result = await run("complete_work", {
				input: {
					work_id: now.id,
					current_revision_id: refreshed.item.revision.revision_id,
					candidate: finalCandidate,
					receipts,
					closeout_requested: true,
				},
				attempt_id: attempt.attempt_id,
				done_authorization_ref: doneAuthorizationRef,
				...(satisfied.length > 0 ? { satisfied_work_ids: satisfied } : {}),
				...(cancellations && cancellations.length > 0 ? { cancellations } : {}),
			});
			if (result.status === "refused") throw new Error(result.event?.rendered_text ?? "completion refused");
			await backend.clearNowRemote(now.id);
			const children = result.status === "applied" && result.completed_work_ids?.length ? ` (+${result.completed_work_ids.length} same-session child(ren))` : "";
			const cancels = result.status === "applied" && result.canceled_work_ids?.length ? ` (+${result.canceled_work_ids.length} canceled)` : "";
			return `${now.key} done${children}${cancels} — ${finalCandidate.candidate_sha256.slice(0, 12)} ${push.status === "contained" ? `contained in remote tip ${push.remoteCommit?.slice(0, 12)}` : `pushed${push.status === "remote_commit" ? " (verified on remote)" : ""}`}`;
		},

		async summaryGate(now: NowRef, carrier: WorkStateCarrier, hooks: BackendHooks): Promise<SummaryGateOk | SummaryGateBlocked> {
			const item = await client.workItem(now.key);
			const current = item.candidate;
			if (current?.kind === "final" && current.commit_sha) {
				const drift = candidateDrift(hooks.cwd, current.commit_sha);
				if (drift.shape === "fixes-on-top") {
					return {
						ok: false,
						reason: `Code changed after the reviewed snapshot. Run /plan to approve the current code, then run /summary.\nDetails: reviewed commit ${current.commit_sha}; current commit ${drift.head}.`,
					};
				}
				if (drift.shape === "unrelated") {
					if (!drift.head) {
						return {
							ok: false,
							reason: `Current code position could not be read. Restore the reviewed snapshot, or run /plan to approve the current code and then run /summary.\nDetails: reviewed commit ${current.commit_sha}; current commit unreadable.`,
						};
					}
					return {
						ok: false,
						reason: `Current code is on a different history from the reviewed snapshot. Restore the reviewed snapshot, or run /plan to approve the current code and then run /summary.\nDetails: reviewed commit ${current.commit_sha}; current commit ${drift.head}.`,
					};
				}
				const push = await pushAndRecordCandidate(now, current.commit_sha, hooks);
				if (push.status === "not_pushed") {
					return {
						ok: false,
						reason: `push unverified — ${push.detail ?? "remote refused"}; candidate remains frozen and no close attempt began. Fix the remote and re-enter /summary`,
					};
				}
				const view = await client.workflow(now.key);
				const packet = buildPlanPacket(view);
				if (packet && (!packet.baseCommit || packet.baseDirtyPaths === undefined)) {
					return { ok: false, reason: "The approved plan predates audit-range binding. Re-enter /plan to restamp it before /summary." };
				}
				const auditBaseCommit = packet?.baseCommit;
				const auditBaseDirtyPaths = packet?.baseDirtyPaths;
				return {
					ok: true,
					issue: now,
					...(packet ? { planHash: packet.planSha256 } : {}),
					...(auditBaseCommit ? { auditBaseCommit } : {}),
					...(auditBaseDirtyPaths ? { auditBaseDirtyPaths } : {}),
					warning: packet ? undefined : "no plan evidence on this candidate — /done will refuse",
					carrier: { candidateId: current.candidate_id, candidateSha: current.candidate_sha256, commitSha: current.commit_sha },
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
			const plannedPacket = buildPlanPacket(gate);
			if (!plannedPacket?.baseCommit || plannedPacket.baseDirtyPaths === undefined) {
				return { ok: false, reason: "The approved plan predates audit-range binding. Re-enter /plan to restamp it before /summary." };
			}
			const freeze = await freezeCandidateCommit(hooks.ui, hooks.cwd, now.key, current.candidate_id, plannedPacket.baseDirtyPaths);
			if ("refused" in freeze) return { ok: false, reason: freeze.reason };
			const candidateId = stableId("final-candidate", item.work_id, item.revision.revision_id, current.candidate_id, freeze.commitSha);
			const frozenSha = candidateSha256(freeze.commitSha, freeze.paths);
			const finalized = await run("finalize_candidate", {
				work_id: item.work_id,
				revision_id: item.revision.revision_id,
				planned_candidate_id: current.candidate_id,
				candidate_id: candidateId,
				candidate_sha256: frozenSha,
				commit_sha: freeze.commitSha,
			});
			const push = await pushAndRecordCandidate(now, freeze.commitSha, hooks);
			if (push.status === "not_pushed") {
				return {
					ok: false,
					reason: `push unverified — ${push.detail ?? "remote refused"}; candidate remains frozen and no close attempt began. Fix the remote and re-enter /summary`,
				};
			}
			const view = await client.workflow(now.key);
			const packet = buildPlanPacket(view);
			const auditBaseCommit = packet?.baseCommit;
			const auditBaseDirtyPaths = packet?.baseDirtyPaths;
			return {
				ok: true,
				issue: now,
				...(packet ? { planHash: packet.planSha256 } : {}),
				...(auditBaseCommit ? { auditBaseCommit } : {}),
				...(auditBaseDirtyPaths ? { auditBaseDirtyPaths } : {}),
				carrier: { candidateId: finalized.candidate.candidate_id, candidateSha: finalized.candidate.candidate_sha256, commitSha: freeze.commitSha },
			};
		},

		// ---- OMP-47 close attempts: the service owns every gate; these are transport ----

		async beginCloseAttempt(now: NowRef, session: CloseAttemptSession): Promise<CloseAttemptOutcome> {
			const result = await run("begin_close_attempt", {
				work_id: now.id,
				attempt_id: stableId("close-attempt", now.id, session.authorizationRef),
				authorization_ref: session.authorizationRef,
				owner_session_id: session.sessionId,
				owner_session_started_at: session.startedAt,
				owner_session_start_commit: session.startCommit,
				repository: session.repository,
				diff_sha256: session.diffSha256,
				starting_dirty_paths: session.dirtyPaths,
				...(session.authorization_kind ? { authorization_kind: session.authorization_kind } : {}),
				...(session.execution_grant_id ? { execution_grant_id: session.execution_grant_id } : {}),
				...(session.candidate_tree_sha ? { candidate_tree_sha: session.candidate_tree_sha } : {}),
				...(session.original_request_sha256 ? { original_request_sha256: session.original_request_sha256 } : {}),
				...(session.criteria_sha256 ? { criteria_sha256: session.criteria_sha256 } : {}),
				...(session.plan_stamp_sha256 ? { plan_stamp_sha256: session.plan_stamp_sha256 } : {}),
				...(session.judge_sha256 ? { judge_sha256: session.judge_sha256 } : {}),
				...(session.riders?.length ? { riders: session.riders } : {}),
			});
			return outcomeOf(result);
		},

		async resolveRiders(entries: { key: string; evidence: string }[]): Promise<RiderProof[]> {
			const riders: RiderProof[] = [];
			for (const entry of entries) {
				if (!entry?.key || typeof entry.evidence !== "string" || !entry.evidence.trim()) {
					throw new Error(`rider entry for ${entry?.key ?? "(missing key)"} needs a key and non-empty evidence`);
				}
				const view = await client.workflow(entry.key);
				if (view.item.state === "DONE" || view.item.state === "CANCELED" || view.item.state === "CANCELLED" || view.item.archived) {
					throw new Error(`rider ${entry.key} is terminal (${view.item.archived ? "archived" : view.item.state}) — riders complete only open work`);
				}
				riders.push({ work_id: view.item.work_id, revision_id: view.item.revision.revision_id, evidence: entry.evidence });
			}
			return riders;
		},

		async resolveCancellations(entries: { key: string; reason: string }[], nowKey: string): Promise<CancellationProof[]> {
			const proofs: CancellationProof[] = [];
			const seenIds = new Set<string>();
			for (const entry of entries) {
				if (!entry?.key || typeof entry.reason !== "string" || !entry.reason.trim()) {
					throw new Error(`cancel entry for ${entry?.key ?? "(missing key)"} needs a key and non-empty reason`);
				}
				if (new TextEncoder().encode(entry.reason).length > 4096) {
					throw new Error(`cancel entry for ${entry.key} reason exceeds 4096 UTF-8 bytes`);
				}
				if (entry.reason.includes("\0")) {
					throw new Error(`cancel entry for ${entry.key} reason contains NUL`);
				}
				const view = await client.workflow(entry.key);
				if (view.item.work_id === nowKey || view.item.alias.key === nowKey) {
					throw new Error(`cannot cancel the primary work item (${entry.key}) in its own completion batch`);
				}
				if (seenIds.has(view.item.work_id)) {
					throw new Error(`duplicate cancellation target (${entry.key})`);
				}
				seenIds.add(view.item.work_id);
				if (view.item.state === "DONE" || view.item.state === "CANCELED" || view.item.state === "CANCELLED" || view.item.archived) {
					throw new Error(`cancellation target ${entry.key} is terminal (${view.item.archived ? "archived" : view.item.state}) — cancellations apply only to open work`);
				}
				proofs.push({ work_id: view.item.work_id, revision_id: view.item.revision.revision_id, reason: entry.reason });
			}
			return proofs;
		},

		async sealAuditManifest(now: NowRef): Promise<CloseAttemptOutcome> {
			const view = await client.workflow(now.key);
			const attempt = liveAttempt(view);
			if (!attempt) throw new Error(`${now.key} has no live close attempt — run /summary first`);
			const verification = view.receipts
				.filter(r => r.kind === "verification" && r.revision_id === attempt.revision_id && r.candidate_id === attempt.candidate_id)
				.sort((a, b) => a.issued_at.localeCompare(b.issued_at) || a.receipt_id.localeCompare(b.receipt_id))
				.at(-1);
			if (!verification) throw new Error(`${now.key} has no verification receipt on the attempt's candidate — append verification first`);
			const result = await run("seal_audit_manifest", { attempt_id: attempt.attempt_id, verification_receipt_id: verification.receipt_id });
			return outcomeOf(result);
		},

		async sealedAuditTask(key: string): Promise<SealedAuditTask | null> {
			const view = await client.workflow(key);
			const attempt = liveAttempt(view);
			const manifest = view.audit_manifest;
			if (!attempt || !manifest || manifest.attempt_id !== attempt.attempt_id) return null;
			return { attemptId: attempt.attempt_id, attemptState: attempt.state, taskBody: manifest.task_body, taskSha256: manifest.task_sha256 };
		},

		async reserveAuditorLaunch(key: string, taskSha256: string, toolCallId: string): Promise<CloseAttemptOutcome> {
			const attempt = liveAttempt(await client.workflow(key));
			if (!attempt) throw new Error(`${key} has no live close attempt — run /summary first`);
			const result = await run("reserve_auditor_launch", { attempt_id: attempt.attempt_id, task_sha256: taskSha256, tool_call_id: toolCallId });
			return outcomeOf(result);
		},

		async cancelAuditorLaunch(key: string, launchId: string): Promise<CloseAttemptOutcome> {
			const view = await client.workflow(key);
			const launch = view.auditor_launches.find(row => row.launch_id === launchId);
			const attemptId = launch?.attempt_id ?? liveAttempt(view)?.attempt_id;
			if (!attemptId) throw new Error(`${key} has no launch ${launchId.slice(0, 8)}… and no live close attempt — nothing to cancel`);
			return outcomeOf(await run("cancel_auditor_launch", { attempt_id: attemptId, launch_id: launchId }));
		},

		async settleAuditorLaunch(key: string, launchId: string, transport: { payload?: unknown; failed?: boolean }): Promise<CloseAttemptOutcome> {
			// Resolve the attempt from the immutable launch row, NOT the live slot:
			// a lost settle response may have already moved the attempt terminal,
			// and the retry must still replay the stored result (same intent).
			const view = await client.workflow(key);
			const launch = view.auditor_launches.find(row => row.launch_id === launchId);
			const attemptId = launch?.attempt_id ?? liveAttempt(view)?.attempt_id;
			if (!attemptId) throw new Error(`${key} has no launch ${launchId.slice(0, 8)}… and no live close attempt — nothing to settle`);
			const result = await run("settle_auditor_launch", {
				attempt_id: attemptId,
				launch_id: launchId,
				...(transport.failed ? { transport_failed: true } : { transport_payload: transport.payload }),
			});
			return outcomeOf(result);
		},

		async pendingDeliveries(key: string): Promise<CloseEventView[]> {
			const view = await client.workflow(key);
			const latestByEvent = new Map<string, string>();
			for (const delivery of view.checkpoint_deliveries ?? []) {
				// deliveries arrive ordered by created_at; the highest sequence wins
				const prior = latestByEvent.get(delivery.event_id);
				if (prior === undefined || delivery.delivery_sequence >= Number(prior.split(":")[0])) {
					latestByEvent.set(delivery.event_id, `${delivery.delivery_sequence}:${delivery.status}`);
				}
			}
			return (view.close_attempt_events ?? [])
				.filter(event => {
					if (!event.requires_delivery) return false;
					const latest = latestByEvent.get(event.event_id);
					const status = latest?.split(":")[1];
					return status !== "delivered" && status !== "waived";
				})
				.map(eventView);
		},

		async attestDelivery(eventId: string, ownerSessionId: string, renderedSha256: string, status: "delivered" | "failed" | "waived", authorizationRef?: string): Promise<CloseAttemptOutcome> {
			const result = await run("attest_checkpoint_delivery", {
				event_id: eventId,
				owner_session_id: ownerSessionId,
				rendered_sha256: renderedSha256,
				status,
				...(authorizationRef ? { authorization_ref: authorizationRef } : {}),
			});
			return outcomeOf(result);
		},

		async getExecution(key?: string): Promise<ExecutionSnapshot | null> {
			try {
				const view = await client.execution(key ?? "");
				return {
					grant: view.grant,
					items: view.items,
					activeItem: view.active_item ?? null,
				};
			} catch {
				return null;
			}
		},

		async finalizeExecutionCandidate(key: string, plannedCandidateId: string, freeze: { commitSha: string; candidateSha256: string; paths: string[] }): Promise<Candidate> {
			const view = await client.workflow(key);
			const finalCandidateId = randomUUID();
			const result = await run("finalize_candidate", {
				work_id: view.item.work_id,
				revision_id: view.item.revision.revision_id,
				planned_candidate_id: plannedCandidateId,
				candidate_id: finalCandidateId,
				candidate_sha256: freeze.candidateSha256,
				commit_sha: freeze.commitSha,
			});
			if (result.type !== "finalize_candidate") throw new Error(`unexpected result ${result.type}`);
			return result.candidate;
		},

		async snapshotQueue(projectFilter?: string, currentKey?: string, cwd?: string): Promise<ExecutionGrantItemClaim[]> {
			const tree = await client.tree();
			const relations = tree.relations ?? [];
			const currentHead = headCommit(cwd ?? process.cwd()) ?? "";
			const itemById = new Map<string, WorkItemView>();
			for (const item of tree.items) itemById.set(item.work_id, item);

			const blockedWorkIds = new Set<string>();
			const activeBlockersByItem = new Map<string, string[]>();
			for (const rel of relations) {
				if (rel.active && rel.kind === "blocks") {
					const list = activeBlockersByItem.get(rel.target_work_id) ?? [];
					list.push(rel.source_work_id);
					activeBlockersByItem.set(rel.target_work_id, list);
					const source = itemById.get(rel.source_work_id);
					if (source && source.state !== "DONE" && source.state !== "CANCELED" && source.state !== "CANCELLED") {
						blockedWorkIds.add(rel.target_work_id);
					}
				}
			}

			let targetProjectId: string | null = null;
			let currentItem: WorkItemView | null = null;
			if (currentKey) {
				currentItem = tree.items.find(i => i.alias.key === currentKey) ?? null;
				if (currentItem) targetProjectId = currentItem.project_id;
			}

			const isEligible = (item: WorkItemView): boolean => {
				if (item.archived) return false;
				if (["DONE", "CANCELED", "CANCELLED", "TRIAGE", "BLOCKED"].includes(item.state)) return false;
				if (extractOwnerQuestion(item.revision.description)) return false;
				if (blockedWorkIds.has(item.work_id)) return false;
				if (targetProjectId !== null && item.project_id !== targetProjectId) return false;
				return true;
			};

			if (currentItem && !isEligible(currentItem)) {
				currentItem = null;
			}

			const eligibleItems = tree.items.filter(isEligible);
			const remaining = eligibleItems.filter(i => !currentItem || i.work_id !== currentItem.work_id);
			remaining.sort((a, b) => {
				const numA = parseKeyNumber(a.alias.key);
				const numB = parseKeyNumber(b.alias.key);
				if (numA !== numB) return numA - numB;
				return a.work_id.localeCompare(b.work_id);
			});

			const ordered = currentItem ? [currentItem, ...remaining] : remaining;
			return ordered.map((item, idx) => ({
				work_id: item.work_id,
				revision_id: item.revision.revision_id,
				position: idx,
				original_request: item.revision.description,
				original_request_sha256: sha256Hex(item.revision.description),
				initial_git_baseline: currentHead,
				project_id: item.project_id,
				active_blocker_ids: (activeBlockersByItem.get(item.work_id) ?? []).sort(),
			}));
		},

		async beginExecution(input): Promise<ExecutionSnapshot> {
			const grantId = randomUUID();
			const result = await run("begin_execution", {
				grant_id: grantId,
				provenance: input.provenance,
				remote_ref: input.remoteRef,
				mode: input.mode,
				items: input.items,
				expected_focus_version: input.expectedFocusVersion,
				judge_sha256: input.judgeSha256,
				judge_manifest: input.judgeManifest,
			});
			if (result.type !== "begin_execution") throw new Error(`unexpected result ${result.type}`);
			const activeItem = result.items.find(i => i.position === 0) ?? null;
			return { grant: result.grant, items: result.items, activeItem };
		},

		async activateExecutionItem(input): Promise<ExecutionSnapshot> {
			const result = await run("activate_execution_item", {
				grant_id: input.grantId,
				expected_grant_version: input.expectedGrantVersion,
				position: input.position,
				work_id: input.workId,
				expected_revision_id: input.expectedRevisionId,
				git_baseline: input.gitBaseline,
				judge_sha256: input.judgeSha256,
				expected_focus_version: input.expectedFocusVersion,
				...(input.expectedProjectId ? { expected_project_id: input.expectedProjectId } : {}),
				...(input.expectedBlockerIds ? { expected_blocker_ids: input.expectedBlockerIds } : {}),
			});
			if (result.type !== "activate_execution_item") throw new Error(`unexpected result ${result.type}`);
			const view = await client.execution(input.grantId);
			return { grant: result.grant, items: view.items, activeItem: result.item };
		},

		async sealExecutionCriteria(input): Promise<ExecutionSnapshot & { sealedCriteria: string[] }> {
			const result = await run("seal_execution_criteria", {
				grant_id: input.grantId,
				expected_grant_version: input.expectedGrantVersion,
				work_id: input.workId,
				expected_revision_id: input.expectedRevisionId,
				criteria: input.criteria,
				description_sha256: input.descriptionSha256,
				judge_sha256: input.judgeSha256,
			});
			if (result.type !== "seal_execution_criteria") throw new Error(`unexpected result ${result.type}`);
			const view = await client.execution(input.grantId);
			return {
				grant: result.grant,
				items: view.items,
				activeItem: result.item,
				sealedCriteria: result.revision.acceptance_criteria,
			};
		},

		async stampExecutionPlan(input): Promise<ExecutionSnapshot> {
			const result = await run("stamp_execution_plan", {
				grant_id: input.grantId,
				expected_grant_version: input.expectedGrantVersion,
				work_id: input.workId,
				revision_id: input.revisionId,
				candidate_id: input.candidateId,
				plan_file: input.planFile,
				plan_body: input.planBody,
				plan_sha256: input.planSha256,
				approach: input.approach,
				verification: input.verification,
				paths: input.paths,
				candidate_sha256: input.candidateSha256,
				judge_sha256: input.judgeSha256,
			});
			if (result.type !== "stamp_execution_plan") throw new Error(`unexpected result ${result.type}`);
			const view = await client.execution(input.grantId);
			return { grant: result.grant, items: view.items, activeItem: result.item };
		},

		async setExecutionState(input): Promise<ExecutionSnapshot> {
			const result = await run("set_execution_state", {
				grant_id: input.grantId,
				expected_grant_version: input.expectedGrantVersion,
				target_state: input.targetState,
				reason: input.reason,
				judge_sha256: input.judgeSha256,
			});
			if (result.type !== "set_execution_state") throw new Error(`unexpected result ${result.type}`);
			const view = await client.execution(input.grantId);
			return { grant: result.grant, items: view.items, activeItem: view.active_item ?? null };
		},

		async completeExecutionItem(input): Promise<ExecutionSnapshot> {
			const result = await run("complete_execution_item", {
				grant_id: input.grantId,
				expected_grant_version: input.expectedGrantVersion,
				work_id: input.workId,
				attempt_id: input.attemptId,
				push_receipt_id: input.pushReceiptId,
				judge_sha256: input.judgeSha256,
			});
			if (result.type !== "complete_execution_item") throw new Error(`unexpected result ${result.type}`);
			const view = await client.execution(input.grantId);
			return { grant: result.grant, items: view.items, activeItem: result.item };
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
		async getPendingExecutionClaims() {
			const { records, unreadable } = await readPendingClaims(pendingDir);
			if (unreadable.length > 0) {
				throw new Error(`unreadable pending claim(s): ${unreadable.join(", ")} — refusing recovery to prevent duplicate execution`);
			}
			const results: Array<{ command: Command; result?: CommandResult }> = [];
			for (const r of records) {
				const env = r.envelope as CommandEnvelope | undefined;
				const cmd = env?.command;
				const res = r.result as CommandResult | undefined;
				if (cmd && (cmd.type === "set_execution_state" || cmd.type === "begin_execution")) {
					results.push({ command: cmd, result: res });
				}
			}
			return results;
		},
	};
	return backend;
}
