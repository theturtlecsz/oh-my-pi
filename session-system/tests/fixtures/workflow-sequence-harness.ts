// HOME-122 harness: drive the real work-now extension through its public
// events, command, and tool with a deterministic in-memory WorkService REST API.
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, getAgentDir, loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { resolveLocalUrlToPath } from "@oh-my-pi/pi-coding-agent/internal-urls";
import { confirmRoundTrip } from "./two-phase";
import { currentTranscriptRef } from "../../extensions/workflow/transcript";
import { createWorkBackend } from "../../extensions/workflow/work";
import { riderBatchPath } from "../../extensions/workflow/rider-batch";
const probe = process.argv[2];
const mode = process.argv[3];
const MODES = ["intake", "plan", "plan-now-change", "summary", "summary-subagent", "summary-reauth", "summary-push-fail", "summary-stale-final", "summary-begin-refused", "summary-refusal-durable", "summary-rider-refusal-durable", "stop-continuation-states", "atomic-child", "done", "done-cancel", "done-cancel-decline", "footer", "audit", "restore", "now-canceled", "center", "center-scoped", "center-stale", "triage-questions", "ledger-reads", "ledger-reads-subagent", "closeout-pending-recovery", "ledger", "descriptions", "omp140-audit-states", "omp140-restart-flow", "omp140-failed-checkpoint", "omp140-terminal-guidance"];
if (!probe || !mode || !MODES.includes(mode)) throw new Error(`usage: harness <probe-repo> ${MODES.join("|")}`);
// OMP-25 scoped centering: the marker must exist before the extension loads.
if (mode === "center-scoped") fs.writeFileSync(path.join(probe, ".work-project"), "The Bookends\n");
// OMP-25 stale marker: names a project the ledger does not have — /center must refuse.
if (mode === "center-stale") fs.writeFileSync(path.join(probe, ".work-project"), "No Such Project\n");

interface Comment {
	body: string;
	createdAt: string;
}

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";
const issue = { id: "id-1", identifier: "HOME-1", title: "First", project: undefined as { name: string } | undefined };
const comments: Comment[] = [];
const writes = { created: 0, addNow: 0, removeNow: 0, closed: 0, canceled: 0 };
let nowSelected = mode === "restore" || mode === "now-canceled";
let nowId: string | null = mode === "restore" ? "id-1" : mode === "now-canceled" ? "id-2" : null;
let slotVersion = 1;
const receipts: Array<Record<string, unknown>> = [];
interface MockAttempt {
	attempt_id: string;
	work_id: string;
	revision_id: string;
	candidate_id: string;
	plan_receipt_id: string | null;
	candidate_sha256: string | null;
	candidate_commit: string | null;
	owner_session_id: string | null;
	owner_session_started_at: string | null;
	owner_session_start_commit: string | null;
	repository: string | null;
	diff_sha256: string | null;
	starting_dirty_paths: string[];
	authorization_kind: string;
	authorization_ref: string;
	launch_count: number;
	cancelled_launch_count: number;
	accepted_report_count: number;
	in_flight_launch_id: string | null;
	state: string;
	terminal_reason: string | null;
	requested_at: string;
	closeout_requested_at: string | null;
	completed_at: string | null;
	completion_authorization_ref: string | null;
}
const attempts: MockAttempt[] = [];
const manifests: Array<Record<string, unknown>> = [];
const launches: Array<Record<string, unknown>> = [];
const closeEvents: Array<Record<string, unknown>> = [];
const deliveries: Array<Record<string, unknown>> = [];
let eventSeq = 0;
const beginCalls: Array<Record<string, unknown>> = [];
const settleCalls: Array<Record<string, unknown>> = [];
const cancelCalls: Array<Record<string, unknown>> = [];
const attestCalls: Array<Record<string, unknown>> = [];
const sscCalls: Array<Record<string, unknown>> = [];
// Deterministic attestation rendezvous (OMP-154): fixtures await the exact
// attest_checkpoint_delivery arrival instead of sleeping on wall-clock timers.
const attestWaiters: Array<() => void> = [];
function nextAttestation(): Promise<void> {
	const { promise, resolve } = Promise.withResolvers<void>();
	attestWaiters.push(resolve);
	return promise;
}
function mockEvent(workId: string, attemptId: string | null, eventType: string, reasonCode: string, requiresDelivery: boolean, legalNextActions: string[] = []): Record<string, unknown> {
	eventSeq += 1;
	const rendered = `CLOSE ATTEMPT — ${eventType}\n${reasonCode}: mock`;
	const event = {
		event_id: `ev-${eventSeq}`,
		sequence: eventSeq,
		work_id: workId,
		attempt_id: attemptId,
		launch_id: null,
		event_type: eventType,
		reason_code: reasonCode,
		reason: "mock",
		legal_next_actions: legalNextActions,
		remaining_launches: 3,
		remaining_reports: 2,
		requires_fresh_authorization: false,
		rendered_text: rendered,
		rendered_sha256: new Bun.CryptoHasher("sha256").update(rendered, "utf8").digest("hex"),
		requires_delivery: requiresDelivery,
		created_at: new Date().toISOString(),
	};
	closeEvents.push(event);
	return event;
}

interface MockWorkItem {
	work_id: string;
	workspace_id: string;
	alias: { key: string };
	revision: { revision_id: string; title: string; description: string; scope: string; acceptance_criteria: string[] };
	state: string;
	project_id: string | null;
	candidate: { candidate_id: string; candidate_sha256: string; commit_sha?: string } | null;
}

const items = new Map<string, MockWorkItem>();
const initialItem: MockWorkItem = {
	work_id: "id-1",
	workspace_id: WORKSPACE_ID,
	alias: { key: "HOME-1" },
	revision: {
		revision_id: "rev-1",
		title: "First",
		description: mode === "descriptions" ? `${"x".repeat(1201)} GET_WORK_SENTINEL` : "",
		scope: "",
		// OMP-38 audit mode: real structured criteria the PLAN PACKET must carry.
		acceptance_criteria: mode === "audit" ? ["AC-1 the focused check passes"] : [],
	},
	state: "IN_PROGRESS",
	project_id: "proj-1",
	candidate: null,
};
items.set("HOME-1", initialItem);
items.set("id-1", initialItem);
if (mode === "done-cancel" || mode === "done-cancel-decline") {
	const item2: MockWorkItem = {
		work_id: "id-2",
		workspace_id: WORKSPACE_ID,
		alias: { key: "HOME-2" },
		revision: { revision_id: "rev-2", title: "Second to cancel", description: "", scope: "", acceptance_criteria: [] },
		state: "BACKLOG",
		project_id: "proj-1",
		candidate: null,
	};
	items.set("HOME-2", item2);
	items.set("id-2", item2);
}
// Owner ruling 2026-08-25: closed work never becomes or stays NOW. The mode
// starts with the focus slot pointing at a CANCELED item.
if (mode === "now-canceled") {
	const canceledItem: MockWorkItem = {
		work_id: "id-2",
		workspace_id: WORKSPACE_ID,
		alias: { key: "HOME-2" },
		revision: { revision_id: "rev-2", title: "Second, canceled", description: "", scope: "", acceptance_criteria: [] },
		state: "CANCELED",
		project_id: "proj-1",
		candidate: null,
	};
	items.set("HOME-2", canceledItem);
	items.set("id-2", canceledItem);
}
if (mode === "plan-now-change") {
	// OMP-124: a second open item the owner switches NOW to mid-plan.
	const second: MockWorkItem = {
		work_id: "id-2",
		workspace_id: WORKSPACE_ID,
		alias: { key: "HOME-2" },
		revision: { revision_id: "rev-2", title: "Second", description: "", scope: "", acceptance_criteria: [] },
		state: "BACKLOG",
		project_id: "proj-1",
		candidate: null,
	};
	items.set("HOME-2", second);
	items.set("id-2", second);
}
if (mode === "ledger-reads") {
	const openItem: MockWorkItem = {
		...initialItem,
		work_id: "id-2",
		alias: { key: "HOME-2" },
		revision: { ...initialItem.revision, revision_id: "rev-2", title: "Zulu open item", description: "" },
		state: "BACKLOG",
		project_id: "proj-1",
		candidate: null,
	};
	const closedItem: MockWorkItem = {
		...initialItem,
		work_id: "id-3",
		alias: { key: "HOME-3" },
		revision: { ...initialItem.revision, revision_id: "rev-3", title: "Closed thing", description: "" },
		state: "DONE",
		project_id: "proj-1",
		candidate: null,
	};
	const canceledItem: MockWorkItem = {
		...initialItem,
		work_id: "id-4",
		alias: { key: "HOME-4" },
		revision: { ...initialItem.revision, revision_id: "rev-4", title: "Killed thing", description: "" },
		state: "CANCELED",
		project_id: "proj-1",
		candidate: null,
	};
	items.set("HOME-2", openItem);
	items.set("id-2", openItem);
	items.set("HOME-3", closedItem);
	items.set("id-3", closedItem);
	items.set("HOME-4", canceledItem);
	items.set("id-4", canceledItem);
}
if (mode === "center" || mode === "center-scoped") {
	// pads the unscoped READY list.
	const elsewhere: MockWorkItem = {
		work_id: "id-x",
		workspace_id: WORKSPACE_ID,
		alias: { key: "HOME-99" },
		revision: { revision_id: "rev-x", title: "Elsewhere item", description: "", scope: "", acceptance_criteria: [] },
		state: "BACKLOG",
		project_id: "proj-2",
		candidate: null,
	};
	items.set("HOME-99", elsewhere);
	items.set("id-x", elsewhere);
	const parked: MockWorkItem = {
		work_id: "id-t",
		workspace_id: WORKSPACE_ID,
		alias: { key: "HOME-50" },
		revision: { revision_id: "rev-t", title: "Parked decision", description: "", scope: "", acceptance_criteria: [] },
		state: "TRIAGE",
		project_id: "proj-1",
		candidate: null,
	};
	items.set("HOME-50", parked);
	items.set("id-t", parked);
	const decisionWithQuestion: MockWorkItem = {
		work_id: "id-q",
		workspace_id: WORKSPACE_ID,
		alias: { key: "HOME-16" },
		revision: {
			revision_id: "rev-q",
			title: "Decision on fleet controller",
			description: "## Owner question\nShould work resume on the autonomous fleet controller?",
			scope: "",
			acceptance_criteria: [],
		},
		state: "TRIAGE",
		project_id: "proj-1",
		candidate: null,
	};
	items.set("HOME-16", decisionWithQuestion);
	items.set("id-q", decisionWithQuestion);
	const blockedItem: MockWorkItem = {
		work_id: "id-b",
		workspace_id: WORKSPACE_ID,
		alias: { key: "HOME-10" },
		revision: { revision_id: "rev-b", title: "Blocked task", description: "", scope: "", acceptance_criteria: [] },
		state: "BACKLOG",
		project_id: "proj-1",
		candidate: null,
	};
	items.set("HOME-10", blockedItem);
	items.set("id-b", blockedItem);
}
let commandPosts = 0;
const activityCalls: string[] = [];
// The first six activity reads fail in unscoped center mode (the failure
// scenarios each take a snapshot before the real first run) — proves the
// fourth section degrades honestly while the other three survive.
const commandTypes: string[] = [];
let activityFailuresLeft = mode === "center" ? 6 : 0;
let idle = true;
let busyDuringSnapshot = false;
let throwNextTree = false;
let switchSessionOnNextTree = false;
let throwNextDeliverMessage = false;
let throwNextWorkflowRead = false;
globalThis.fetch = (async (url: unknown, init?: { body?: string; method?: string }) => {
	const u = String(url);
	const method = init?.method ?? "GET";

	if (u.includes("/v1/health/live") || u.includes("/v1/health/ready")) {
		return new Response(JSON.stringify({ live: true, ready: true, alerts: [] }), { status: 200 });
	}
	if (u.includes("/authority")) {
		return new Response(JSON.stringify({ authority: "work", epoch_id: null, epoch_state: "sealed" }), { status: 200 });
	}
	if (u.includes("/tree")) {
		if (throwNextTree) {
			throwNextTree = false;
			return new Response(JSON.stringify({ error: { code: "unavailable", diagnostics: ["tree service down"] } }), { status: 503 });
		}
		if (switchSessionOnNextTree) {
			switchSessionOnNextTree = false;
			currentSessionId = "session-switched";
			await runner.emit({ type: "session_switch", reason: "new" } as never);
		}
		return new Response(
			JSON.stringify({
				projects: [
					{ project_id: "proj-1", workspace_id: WORKSPACE_ID, name: "The Bookends", health: "onTrack" },
					...(mode === "center" || mode === "center-scoped" ? [{ project_id: "proj-2", workspace_id: WORKSPACE_ID, name: "Elsewhere", health: "onTrack" }] : []),
					...(mode === "ledger-reads" ? [{ project_id: "proj-3", workspace_id: WORKSPACE_ID, name: "Empty Surface", health: "onTrack" }] : []),
				],
				items: Array.from(new Set(items.values())),
				relations: (mode === "center" || mode === "center-scoped")
					? [{ workspace_id: WORKSPACE_ID, source_work_id: "id-1", target_work_id: "id-b", kind: "blocks", active: true }]
					: [],
			}),
			{ status: 200 },
		);
	}
	if (u.includes(`/focus/${OWNER_ID}`)) {
		return new Response(
			JSON.stringify({
				workspace_id: WORKSPACE_ID,
				owner_id: OWNER_ID,
				work_id: nowSelected ? (nowId ?? "id-1") : null,
				version: slotVersion,
			}),
			{ status: 200 },
		);
	}
	if (u.includes("/activity")) {
		activityCalls.push(u);
		if (busyDuringSnapshot) {
			busyDuringSnapshot = false;
			idle = false;
		}
		if (activityFailuresLeft > 0) {
			activityFailuresLeft--;
			return new Response(JSON.stringify({ error: { code: "unavailable", request_id: null, correlation_id: null, diagnostics: [] } }), { status: 503 });
		}
		const scoped = u.includes("project_id=proj-1");
		const events = [
			{ kind: "handoff", work_id: "id-1", key: "HOME-1", title: "First", project_id: "proj-1", occurred_at: "2026-08-19T12:00:00+00:00" },
			...(scoped ? [] : [{ kind: "completed", work_id: "id-x", key: "HOME-99", title: "Elsewhere item", project_id: "proj-2", occurred_at: "2026-08-19T11:00:00+00:00" }]),
		];
		return new Response(JSON.stringify({ workspace_id: WORKSPACE_ID, total: scoped ? 9 : events.length, events }), { status: 200 });
	}
	if (u.endsWith("/workflow")) {
		if (throwNextWorkflowRead) {
			throwNextWorkflowRead = false;
			return new Response(JSON.stringify({ error: "injected workflow read failure" }), { status: 500 });
		}
		const key = u.split("/v1/work-items/")[1]?.split("/")[0] ?? "HOME-1";
		const it = items.get(key) ?? initialItem;
		const plan = receipts.filter(r => r.kind === "plan").at(-1) as Record<string, unknown> | undefined;
		const handoff = receipts.filter(r => r.kind === "handoff").at(-1) as Record<string, unknown> | undefined;
		const audit = receipts.filter(r => r.kind === "audit").at(-1) as Record<string, unknown> | undefined;
		const closeout = receipts.filter(r => r.kind === "closeout").at(-1) as Record<string, unknown> | undefined;
		const review = closeout ?? audit;
		const live = attempts.find(a => ["active", "audit_ready", "auditor_in_flight", "audited", "closeout_requested"].includes(a.state));
		return new Response(
			JSON.stringify({
				item: it,
				project: { project_id: "proj-1", workspace_id: WORKSPACE_ID, name: "The Bookends", health: "onTrack" },
				plan: plan ? { plan_name: "work-plan.md", plan_sha256: ((plan.payload as Record<string, unknown>)?.plan_sha256 as string) ?? "", at: plan.issued_at } : null,
				handoff: handoff ? { at: handoff.issued_at } : null,
				review: review ? { hash: String((review.payload_sha256 as string) ?? "").slice(0, 12), at: review.issued_at } : null,
				close_attempts: attempts,
				audit_manifest: live ? (manifests.find(m => m.attempt_id === live.attempt_id) ?? null) : null,
				auditor_launches: launches,
				close_attempt_events: closeEvents,
				checkpoint_deliveries: deliveries,
				relations: [],
				receipts,
				current_candidate: it.candidate,
			}),
			{ status: 200 },
		);
	}
	if (u.includes("/v1/work-items/")) {
		const key = decodeURIComponent(u.split("/v1/work-items/")[1] ?? "HOME-1");
		const it = items.get(key) ?? initialItem;
		return new Response(JSON.stringify(it), { status: 200 });
	}
	if (method === "POST" && u.endsWith("/v1/commands")) {
		commandPosts++;
		commandTypes.push(JSON.parse(init?.body ?? "{}")?.command?.type ?? "unknown");
		const env = JSON.parse(init?.body ?? "{}") as { command: { type: string; payload: Record<string, unknown> } };
		const cmdType = env.command?.type;
		const payload = env.command?.payload ?? {};
		if (cmdType === "create_work_batch") {
			const batchItems = (payload.items as Array<{ client_ref: string; title: string; description?: string }>) ?? [];
			const createdList: Array<{ client_ref: string; work_id: string; revision_id: string; key: string; state: string; row_version: number }> = [];
			for (const b of batchItems) {
				writes.created++;
				const created: MockWorkItem = {
					work_id: `id-${writes.created}`,
					workspace_id: WORKSPACE_ID,
					alias: { key: `HOME-${writes.created}` },
					revision: { revision_id: `rev-${writes.created}`, title: b.title, description: b.description ?? "", scope: "", acceptance_criteria: [] },
					state: "IN_PROGRESS",
					project_id: "proj-1",
					candidate: null,
				};
				items.set(created.alias.key, created);
				items.set(created.work_id, created);
				createdList.push({ client_ref: b.client_ref, work_id: created.work_id, revision_id: created.revision.revision_id, key: created.alias.key, state: created.state, row_version: 1 });
				if (writes.created === 1) Object.assign(issue, { id: created.work_id, identifier: created.alias.key, title: created.revision.title });
			}
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000010" },
					result: { type: "create_work_batch", items: createdList },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "set_focus") {
			writes.addNow++;
			nowSelected = true;
			nowId = ((payload.slot as Record<string, unknown>)?.work_id as string) ?? "id-1";
			slotVersion++;
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000011" },
					result: { type: "set_focus", workspace_id: WORKSPACE_ID, owner_id: OWNER_ID, work_id: nowId, version: slotVersion },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "clear_focus") {
			writes.removeNow++;
			nowSelected = false;
			nowId = null;
			slotVersion++;
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000012" },
					result: { type: "clear_focus", workspace_id: WORKSPACE_ID, owner_id: OWNER_ID, work_id: null, version: slotVersion },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "revise_work") {
			const rev = payload.revision as { work_id: string; revision_id: string; title?: string; description?: string };
			const it = items.get(rev.work_id) ?? items.get("HOME-1");
			if (it) {
				if (rev.title) it.revision.title = rev.title;
				if (rev.description !== undefined) it.revision.description = rev.description;
				it.revision.revision_id = rev.revision_id;
			}
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000015" },
					result: { type: "revise_work", revision_id: rev.revision_id, changed: true },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "set_work_state") {
			const workId = payload.work_id as string;
			const targetState = payload.state as string;
			const it = items.get(workId) ?? items.get("HOME-1");
			if (it) it.state = targetState;
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000016" },
					result: { type: "set_work_state", state: targetState },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "append_evidence") {
			const rec = payload.receipt as Record<string, unknown>;
			const kind = rec.kind as string;
			if (kind === "plan") {
				const planPayload = (rec.payload as Record<string, unknown>) ?? {};
				const appText = ((planPayload.approach as string[]) ?? []).map((s, i) => `${i + 1}. ${s}`).join("\n");
				const verText = ((planPayload.verification as string[]) ?? []).map((s, i) => `${i + 1}. ${s}`).join("\n");
				comments.push({
					body: `**Plan approved**\n- SHA-256: \`${planPayload.plan_sha256}\`\n\n## Approach\n${appText}\n\n## Verification\n${verText}`,
					createdAt: new Date().toISOString(),
				});
				const it = items.get(rec.work_id as string) ?? initialItem;
				it.candidate = { candidate_id: rec.candidate_id as string, candidate_sha256: "0".repeat(64) };
			} else if (kind === "closeout") {
				comments.push({ body: `**Session review**\n- Plan SHA-256: \`${(rec.payload as Record<string, unknown>)?.plan_sha256 ?? (rec.payload as Record<string, unknown>)?.body ?? ""}\``, createdAt: new Date().toISOString() });
			} else if (kind === "audit") {
				comments.push({ body: String((rec.payload as Record<string, unknown>)?.report ?? "VERDICT: PASS"), createdAt: new Date().toISOString() });
			}
			receipts.push(rec);
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000013" },
					result: { type: "append_evidence", receipt: rec },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "finalize_candidate") {
			const it = items.get("HOME-1") ?? initialItem;
			const finalCandId = (payload.candidate_id as string) ?? "cand-1";
			const plannedCandId = (payload.planned_candidate_id as string) ?? it.candidate?.candidate_id;
			const commitSha = (payload.commit_sha as string) ?? "commit-1";
			const finalCand = {
				candidate_id: finalCandId,
				work_id: it.work_id,
				revision_id: it.revision.revision_id,
				candidate_sha256: "0".repeat(64),
				commit_sha: commitSha,
				kind: "final" as const,
				allocated_at: new Date().toISOString(),
			};
			it.candidate = finalCand;
			initialItem.candidate = finalCand;
			for (const v of items.values()) v.candidate = finalCand;
			const planRec = receipts.filter(r => r.kind === "plan" && r.candidate_id === plannedCandId).at(-1);
			if (planRec) {
				receipts.push({
					...planRec,
					receipt_id: `rec-plan-${finalCandId}`,
					candidate_id: finalCandId,
					issued_at: new Date().toISOString(),
					candidate_sha256: finalCand.candidate_sha256,
					candidate_commit: commitSha,
				});
			}
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000014" },
					result: { type: "finalize_candidate", candidate: finalCand },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "create_same_session_child") {
			sscCalls.push(payload);
			const parentAttempt = attempts.find(a => a.attempt_id === payload.attempt_id);
			if (!parentAttempt) throw new Error("mock: create_same_session_child without live attempt");
			const inputItem = payload.item;
			const clientRef = inputItem && typeof inputItem === "object" && "client_ref" in inputItem ? String(inputItem.client_ref) : "c";
			const childItem = { client_ref: clientRef, work_id: "id-child-1", revision_id: "rev-child-1", key: "HOME-77", state: "BACKLOG", row_version: 1 };
			const receipt = {
				receipt_id: "rec-ssc-1",
				work_id: childItem.work_id,
				revision_id: childItem.revision_id,
				candidate_id: parentAttempt.candidate_id,
				kind: "same_session_found_fixed",
				payload: { attempt_id: parentAttempt.attempt_id, owner_session_id: payload.owner_session_id, base_commit: parentAttempt.owner_session_start_commit, fix_commit: parentAttempt.candidate_commit, candidate_sha256: parentAttempt.candidate_sha256, finding: payload.finding, verification: payload.verification },
				payload_sha256: "1".repeat(64),
				issuer: "service",
				issued_at: new Date().toISOString(),
				independent: false,
			};
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-ssc-${sscCalls.length}` }, result: { type: "create_same_session_child", item: childItem, receipt } }), { status: 200 });
		}
		if (cmdType === "begin_close_attempt") {
			beginCalls.push(payload);
			const it = items.get(payload.work_id as string) ?? initialItem;
			// OMP-127: the first begin of this mode is refused with no attempt —
			// the host must keep review writes locked until a begin applies.
			if ((mode === "summary-begin-refused" || mode === "summary-refusal-durable") && beginCalls.length === 1) {
				const event = mockEvent(it.work_id, null, "close_attempt_refused", "plan_receipt_missing", true);
				return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-begin-${eventSeq}` }, result: { type: "begin_close_attempt", status: "refused", attempt: null, event } }), { status: 200 });
			}
			if (!it.candidate || !("commit_sha" in it.candidate) || !(it.candidate as { commit_sha?: string }).commit_sha) {
				const event = mockEvent(it.work_id, null, "close_attempt_refused", "candidate_not_final", true);
				return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-begin-${eventSeq}` }, result: { type: "begin_close_attempt", status: "refused", attempt: null, event } }), { status: 200 });
			}
			const candidate = it.candidate as { candidate_id: string; candidate_sha256: string; commit_sha?: string };
			const live = attempts.find(a => ["active", "audit_ready", "auditor_in_flight", "audited", "closeout_requested"].includes(a.state));
			if (live && live.candidate_id === candidate.candidate_id) {
				const event = mockEvent(it.work_id, live.attempt_id, "attempt_resumed", "attempt_resumed", false);
				return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-begin-${eventSeq}` }, result: { type: "begin_close_attempt", status: "applied", attempt: live, event } }), { status: 200 });
			}
			for (const attempt of attempts) {
				if (["active", "audit_ready", "auditor_in_flight"].includes(attempt.state)) {
					attempt.state = "superseded";
					attempt.terminal_reason = "superseded_by_new_summary";
				}
			}
			const attempt: MockAttempt = {
				attempt_id: payload.attempt_id as string,
				work_id: it.work_id,
				revision_id: it.revision.revision_id,
				candidate_id: candidate.candidate_id,
				plan_receipt_id: "plan-receipt-1",
				candidate_sha256: candidate.candidate_sha256,
				candidate_commit: candidate.commit_sha ?? null,
				owner_session_id: payload.owner_session_id as string,
				owner_session_started_at: payload.owner_session_started_at as string,
				owner_session_start_commit: payload.owner_session_start_commit as string,
				repository: payload.repository as string,
				diff_sha256: payload.diff_sha256 as string,
				starting_dirty_paths: (payload.starting_dirty_paths as string[]) ?? [],
				authorization_kind: "summary",
				authorization_ref: payload.authorization_ref as string,
				launch_count: 0,
				cancelled_launch_count: 0,
				accepted_report_count: 0,
				in_flight_launch_id: null,
				state: "active",
				terminal_reason: null,
				requested_at: new Date().toISOString(),
				closeout_requested_at: null,
				completed_at: null,
				completion_authorization_ref: null,
			};
			attempts.push(attempt);
			const event = mockEvent(it.work_id, attempt.attempt_id, "attempt_begun", "attempt_begun", false);
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-begin-${eventSeq}` }, result: { type: "begin_close_attempt", status: "applied", attempt, event } }), { status: 200 });
		}
		if (cmdType === "seal_audit_manifest") {
			const attempt = attempts.find(a => a.attempt_id === payload.attempt_id);
			if (!attempt) throw new Error("seal without attempt");
			const taskBody = [
				"Approved plan",
				`Plan receipt SHA-256: ${"d".repeat(64)}`,
				"the exact stored plan body",
				"",
				"Acceptance criteria",
				"- AC-1: the focused check passes",
				"",
				"Starting state (commit + pre-existing dirty files)",
				`Start commit: ${attempt.owner_session_start_commit}`,
				"Pre-existing dirty files: (none)",
				"",
				"Final diff",
				"Mode: git-range-sha256",
				`Repository: ${attempt.repository}`,
				`Start commit: ${attempt.owner_session_start_commit}`,
				`Final commit: ${attempt.candidate_commit}`,
				`SHA-256: ${attempt.diff_sha256}`,
				"",
				"Verification",
				"bun test → pass",
			].join("\n");
			const manifest = {
				manifest_id: `man-${attempt.attempt_id}`,
				work_id: attempt.work_id,
				attempt_id: attempt.attempt_id,
				manifest_version: 1,
				plan_receipt_id: "plan-receipt-1",
				verification_receipt_id: payload.verification_receipt_id,
				candidate_id: attempt.candidate_id,
				candidate_sha256: attempt.candidate_sha256,
				candidate_commit: attempt.candidate_commit,
				task_body: taskBody,
				task_sha256: new Bun.CryptoHasher("sha256").update(taskBody, "utf8").digest("hex"),
				section_hashes: {},
				created_at: new Date().toISOString(),
			};
			manifests.push(manifest);
			attempt.state = "audit_ready";
			const event = mockEvent(attempt.work_id, attempt.attempt_id, "manifest_sealed", "manifest_sealed", false);
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-seal-${eventSeq}` }, result: { type: "seal_audit_manifest", status: "applied", attempt, manifest, event } }), { status: 200 });
		}
		if (cmdType === "reserve_auditor_launch") {
			const attempt = attempts.find(a => a.attempt_id === payload.attempt_id);
			const manifest = manifests.find(m => m.attempt_id === payload.attempt_id);
			if (!attempt || !manifest) throw new Error("reserve without seal");
			if (payload.task_sha256 !== manifest.task_sha256) {
				const event = mockEvent(attempt.work_id, attempt.attempt_id, "close_attempt_refused", "manifest_task_mismatch", true);
				return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-res-${eventSeq}` }, result: { type: "reserve_auditor_launch", status: "refused", attempt, event } }), { status: 200 });
			}
			if (attempt.state !== "audit_ready") {
				const event = mockEvent(attempt.work_id, attempt.attempt_id, "close_attempt_refused", "attempt_not_ready", true);
				return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-res-${eventSeq}` }, result: { type: "reserve_auditor_launch", status: "refused", attempt, event } }), { status: 200 });
			}
			attempt.launch_count += 1;
			attempt.state = "auditor_in_flight";
			const launch = { launch_id: `launch-${attempt.launch_count}`, attempt_id: attempt.attempt_id, manifest_id: manifest.manifest_id, launch_number: attempt.launch_count, task_sha256: payload.task_sha256, tool_call_id: payload.tool_call_id, reserved_at: new Date().toISOString() };
			attempt.in_flight_launch_id = launch.launch_id as string;
			launches.push(launch);
			const event = mockEvent(attempt.work_id, attempt.attempt_id, "auditor_launch_reserved", "auditor_launch_reserved", false);
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-res-${eventSeq}` }, result: { type: "reserve_auditor_launch", status: "applied", attempt, launch, event } }), { status: 200 });
		}
		if (cmdType === "cancel_auditor_launch") {
			cancelCalls.push(payload);
			const attempt = attempts.find(a => a.attempt_id === payload.attempt_id);
			if (!attempt || attempt.state !== "auditor_in_flight" || attempt.in_flight_launch_id !== payload.launch_id) throw new Error("cancel without in-flight launch");
			attempt.cancelled_launch_count += 1;
			attempt.in_flight_launch_id = null;
			attempt.state = "audit_ready";
			const launch = launches.find(row => row.launch_id === payload.launch_id);
			const event = mockEvent(attempt.work_id, attempt.attempt_id, "auditor_launch_cancelled", "host_launch_failed", true);
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-cancel-${eventSeq}` }, result: { type: "cancel_auditor_launch", status: "applied", attempt, launch, event } }), { status: 200 });
		}
		if (cmdType === "settle_auditor_launch") {
			settleCalls.push(payload);
			const attempt = attempts.find(a => a.attempt_id === payload.attempt_id);
			if (!attempt) throw new Error("settle without attempt");
			attempt.in_flight_launch_id = null;
			const transport = payload.transport_payload;
			const text = typeof transport === "string" ? transport : typeof (transport as Record<string, unknown>)?.report === "string" ? String((transport as Record<string, unknown>).report) : "";
			const verdictMatch = /^VERDICT\s*:\s*(PASS|NEEDS_FIX|BLOCKED)\b/.exec(text.trim());
			if (payload.transport_failed === true || !verdictMatch) {
				attempt.state = attempt.launch_count - attempt.cancelled_launch_count >= 3 ? "budget_exhausted" : "audit_ready";
				if (attempt.state === "budget_exhausted") attempt.terminal_reason = "auditor_budget_exhausted";
				const event = mockEvent(attempt.work_id, attempt.attempt_id, "auditor_launch_settled", payload.transport_failed === true ? "transport_failed" : "verdict_missing", true);
				return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-set-${eventSeq}` }, result: { type: "settle_auditor_launch", status: "refused", attempt, event } }), { status: 200 });
			}
			const verdict = verdictMatch[1];
			attempt.accepted_report_count += 1;
			attempt.state = verdict === "PASS" ? "audited" : verdict === "NEEDS_FIX" ? "remediation_required" : "blocked";
			if (verdict !== "PASS") attempt.terminal_reason = verdict === "NEEDS_FIX" ? "needs_fix" : "auditor_blocked";
			const actions = verdict === "PASS"
				? ["record the closeout review", "owner /done closes"]
				: verdict === "NEEDS_FIX"
					? ["fix the findings", "enter /summary again for a fresh attempt"]
					: ["resolve the blocker", "enter /summary again for a fresh attempt"];
			const event = mockEvent(attempt.work_id, attempt.attempt_id, "auditor_launch_settled", `verdict_${verdict.toLowerCase()}`, true, actions);
			const receipt = {
				receipt_id: `rec-audit-${eventSeq}`,
				work_id: attempt.work_id,
				revision_id: attempt.revision_id,
				candidate_id: attempt.candidate_id,
				kind: "audit",
				payload: { report: text.trim() },
				payload_sha256: "0".repeat(64),
				issuer: "work-service/auditor-settle",
				issued_at: new Date().toISOString(),
				candidate_sha256: attempt.candidate_sha256,
				candidate_commit: attempt.candidate_commit,
				verdict,
				independent: true,
			};
			receipts.push(receipt);
			comments.push({ body: text.trim(), createdAt: new Date().toISOString() });
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-set-${eventSeq}` }, result: { type: "settle_auditor_launch", status: "applied", attempt, receipt, verdict, event } }), { status: 200 });
		}
		if (cmdType === "attest_checkpoint_delivery") {
			attestCalls.push(payload);
			const target = closeEvents.find(e => e.event_id === payload.event_id);
			if (!target) throw new Error("attest without event");
			deliveries.push({ delivery_id: `del-${deliveries.length + 1}`, event_id: payload.event_id, delivery_sequence: deliveries.filter(d => d.event_id === payload.event_id).length + 1, owner_session_id: payload.owner_session_id, rendered_sha256: payload.rendered_sha256, status: payload.status, authorization_ref: payload.authorization_ref ?? null, created_at: new Date().toISOString() });
			attestWaiters.shift()?.();
			const event = mockEvent(target.work_id as string, (target.attempt_id as string) ?? null, "checkpoint_delivery_recorded", `delivery_${payload.status}`, false);
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-att-${eventSeq}` }, result: { type: "attest_checkpoint_delivery", status: "applied", delivery: deliveries.at(-1), event } }), { status: 200 });
		}
		if (cmdType === "record_closeout_review") {
			const attempt = attempts.find(a => a.attempt_id === payload.attempt_id);
			if (!attempt) throw new Error("record_closeout_review without attempt");
			const receipt = payload.receipt as Record<string, unknown>;
			receipts.push(receipt);
			comments.push({ body: `**Session review**\n- Plan SHA-256: \`${(receipt.payload as Record<string, unknown>)?.plan_sha256 ?? (receipt.payload as Record<string, unknown>)?.body ?? ""}\``, createdAt: new Date().toISOString() });
			attempt.state = "closeout_requested";
			attempt.closeout_requested_at = new Date().toISOString();
			const event = mockEvent(attempt.work_id, attempt.attempt_id, "closeout_review_recorded", "closeout_review_recorded", true);
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000015" },
					result: { type: "record_closeout_review", status: "applied", receipt, attempt, event },
				}),
				{ status: 200 },
			);
		}
		if (cmdType === "set_work_state") {
			const st = payload.state as string;
			if (st === "CANCELED") writes.canceled++;
			const it = items.get((payload.work_id as string) ?? "id-1");
			if (it) it.state = st;
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-state-${eventSeq}` }, result: { type: "set_work_state", work_id: payload.work_id, state: st, row_version: 2 } }), { status: 200 });
		}
		if (cmdType === "complete_work") {
			writes.closed++;
			const inp = payload.input as Record<string, unknown>;
			const it = items.get((inp?.work_id as string) ?? "id-1") ?? initialItem;
			it.state = "DONE";
			const attempt = attempts.find(a => a.attempt_id === payload.attempt_id);
			if (attempt) {
				attempt.state = "completed";
				attempt.completed_at = new Date().toISOString();
				attempt.completion_authorization_ref = (payload.done_authorization_ref as string) ?? null;
			}
			comments.push({ body: "**Owner verdict in session: done**", createdAt: new Date().toISOString() });
			const event = mockEvent(it.work_id, attempt?.attempt_id ?? null, "work_completed", "work_completed", false);
			const cancels = (payload.cancellations as Array<{ work_id: string; reason: string }>) ?? [];
			for (const c of cancels) {
				const cit = items.get(c.work_id);
				if (cit) cit.state = "CANCELED";
			}
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000016" },
					result: {
						type: "complete_work",
						status: "applied",
						work_id: it.work_id,
						state: "DONE",
						row_version: 2,
						completed_work_ids: (payload.satisfied_work_ids as string[]) ?? [],
						canceled_work_ids: cancels.map(c => c.work_id),
						event,
					},
				}),
				{ status: 200 },
			);
		}
		return new Response(
			JSON.stringify({
				receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000017" },
				result: { type: cmdType },
			}),
			{ status: 200 },
		);
	}
	return new Response(JSON.stringify({ error: { code: "not_found", diagnostics: [u] } }), { status: 404 });
}) as typeof fetch;

const repoRoot = path.resolve(import.meta.dir, "../../..");
const EXTENSION_FILES = ["session-system/extensions/work-now.ts"];
const loaded = await loadExtensions(EXTENSION_FILES.map(file => path.join(repoRoot, file)), probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(e => e.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("work-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool did not register");
const uiCalls: string[] = [];
let currentSessionId = "session-test";
const statuses: string[] = [];
const statusCalls: { key: string; text: string | null; placement: string }[] = [];
const sentUserMessages: string[] = [];
const sentMessages: Array<{ message: unknown; options: unknown }> = [];
const deliveredMessages: Array<{ customType?: string; content?: unknown; details?: unknown; display?: boolean }> = [];
let activeTools = ["read", "bash", "work"];
let throwNextSend = false;
let throwNextSendMessage = false;
let throwNextSetTools = false;
let abortCalls = 0;
const depth = mode === "summary-subagent" || mode === "ledger-reads-subagent" ? 1 : 0;
const inheritedNow =
	mode === "summary-subagent"
		? [{ type: "custom", customType: "work-now", data: { backend: "work", issueId: "id-1", identifier: "HOME-1", title: "First", setAt: Date.now() } }]
		: [];
const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
const gptModel = { id: "gpt-5.2", provider: "openai", name: "GPT 5.2", api: "openai-responses" };
const artifactsDir = path.join(probe, ".artifacts");
fs.mkdirSync(artifactsDir, { recursive: true });
const localProtocolOptions = {
	getArtifactsDir: () => artifactsDir,
	getSessionId: () => currentSessionId,
};
function writeIntakeBlueprint(name: string, content: string): string {
	const p = resolveLocalUrlToPath(`local://${name}`, localProtocolOptions);
	fs.mkdirSync(path.dirname(p), { recursive: true });
	fs.writeFileSync(p, content);
	return content;
}
const runner = new ExtensionRunner(
	loaded.extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch: () => inheritedNow, getSessionId: () => currentSessionId, getArtifactsDir: () => artifactsDir } as never,
	{ getAvailable: () => [fableModel, gptModel], hasProvider: () => true } as never,
	undefined,
	{ getModelRole: (role: string) => (role === "audit" ? "openai/gpt-5.2" : undefined), get: () => undefined, getStorage: () => undefined } as never,
	localProtocolOptions,
	undefined,
	depth,
);
runner.initialize(
	{
		appendEntry: () => {},
		getSessionId: () => currentSessionId,
		deliverMessage: async (payload: unknown) => {
			if (throwNextDeliverMessage) {
				throwNextDeliverMessage = false;
				throw new Error("delivery rejected");
			}
			deliveredMessages.push(payload as never);
		},
		setModel: async () => true,
		getThinkingLevel: () => "high",
		setThinkingLevel: () => {},
		sendMessage: (message: unknown, options?: unknown) => {
			if (throwNextSendMessage) {
				throwNextSendMessage = false;
				throw new Error("delivery rejected");
			}
			sentMessages.push({ message, options });
			if (message && typeof message === "object" && "customType" in message && message.customType === "center-readout") {
				deliveredMessages.push(message as never);
			}
		},
		sendUserMessage: (content: unknown) => {
			if (throwNextSend) {
				throwNextSend = false;
				throw new Error("injection refused");
			}
			sentUserMessages.push(typeof content === "string" ? content : JSON.stringify(content));
		},
		getActiveTools: () => [...activeTools],
		setActiveTools: async (names: string[]) => {
			if (throwNextSetTools && names.length === 0) {
				throwNextSetTools = false;
				throw new Error("registry refused");
			}
			activeTools = [...names];
		},
	} as never,
	{
		getModel: () => fableModel,
		isIdle: () => idle,
		abort: () => {
			abortCalls++;
		},
		hasPendingMessages: () => false,
		shutdown: () => {},
		getSystemPrompt: () => [],
	} as never,
	undefined,
	{
		theme: { fg: (_color: string, text: string) => text },
		setStatus: (key: string, text: string | undefined, options?: { placement?: string }) => {
			statuses.push(text ?? "");
			statusCalls.push({ key, text: text ?? null, placement: options?.placement ?? "footer" });
		},
		notify: (text: string) => uiCalls.push(`notify:${text}`),
		select: async (title: string) => {
			uiCalls.push(`select:${title}`);
			return undefined;
		},
		confirm: async (title: string, body?: string) => {
			uiCalls.push(`confirm:${title}${body ? `\n${body}` : ""}`);
			if (mode === "done-cancel-decline" && title.includes("This is your verdict")) return false;
			return true;
		},
	} as never,
);
// OMP-47: the process-global audit bridge is gone; owner lifecycle now resets
// the shared transcript ref (confirmation receipts) only at task depth 0.
const seedLifecycle = mode === "summary" || mode === "summary-subagent";
const transcriptBeforeStart = currentTranscriptRef();
await runner.emit({ type: "session_start" } as never);
const lifecycleAfterStart = seedLifecycle
	? { transcriptChanged: currentTranscriptRef() !== transcriptBeforeStart }
	: undefined;
const ctx = runner.createContext();

async function execute(params: Record<string, unknown>): Promise<string> {
	const result = await tool.definition.execute("t", params, undefined, undefined, ctx);
	return result.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
}

async function setNow(): Promise<void> {
	const { confirmed } = await confirmRoundTrip(execute, { action: "set_now", work: "HOME-1" });
	if (!confirmed.includes("NOW → HOME-1")) throw new Error(`set_now failed: ${confirmed}`);
}

// OMP-155: content the old summary rebuild dropped — a nested bullet, an
// extra section, and a multibyte character — must survive in the receipt.
const planA =
	"# Work\n\n## Approach\n1. Change the shared path\n   - nested detail the summary dropped\n\n## Verification\n1. Run the focused check\n\n## Assumptions & contingencies\n- café rollback stays reversible\n";
const planB = `${planA}2. Run the smoke path\n`;
async function approve(content: string): Promise<{ cancel: boolean; reason?: string }> {
	const result = await runner.emit({
		type: "plan_approved",
		planFilePath: "local://work-plan.md",
		planContent: content,
		title: "Work",
	} as never);
	return (result ?? { cancel: false }) as { cancel: boolean; reason?: string };
}

const intakeMessage = {
	type: "message_start",
	message: { role: "custom", customType: "skill-prompt", attribution: "user", details: { name: "intake", path: "/x/SKILL.md" }, content: "intake", timestamp: Date.now() },
};
const summaryMessage = {
	type: "message_start",
	message: { role: "custom", customType: "skill-prompt", attribution: "user", details: { name: "summary", path: "/x/SKILL.md" }, content: "summary", timestamp: Date.now() },
};
async function enterSummary(): Promise<void> {
	await runner.emitInput("/skill:summary", undefined, "interactive");
	await runner.emit(summaryMessage as never);
}
const pastedSummary = {
	type: "message_start",
	message: { role: "user", content: "[IMPORTANT: User invoked the summary skill]", timestamp: Date.now() },
};

const out: Record<string, unknown> = {};
if (mode === "intake") {
	await runner.emit(intakeMessage as never);
	const immediateAsk = await runner.emitToolCall({
		type: "tool_call",
		toolName: "ask",
		toolCallId: "ask-immediate",
		input: { questions: [{ id: "q1", question: "one?" }] },
	} as never);
	out.immediateAsk = immediateAsk;
	out.createBeforeScan = await execute({ action: "create_work", title: "Early", project: "The Bookends" });

	await runner.emit({
		type: "message_end",
		message: {
			role: "assistant",
			content: [
				{ type: "text", text: "## Figured out myself\n1\n## Asking you\n2\n## Leaving for later\n3" },
				{ type: "toolCall", id: "t-read", name: "read", arguments: {} },
			],
		},
	} as never);
	const askAfterCoEmitted = await runner.emitToolCall({
		type: "tool_call",
		toolName: "ask",
		toolCallId: "ask-coemitted",
		input: { questions: [{ id: "q1", question: "one?" }] },
	} as never);
	out.askAfterCoEmitted = askAfterCoEmitted;

	await runner.emit({
		type: "message_end",
		message: {
			role: "assistant",
			content: [
				{ type: "text", text: "## Asking you\n2\n## Figured out myself\n1\n## Leaving for later\n3" },
			],
		},
	} as never);
	const askAfterBadOrder = await runner.emitToolCall({
		type: "tool_call",
		toolName: "ask",
		toolCallId: "ask-badorder",
		input: { questions: [{ id: "q1", question: "one?" }] },
	} as never);
	out.askAfterBadOrder = askAfterBadOrder;

	await runner.emit({
		type: "message_end",
		message: {
			role: "assistant",
			content: [
				{ type: "text", text: "## Figured out myself\n- fact\n## Asking you\n- decision\n## Leaving for later\n- parked" },
			],
		},
	} as never);

	const askMulti = await runner.emitToolCall({
		type: "tool_call",
		toolName: "ask",
		toolCallId: "ask-multi",
		input: { questions: [{ id: "q1", question: "one?" }, { id: "q2", question: "two?" }] },
	} as never);
	out.askMulti = askMulti;

	const askValid = await runner.emitToolCall({
		type: "tool_call",
		toolName: "ask",
		toolCallId: "ask-valid",
		input: { questions: [{ id: "q1", question: "one?" }] },
	} as never);
	out.askValid = askValid;

	// 1. Missing blueprint refuses before preview
	out.missingBlueprint = await execute({ action: "create_work", title: "First", description: "one", project: "The Bookends" });

	// 2. Mismatch payload refuses
	const blueprint1 = "# First Complaint\n\nProblem: one\n";
	writeIntakeBlueprint("intake-first.md", blueprint1);
	out.mismatchPayload = await execute({ action: "create_work", title: "First", description: "different", project: "The Bookends" });

	// 3. Exact preview, artifact drift invalidates confirm, restore permits confirmation
	const firstPreviewRaw = await execute({ action: "create_work", title: "First", description: blueprint1, project: "The Bookends" });
	out.preview = firstPreviewRaw;
	const confirmIdMatch = /confirmation_id: (\S+)/.exec(firstPreviewRaw);
	const confirmId1 = confirmIdMatch ? confirmIdMatch[1] : "";
	writeIntakeBlueprint("intake-first.md", `${blueprint1}\nmutated\n`);
	out.confirmAfterDrift = await execute({ action: "create_work", title: "First", description: blueprint1, project: "The Bookends", confirm: true, confirmation_id: confirmId1 });
	writeIntakeBlueprint("intake-first.md", blueprint1);
	out.confirmed = await execute({ action: "create_work", title: "First", description: blueprint1, project: "The Bookends", confirm: true, confirmation_id: confirmId1 });

	// 4. OMP-166 multi-deliverable bundle refusal
	const omp166Blueprint = [
		"# Session workflow slimming — cut standing token burn and loadout brittleness",
		"",
		"## Problem",
		"",
		"Every session starts by paying for things it never uses. Session-start context in this repo runs ~60,000 tokens typical (28,000 floor, measured across 147 recorded sessions); this repo has spent $3,277 over 22,918 turns. Inside that opening load: ~120 skill descriptions of which only 22 were ever opened in 149 sessions, and ~290 phone/TV/research tool descriptions carried twice per session that were executed zero times in this repo. The task-observer gate forces a full 26.6KB read every session (126 so far, ~830k tokens). Around it, debris accumulates unbounded: 1.18GB session store with ~150 of 244 folders being test-run leftovers (71 empty), 4,190 unrotated log files (84MB), and two skill symlink trees (12 vs 27 links) pointing at one source that have diverged.",
		"",
		"## Solution",
		"",
		"One slimming pass over the session loadout, in four slices: (1) move the device/research tool catalogs (deepadb, android, maestro, notebooklm, tavily, firecrawl) out of the global config and into only the projects that use them; (2) globally uninstall skill packs that no project uses, reinstalling on demand when a matching task appears; (3) keep the task-observer hard gate but satisfy it with a ~2KB core digest, loading the full skill and references only when logging or reviewing; (4) clean the debris — purge test-run session folders, bound the log directory, and consolidate or mechanically sync the skill symlink trees. Review-only session; all changes execute under /plan in the execution lane.",
		"",
		"## Decisions",
		"",
		"- Device/research tool catalogs move from global `~/.claude.json` to per-project mounts (media-discovery keeps firecrawl; device work keeps deepadb/android/maestro). — Q1: where should the catalogs live?",
		"- Skill packs unused by any project are uninstalled globally, not just hidden per-repo; reinstall on demand. — Q2: how aggressively to prune? (Owner chose the stronger option over the per-repo recommendation.)",
		"- The task-observer first-tool gate stays mechanical but is satisfied by a ~2KB digest; full skill loads on demand. — Q3: change the gate mechanism?",
		"- Housekeeping debris (session store, logs, symlink trees) rides this work item as a fourth slice. — Q4: where does cleanup get filed?",
		"- Single work item with four slices as acceptance criteria; no parent/child split. — Q5: how should the ledger carry it?",
		"",
		"## Acceptance criteria",
		"",
		"- [ ] A fresh session in this repo lists no deepadb/android/maestro/notebooklm/tavily tools, and a fresh media-discovery session still reaches firecrawl. Probe at build (needs new sessions after config change): inspect the session's tool inventory both places. Current state probed 2026-08-27: all mounted globally via `~/.claude.json`; 0 executions in this repo across 22,918 turns; real use elsewhere confirmed.",
		"- [ ] The per-session skill catalog drops from ~120 entries to the used set plus deliberate keeps; packs unused by any project are uninstalled. Probe at build: count entries in a fresh session's skill list; verify removed packs absent from discovery roots. Current state probed 2026-08-27: ~120 loaded, 22 ever opened here.",
		"- [ ] The task-observer gate passes on a digest read of ≤ ~2KB; the full 26.6KB skill is no longer read at every session start; enforcement (block until read) still fires when the digest is skipped. Probe at build: start a fresh session, observe the gate accept the digest; check digest file size.",
		"- [ ] Session store test-debris folders are gone (244 project dirs → roughly the ~90 real ones; 0 empty), the log directory is bounded by rotation or cleanup (4,190 files today), and skill installation resolves to one canonical tree or a mechanically synced pair. Probe at build: recount dirs/files; resolve every symlink.",
		"",
		"## Out of scope",
		"",
		"- Splitting the 100KB workflow extension file (`host.ts`) into modules.",
		"- The overdue observation-log review (~60 open items) — summary-lane job.",
		"- Changing the standing persona modes (ponytail, caveman).",
		"- Executing any of the above in this session — review-only; execution goes through /plan.",
		"",
		"## Deferred & assumptions",
		"",
		"- [deferred design] Mechanical enforcement for repeated shell `grep`/`sed` (648 uses despite the built-in tool) and `sleep 30` polling (98 uses) — decide whether a hook is worth new machinery after the loadout cuts land.",
		"- [scheduled review] Observation-log review (60 OPEN, clusters on summary and task-observer) — runs in the summary lane, not here.",
		"- [assumption] Cache pricing keeps first-turn context the dominant per-session cost lever; medians treated as decision-grade.",
		"- Environmental figures (60k/28k first-turn tokens, 244 session dirs, 4,190 log files, 12/27 symlinks, ~120 skills) observed 2026-08-27 — re-probe at execution.",
		"",
		"## Coverage table",
		"",
		"| Category | Status |",
		"|---|---|",
		"| Functional scope & behavior | Resolved — four slices settled |",
		"| Domain & data model | Clear (N/A — no new records or entities) |",
		"| Interaction & UX flow | Resolved — gate behavior settled (Q3) |",
		"| Non-functional qualities | Resolved — token/size targets in criteria |",
		"| Integrations & external dependencies | Resolved — per-project MCP mounts (Q1) |",
		"| Edge cases & failure handling | Partial — reinstall-on-demand path exercised only at build (deferred) |",
		"| Constraints & tradeoffs | Resolved — global prune chosen over per-repo hide (Q2) |",
		"| Terminology | Clear (N/A — no new nouns minted) |",
		"| Completion signals | Resolved — probed acceptance criteria |",
		"| Placeholders & ambiguities in existing text | Clear (N/A — no source draft) |",
		"| Scope boundaries | Resolved — out-of-scope list |",
		"",
	].join("\n");
	writeIntakeBlueprint("intake-omp166.md", omp166Blueprint);
	out.omp166Refusal = await execute({ action: "create_work", title: "Bundled", description: omp166Blueprint, project: "The Bookends" });

	// 5. Unlinked batch child refusal & 6. Linked batch acceptance
	const batchBlueprint = "# Batch Parent\n\nParent description\n";
	writeIntakeBlueprint("intake-batch.md", batchBlueprint);
	out.unlinkedBatchRefusal = await execute({
		action: "create_work",
		title: "Batch Parent",
		description: batchBlueprint,
		project: "The Bookends",
		batch: [
			{ title: "Child 0", blocks: [1] },
			{ title: "Child 1" },
			{ title: "Child 2" },
		],
	});
	const linkedBatch = await confirmRoundTrip(execute, {
		action: "create_work",
		title: "Batch Parent",
		description: batchBlueprint,
		project: "The Bookends",
		batch: [
			{ title: "Child 0", blocks: [1] },
			{ title: "Child 1", blocks: [2] },
			{ title: "Child 2" },
		],
	});
	out.linkedBatchPreview = linkedBatch.preview;
	out.linkedBatchConfirmed = linkedBatch.confirmed;

	// 7. Second single-issue blueprint creates HOME-6 without replacing NOW
	const blueprint2 = "# Second Complaint\n\nProblem: two\n";
	writeIntakeBlueprint("intake-second.md", blueprint2);
	const second = await confirmRoundTrip(execute, { action: "create_work", title: "Second", description: blueprint2, project: "The Bookends" });
	out.second = second.confirmed;
	const stop = await extension.handlers.get("session_stop")?.[0]?.({ type: "session_stop", stop_hook_active: false }, ctx);
	out.stop = stop ?? null;
	out.writes = writes;
	out.nowSelected = nowSelected;

	const publishMessage = {
		type: "message_start",
		message: {
			role: "custom",
			customType: "skill-prompt",
			attribution: "user",
			details: { name: "intake", args: "--publish local://intake-published.md" },
			content: "intake --publish",
			timestamp: Date.now(),
		},
	};
	const blueprintPublish = "# Published Complaint\n\nProblem: published\n";
	writeIntakeBlueprint("intake-published.md", blueprintPublish);
	await runner.emit(publishMessage as never);
	const publishTrip = await confirmRoundTrip(execute, { action: "create_work", title: "Published", description: blueprintPublish, project: "The Bookends" });
	out.publishConfirmed = publishTrip.confirmed;
} else if (mode === "plan") {
	out.noNow = await runner.emitInput("/plan", undefined, "interactive");
	await setNow();
	out.first = await approve(planA);
	out.commentsAfterFirst = comments.length;
	out.submittedPlan = planA;
	out.firstReceiptBody = ((receipts.find(r => r.kind === "plan") as Record<string, unknown> | undefined)?.payload as Record<string, unknown> | undefined)?.body ?? null;
	out.firstGetWork = await execute({ action: "get_work", work: "HOME-1" });
	out.invalid = await approve("# Missing required sections\n");
	out.commentsAfterInvalid = comments.length;
	out.firstBody = comments[0]?.body;
	out.same = await approve(planA);
	out.commentsAfterSame = comments.length;
	out.changed = await approve(planB);
	out.commentsAfterChanged = comments.length;
	out.candidateAfterChanged = items.get("HOME-1")?.candidate?.candidate_id ?? null;
	// OMP-155: a VALID plan over the packet ceiling is refused before any write.
	out.oversized = await approve(`# Work\n\n## Approach\n1. ${"x".repeat(33 * 1024)}\n\n## Verification\n1. Run the focused check\n`);
	out.commentsAfterOversized = comments.length;
	out.planReceiptsAfterOversized = receipts.filter(r => r.kind === "plan").length;
	out.candidateAfterOversized = items.get("HOME-1")?.candidate?.candidate_id ?? null;
	out.hashA = Bun.SHA256.hash(planA, "hex");
	out.hashB = Bun.SHA256.hash(planB, "hex");
	const stopHandler = extension.handlers.get("session_stop")?.[0];
	out.stopFirst = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
	out.stopSecond = (await stopHandler?.({ type: "session_stop", stop_hook_active: true }, ctx)) ?? null;
	// HOME-147: ambient untyped notes no longer exist — a kindless call is refused with the kind menu.
	out.evidence = await execute({ action: "append_evidence", work: "HOME-1", body: "tests pass" });
	await runner.emit({ type: "turn_end" } as never);
	out.statusAfterEvidence = statuses.at(-1);
	out.handoff = await execute({ action: "append_evidence", work: "HOME-1", kind: "handoff", body: "done / none / resume" });
	await runner.emit({ type: "turn_end" } as never);
	out.statusAfterHandoff = statuses.at(-1);
	out.stopAfterHandoff = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
} else if (mode === "plan-now-change") {
	// OMP-124: NOW switched after /plan entry must move the approval target.
	await setNow(); // HOME-1
	out.planCapture = await runner.emitInput("/plan", undefined, "interactive"); // binds planTarget to HOME-1
	const switchTrip = await confirmRoundTrip(execute, { action: "set_now", work: "HOME-2" });
	out.switchConfirmed = switchTrip.confirmed;
	out.approved = await approve(planA);
	out.home1Candidate = items.get("id-1")?.candidate ?? null;
	out.home2Candidate = items.get("id-2")?.candidate ?? null;
	out.planReceiptTargets = receipts.filter(r => r.kind === "plan").map(r => String(r.work_id));
	// Sibling: /now clear (localClear) must also drop a captured plan target —
	// approval then falls back to the empty current NOW and refuses.
	out.planCapture2 = await runner.emitInput("/plan", undefined, "interactive"); // rebinds to HOME-2
	const nowCommand = extension.commands.get("now");
	if (!nowCommand) throw new Error("now command missing");
	await nowCommand.handler("clear", runner.createCommandContext());
	out.clearedApprove = await approve(planA);
	out.planReceiptTargetsAfterClear = receipts.filter(r => r.kind === "plan").map(r => String(r.work_id));
} else if (mode === "summary" || mode === "summary-subagent") {
	out.lifecycleAfterStart = lifecycleAfterStart;
	if (mode === "summary-subagent") {
		// OMP-43: a subagent session_switch must leave the shared transcript ref alone.
		const transcriptBeforeSwitch = currentTranscriptRef();
		await runner.emit({ type: "session_switch", reason: "resume" } as never);
		out.lifecycleAfterSwitch = { transcriptChanged: currentTranscriptRef() !== transcriptBeforeSwitch };
	}
	if (mode === "summary") await setNow(); // subagent mode inherits NOW from the branch
	if (mode === "summary") {
		await enterSummary();
		out.noPlanNotice = uiCalls.at(-1);
		out.noPlanReview = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "premature" });
	}
	await approve(planA);
	out.beforeInvocation = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "before" });
	await runner.emit(pastedSummary as never);
	out.afterPaste = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "pasted" });
	await enterSummary();
	out.afterStructured = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "review body" });
	out.reviewBodies = comments.filter(comment => comment.body.startsWith("**Session review**")).map(comment => comment.body);
	if (mode === "summary") {
		// OMP-43 depth-0 control: an OWNER session_switch still resets the shared ref.
		const transcriptBeforeSwitch = currentTranscriptRef();
		await runner.emit({ type: "session_switch", reason: "resume" } as never);
		out.ownerSwitchLifecycle = { transcriptChanged: currentTranscriptRef() !== transcriptBeforeSwitch };
		out.beginCalls = beginCalls.length;
	}
	out.uiCalls = uiCalls;
} else if (mode === "summary-begin-refused") {
	// OMP-127 fail-closed: a refused close-attempt begin keeps review writes
	// locked; a second literal /summary in the same session recovers.
	await setNow();
	await approve(planA);
	await enterSummary();
	out.beginCallsAfterFirst = beginCalls.length;
	out.firstVerify = await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "bun test → pass" });
	out.verificationReceiptsAfterFirst = receipts.filter(receipt => receipt.kind === "verification").length;
	await enterSummary();
	out.beginCallsAfterSecond = beginCalls.length;
	out.secondVerify = await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "bun test → pass" });
} else if (mode === "summary-refusal-durable") {
	// OMP-137: a refused begin persists its COMPLETE typed reason; a fresh
	// session surfaces it as one pending notice; a successful retry clears it.
	// session-ledger also registers before_agent_start; drain EVERY handler and
	// keep the first returned message (the workflow notice/digest injection).
	const beforeAgentHandlers = extension.handlers.get("before_agent_start") ?? [];
	const drain = async () => {
		for (const handler of beforeAgentHandlers) {
			const result = (await handler({ type: "before_agent_start" } as never, ctx)) as { message?: { content?: string } } | undefined;
			if (result?.message?.content) return result.message.content;
		}
		return "";
	};
	await setNow();
	await approve(planA);
	await enterSummary(); // begin #1 → typed service refusal
	out.beginCallsAfterFirst = beginCalls.length;
	await runner.emit({ type: "session_switch", reason: "new" } as never);
	currentSessionId = "session-2";
	await runner.emit({ type: "session_start" } as never);
	out.noticeAfterRestart = await drain();
	await enterSummary(); // begin #2 applies — the refusal is resolved
	out.beginCallsAfterRetry = beginCalls.length;
	await runner.emit({ type: "session_switch", reason: "new" } as never);
	currentSessionId = "session-3";
	await runner.emit({ type: "session_start" } as never);
	out.noticeAfterRetry = await drain();
} else if (mode === "summary-rider-refusal-durable") {
	// OMP-149: a host-side staged-rider validation refusal persists its
	// specific validator reason; removing the batch permits a retry to clear it.
	const beforeAgentHandlers = extension.handlers.get("before_agent_start") ?? [];
	const drain = async () => {
		for (const handler of beforeAgentHandlers) {
			const result = (await handler({ type: "before_agent_start" } as never, ctx)) as { message?: { content?: string } } | undefined;
			if (result?.message?.content) return result.message.content;
		}
		return "";
	};
	await setNow();
	await approve(planA);
	const batchPath = riderBatchPath(getAgentDir(), process.cwd());
	fs.mkdirSync(path.dirname(batchPath), { recursive: true });
	fs.writeFileSync(batchPath, JSON.stringify([{ key: "HOME-2", evidence: "staged rider regression" }]));
	fs.chmodSync(batchPath, 0o644);
	await enterSummary(); // host validation refuses before begin
	out.beginCallsAfterFirst = beginCalls.length;
	await runner.emit({ type: "session_switch", reason: "new" } as never);
	currentSessionId = "session-2";
	await runner.emit({ type: "session_start" } as never);
	out.noticeAfterRestart = await drain();
	fs.rmSync(batchPath);
	await enterSummary(); // clean retry begins and applies
	out.beginCallsAfterRetry = beginCalls.length;
	await runner.emit({ type: "session_switch", reason: "new" } as never);
	currentSessionId = "session-3";
	await runner.emit({ type: "session_start" } as never);
	out.noticeAfterRetry = await drain();
} else if (mode === "stop-continuation-states") {
	// OMP-134: the closeout continuation is emitted ONLY from live ledger state —
	// audited fires exactly once with the service event; every other state and an
	// unreadable workflow emit nothing and stay retryable.
	await setNow();
	await approve(planA);
	await enterSummary(); // begin applies → attempt active, review obligation armed
	const stopHandler = extension.handlers.get("session_stop")?.[0];
	const stop = async () => (await stopHandler?.({ type: "session_stop", stop_hook_active: false } as never, ctx)) ?? null;
	const attempt = attempts[0];
	if (!attempt) throw new Error("no attempt after summary");
	out.stopWhileActive = await stop();
	attempt.state = "audit_ready";
	out.stopWhileAuditReady = await stop();
	attempt.state = "auditor_in_flight";
	out.stopWhileInFlight = await stop();
	attempt.state = "audited";
	mockEvent("id-1", attempt.attempt_id, "auditor_launch_settled", "verdict_pass", false, ["enter /summary to resume close review"]);
	throwNextWorkflowRead = true;
	out.stopWhileUnreadable = await stop();
	out.stopWhenAudited = await stop();
	out.stopAfterFired = await stop();
} else if (mode === "atomic-child") {
	// OMP-139: the atomic same-session filing — unsupported authority fields
	// refuse BEFORE any preview; one confirmed filing emits exactly one command.
	await setNow();
	await approve(planA);
	await enterSummary(); // live attempt so the filing has a binding target
	const body = "## Finding\nbug found in-session\n\n## Verification\nfix proven in-session";
	const base = { action: "create_work", work: "HOME-1", kind: "same_session_found_fixed", title: "atomic child", body };
	const postsBefore = commandPosts;
	const uiBefore = uiCalls.length;
	out.rejectBatch = await execute({ ...base, batch: [{ title: "extra" }] });
	out.rejectQueue = await execute({ ...base, queue: true });
	out.rejectQuestion = await execute({ ...base, question: "why?" });
	out.rejectProject = await execute({ ...base, project: "The Bookends" });
	out.rejectKind = await execute({ ...base, kind: "handoff" });
	out.rejectNoWork = await execute({ action: "create_work", kind: "same_session_found_fixed", title: "atomic child", body });
	out.rejectNoBody = await execute({ action: "create_work", work: "HOME-1", kind: "same_session_found_fixed", title: "atomic child" });
	out.rejectHalfSections = await execute({ ...base, body: "## Finding\nonly finding" });
	out.postsDuringRejections = commandPosts - postsBefore;
	out.rejectionCommandTypes = commandTypes.slice(postsBefore);
	out.sscCallsAfterRejections = sscCalls.length;
	out.confirmUiDuringRejections = uiCalls.slice(uiBefore).filter(call => call.startsWith("confirm:")).length;
	const round = await confirmRoundTrip(execute, base);
	out.preview = round.preview;
	out.confirmed = round.confirmed;
	out.sscCallsAfterConfirm = sscCalls.length;
	out.sscPayload = sscCalls[0] ?? null;
} else if (mode === "restore") {
	out.now = await execute({ action: "my_now" });
} else if (mode === "now-canceled") {
	// Restore: session start reconciled against a slot pointing at canceled work.
	out.now = await execute({ action: "my_now" });
	// set_now on a canceled key must refuse before any owner prompt or focus write.
	out.refusal = await execute({ action: "set_now", work: "HOME-2" });
	// The literal keyed /now command has no pre-gate — it must hit the shared
	// setNow guard and surface the refusal through its own catch.
	const nowCommand = extension.commands.get("now");
	if (!nowCommand) throw new Error("now command missing");
	await nowCommand.handler("HOME-2", ctx);
	out.nowCommandNotices = uiCalls.filter(call => call.startsWith("notify:/now failed")).join(" | ");
	out.addNowWrites = writes.addNow;
} else if (mode === "omp140-audit-states") {
	await setNow();
	out.noAttemptGetWork = await execute({ action: "get_work", work: "HOME-1" });
	await approve(planA);
	await enterSummary();
	out.activeGetWork = await execute({ action: "get_work", work: "HOME-1" });
	await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "bun test → pass" });
	out.auditReadyGetWork = await execute({ action: "get_work", work: "HOME-1" });
	const attempt = attempts[0]!;
	const manifest = manifests[0]!;
	await globalThis.fetch("http://127.0.0.1:54322/v1/commands", {
		method: "POST",
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE_ID,
			operation_id: "00000000-0000-7000-8000-000000000010",
			request_id: "00000000-0000-7000-8000-000000000011",
			correlation_id: "00000000-0000-7000-8000-000000000012",
			command: {
				type: "reserve_auditor_launch",
				payload: { attempt_id: attempt.attempt_id, task_sha256: manifest.task_sha256, tool_call_id: "call-1" },
			},
		}),
	});
	await globalThis.fetch("http://127.0.0.1:54322/v1/commands", {
		method: "POST",
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE_ID,
			operation_id: "00000000-0000-7000-8000-000000000013",
			request_id: "00000000-0000-7000-8000-000000000014",
			correlation_id: "00000000-0000-7000-8000-000000000015",
			command: {
				type: "settle_auditor_launch",
				payload: { attempt_id: attempt.attempt_id, launch_id: "launch-1", transport_payload: "VERDICT: PASS\nFINDINGS\nnone\nACCEPTANCE COVERAGE\nall\nOUT OF SCOPE\nnone\nCHECKS RUN\nall\nREMAINING QUESTIONS\nnone" },
			},
		}),
	});
	out.auditedGetWork = await execute({ action: "get_work", work: "HOME-1" });
	// OMP-152: the direct settle above minted an undelivered requires_delivery
	// event; the closeout preflight queues it and refuses once, then the
	// retried identical write lands — no restart, no waiver.
	const settleAttested = nextAttestation();
	await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "review body" });
	await settleAttested;
	await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "review body" });
	out.closeoutRequestedGetWork = await execute({ action: "get_work", work: "HOME-1" });
} else if (mode === "omp140-restart-flow") {
	await setNow();
	await approve(planA);
	await enterSummary();
	await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "bun test → pass" });
	const attempt = attempts[0]!;
	const manifest = manifests[0]!;
	await globalThis.fetch("http://127.0.0.1:54322/v1/commands", {
		method: "POST",
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE_ID,
			operation_id: "00000000-0000-7000-8000-000000000010",
			request_id: "00000000-0000-7000-8000-000000000011",
			correlation_id: "00000000-0000-7000-8000-000000000012",
			command: {
				type: "reserve_auditor_launch",
				payload: { attempt_id: attempt.attempt_id, task_sha256: manifest.task_sha256, tool_call_id: "call-1" },
			},
		}),
	});
	await globalThis.fetch("http://127.0.0.1:54322/v1/commands", {
		method: "POST",
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE_ID,
			operation_id: "00000000-0000-7000-8000-000000000013",
			request_id: "00000000-0000-7000-8000-000000000014",
			correlation_id: "00000000-0000-7000-8000-000000000015",
			command: {
				type: "settle_auditor_launch",
				payload: { attempt_id: attempt.attempt_id, launch_id: "launch-1", transport_payload: "VERDICT: PASS\nFINDINGS\nnone\nACCEPTANCE COVERAGE\nall\nOUT OF SCOPE\nnone\nCHECKS RUN\nall\nREMAINING QUESTIONS\nnone" },
			},
		}),
	});
	// Emulate fresh session 2 (attempt is audited)
	await runner.emit({ type: "session_switch", reason: "new" } as never);
	currentSessionId = "session-2";
	await runner.emit({ type: "session_start" } as never);
	const testBackend = createWorkBackend({ baseUrl: "http://127.0.0.1:54322", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID }, () => "test-token");
	out.session2Extras = await testBackend.digestExtras();
	out.session2Center = await testBackend.centerSnapshot();
	await enterSummary();
	out.beginCallsAfterResume = beginCalls.length;
	out.session2Review = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "session 2 review" });
	out.attemptStateAfterReview = attempts[0]?.state;
	// Emulate fresh session 3 (attempt is closeout_requested)
	await runner.emit({ type: "session_switch", reason: "new" } as never);
	currentSessionId = "session-3";
	await runner.emit({ type: "session_start" } as never);
	out.session3Extras = await testBackend.digestExtras();
	out.session3Center = await testBackend.centerSnapshot();
	const done = extension.commands.get("done");
	if (!done) throw new Error("done command missing");
	await done.handler("", runner.createCommandContext());
	out.doneState = items.get("id-1")?.state;
} else if (mode === "omp140-failed-checkpoint") {
	await setNow();
	await approve(planA);
	await enterSummary();
	closeEvents.push({
		event_id: "event-failed-del",
		workspace_id: WORKSPACE_ID,
		work_id: "id-1",
		attempt_id: "att-1",
		event_type: "closeout_review_recorded",
		reason_code: "closeout_review_recorded",
		reason: "review recorded",
		legal_next_actions: ["owner /done closes"],
		remaining_launches: 3,
		remaining_reports: 2,
		requires_fresh_authorization: false,
		rendered_text: "failed event",
		rendered_sha256: "0".repeat(64),
		requires_delivery: true,
		created_at: new Date().toISOString(),
	});
	const testBackend = createWorkBackend({ baseUrl: "http://127.0.0.1:54322", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID }, () => "test-token");
	out.extrasWithPending = await testBackend.digestExtras();
} else if (mode === "omp140-terminal-guidance") {
	await setNow();
	attempts.length = 0;
	closeEvents.length = 0;
	const remediationAttempt: MockAttempt = {
		attempt_id: "att-remediation",
		work_id: "id-1",
		revision_id: "rev-1",
		candidate_id: "cand-1",
		plan_receipt_id: "plan-1",
		candidate_sha256: "0".repeat(64),
		candidate_commit: "0".repeat(40),
		owner_session_id: "s1",
		owner_session_started_at: new Date().toISOString(),
		owner_session_start_commit: "0".repeat(40),
		repository: "/repo",
		diff_sha256: "0".repeat(64),
		starting_dirty_paths: [],
		authorization_kind: "summary",
		authorization_ref: "summary:auth-r",
		launch_count: 1,
		cancelled_launch_count: 0,
		accepted_report_count: 1,
		in_flight_launch_id: null,
		state: "remediation_required",
		terminal_reason: "needs_fix",
		requested_at: new Date().toISOString(),
		closeout_requested_at: null,
		completed_at: null,
		completion_authorization_ref: null,
	};
	attempts.push(remediationAttempt);
	mockEvent("id-1", "att-remediation", "auditor_launch_settled", "verdict_needs_fix", false, ["fix the findings", "enter /summary again for a fresh attempt"]);
	const testBackend = createWorkBackend({ baseUrl: "http://127.0.0.1:54322", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID }, () => "test-token");
	out.terminalExtras = await testBackend.digestExtras();
	// Test budget_exhausted attempt with close_attempt_refused reason_code="budget_exhausted"
	attempts.length = 0;
	closeEvents.length = 0;
	const budgetAttempt: MockAttempt = {
		attempt_id: "att-budget",
		work_id: "id-1",
		revision_id: "rev-1",
		candidate_id: "cand-1",
		plan_receipt_id: "plan-1",
		candidate_sha256: "0".repeat(64),
		candidate_commit: "0".repeat(40),
		owner_session_id: "s1",
		owner_session_started_at: new Date().toISOString(),
		owner_session_start_commit: "0".repeat(40),
		repository: "/repo",
		diff_sha256: "0".repeat(64),
		starting_dirty_paths: [],
		authorization_kind: "summary",
		authorization_ref: "summary:auth-b",
		launch_count: 3,
		cancelled_launch_count: 0,
		accepted_report_count: 0,
		in_flight_launch_id: null,
		state: "budget_exhausted",
		terminal_reason: "auditor_budget_exhausted",
		requested_at: new Date().toISOString(),
		closeout_requested_at: null,
		completed_at: null,
		completion_authorization_ref: null,
	};
	attempts.push(budgetAttempt);
	closeEvents.push({
		event_id: "event-budget-refusal",
		workspace_id: WORKSPACE_ID,
		work_id: "id-1",
		attempt_id: "att-budget",
		event_type: "close_attempt_refused",
		reason_code: "budget_exhausted",
		reason: "the auditor budget for this attempt is exhausted",
		legal_next_actions: ["enter /summary again for a fresh bounded attempt"],
		remaining_launches: 0,
		remaining_reports: 2,
		requires_fresh_authorization: true,
		rendered_text: "budget exhausted text",
		rendered_sha256: "0".repeat(64),
		requires_delivery: true,
		created_at: new Date().toISOString(),
	});
	out.budgetExtras = await testBackend.digestExtras();
} else if (mode === "center" || mode === "center-scoped") {
	const center = extension.commands.get("center");
	if (!center) throw new Error("center command missing");
	const cmdCtx = runner.createCommandContext();
	const toolsBefore = [...activeTools];
	const postsBefore = commandPosts;

	if (mode === "center") {
		// (a) Read failure: tree read fails — tools untouched, one plain error.
		throwNextTree = true;
		await center.handler("", cmdCtx);
		out.readFailNotice = uiCalls.at(-1);
		out.toolsAfterReadFail = [...activeTools];

		// (b) Delivery failure: deliverMessage throws — tools untouched, one plain error.
		throwNextDeliverMessage = true;
		await center.handler("", cmdCtx);
		out.deliverFailNotice = uiCalls.at(-1);
		out.toolsAfterDeliverFail = [...activeTools];

		// (c) Session switch during read: drops delivery and clears overlap guard.
		switchSessionOnNextTree = true;
		const deliveredBeforeSwitch = deliveredMessages.length;
		await center.handler("", cmdCtx);
		out.deliveredDuringSwitch = deliveredMessages.length - deliveredBeforeSwitch;

		// (d) Steer race / busy guard
		busyDuringSnapshot = true;
		await center.handler("", cmdCtx);
		out.busyNotice = uiCalls.at(-1);
		idle = true;

		// (d) Unscoped center run (NOW unset)
		await center.handler("", cmdCtx);
		out.deliveredUnscoped = deliveredMessages.at(-1);
		out.toolsAfterCenter = [...activeTools];
		out.posts = commandPosts - postsBefore;
		out.prompts = sentUserMessages.length;

		// Second run: NOW set (HOME-1), plan not approved
		await setNow();
		await center.handler("", cmdCtx);
		out.deliveredWithNowNoPlan = deliveredMessages.at(-1);

		// Third run: Plan approved
		await approve(planA);
		await center.handler("", cmdCtx);
		out.deliveredWithPlan = deliveredMessages.at(-1);

		// Fourth run: Handoff appended
		await execute({ action: "append_evidence", work: "HOME-1", kind: "handoff", body: "done / none / resume" });
		await center.handler("", cmdCtx);
		out.deliveredWithHandoff = deliveredMessages.at(-1);
	} else {
		// Scoped run (projectFilter = "The Bookends")
		await setNow();
		const postsBeforeCenter = commandPosts;
		await center.handler("", cmdCtx);
		out.posts = commandPosts - postsBeforeCenter;
		out.deliveredScoped = deliveredMessages.at(-1);
		out.activityCalls = activityCalls;
		out.prompts = sentUserMessages.length;
		out.tools = [...activeTools];
	}
	out.toolsBefore = toolsBefore;
} else if (mode === "center-stale") {
	const center = extension.commands.get("center");
	if (!center) throw new Error("center command missing");
	await center.handler("", runner.createCommandContext());
	out.staleNotice = uiCalls.filter(call => call.includes("/center failed")).at(-1) ?? null;
	out.deliveredCount = deliveredMessages.length;
	out.prompts = sentUserMessages.length;
	out.tools = [...activeTools];
} else if (mode === "triage-questions") {
	out.createNoQuestion = await execute({ action: "create_work", title: "Needs decision", queue: true, project: "The Bookends" });
	out.createMultiline = await execute({ action: "create_work", title: "Needs decision", queue: true, question: "line 1\nline 2", project: "The Bookends" });
	const createValid = await confirmRoundTrip(execute, { action: "create_work", title: "Decision item", queue: true, question: "Should we proceed with option A?", project: "The Bookends" });
	out.createPreview = createValid.preview;
	out.createConfirmed = createValid.confirmed;
	out.createdDescription = items.get("HOME-1")?.revision.description;

	out.queueNoQuestion = await execute({ action: "queue_work", work: "HOME-1" });
	out.queueMultiline = await execute({ action: "queue_work", work: "HOME-1", question: "line 1\nline 2" });
	const queueValid = await confirmRoundTrip(execute, { action: "queue_work", work: "HOME-1", question: "Updated decision question?" });
	out.queuePreview = queueValid.preview;
	out.queueConfirmed = queueValid.confirmed;
	out.queuedDescription = items.get("HOME-1")?.revision.description;

	out.waitingOutput = await execute({ action: "waiting" });
} else if (mode === "descriptions") {
	out.getWork = await execute({ action: "get_work", work: "HOME-1" });
	const description = `${"x".repeat(401)} PREVIEW_SENTINEL`;
	out.create = await confirmRoundTrip(execute, { action: "create_work", title: "Long description", description, project: "The Bookends" });
	out.batch = await confirmRoundTrip(execute, {
		action: "create_work",
		title: "Long batch parent",
		description,
		project: "The Bookends",
		batch: [{ title: "Long batch child", description: `${"x".repeat(201)} CHILD_SENTINEL` }],
	});
	out.revise = await confirmRoundTrip(execute, { action: "revise_work", work: "HOME-1", description });
} else if (mode === "footer") {
	out.initialCalls = [...statusCalls];
	await setNow();
	out.callsAfterSetNow = [...statusCalls];
} else if (mode === "audit") {
	await setNow();
	await approve(planA);
	const REPORT = [
		"VERDICT: PASS",
		"",
		"FINDINGS",
		"(none)",
		"",
		"ACCEPTANCE COVERAGE",
		"| AC-1 | met | tests |",
		"",
		"OUT OF SCOPE",
		"none",
		"",
		"CHECKS RUN",
		"bun test → pass",
		"",
		"REMAINING QUESTIONS",
		"none",
	].join("\n");
	// Pre-summary close-ritual writes are refused.
	out.unauthorized = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: REPORT });
	await runner.emitInput("/unrelated", undefined, "interactive");
	out.beginCallsAfterRewrite = beginCalls.length;
	for (const nearMiss of ["/skill:summary-report", "/skill:summary anything", "/summary pasted prose", "/summary\npasted"]) {
		await runner.emitInput(nearMiss, undefined, "interactive");
	}
	out.beginCallsAfterNearMiss = beginCalls.length;
	// Production /skill:summary authorizes on trusted raw owner input before
	// its structured prompt starts.
	await runner.emitInput("/skill:summary", undefined, "interactive");
	out.beginCallsBeforeSummaryMessage = beginCalls.length;
	await runner.emit(summaryMessage as never);
	out.beginCalls = beginCalls.length;
	out.beginSession = beginCalls[0] ? { hasStartCommit: typeof beginCalls[0].owner_session_start_commit === "string", hasDiffSha: typeof beginCalls[0].diff_sha256 === "string", hasAuthorization: String(beginCalls[0].authorization_ref ?? "").startsWith("summary:") } : null;
	// Verification append seals the manifest.
	out.verify = await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "bun test → pass" });
	const getWork = await execute({ action: "get_work", work: "HOME-1" });
	out.getWork = getWork;
	out.nextActionInGetWork = getWork.includes('NEXT REQUIRED ACTION: work action:"run_audit", work:"HOME-1"');
	out.getWorkStartsWithStatus = getWork.startsWith("STATUS: CLOSE ATTEMPT audit_ready");
	out.nextActionCount = (getWork.match(/NEXT REQUIRED ACTION:/g) || []).length;
	out.noSealedTaskBytes = !getWork.includes("----- SEALED AUDITOR TASK BEGIN -----");

	// Execute a refused action while the attempt is live to verify refusal finalizer banner
	out.refusedWhileAuditReady = await execute({ action: "append_evidence", work: "HOME-1" });
	const attempt = attempts[0]!;
	const manifest = manifests[0]!;
	await globalThis.fetch("http://127.0.0.1:54322/v1/commands", {
		method: "POST",
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE_ID,
			operation_id: "00000000-0000-7000-8000-000000000010",
			request_id: "00000000-0000-7000-8000-000000000011",
			correlation_id: "00000000-0000-7000-8000-000000000012",
			command: {
				type: "reserve_auditor_launch",
				payload: { attempt_id: attempt.attempt_id, task_sha256: manifest.task_sha256, tool_call_id: "call-1" },
			},
		}),
	});
	await globalThis.fetch("http://127.0.0.1:54322/v1/commands", {
		method: "POST",
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE_ID,
			operation_id: "00000000-0000-7000-8000-000000000013",
			request_id: "00000000-0000-7000-8000-000000000014",
			correlation_id: "00000000-0000-7000-8000-000000000015",
			command: {
				type: "settle_auditor_launch",
				payload: { attempt_id: attempt.attempt_id, launch_id: "launch-1", transport_payload: REPORT },
			},
		}),
	});
	out.settlePayload = settleCalls[0]?.transport_payload ?? null;
	out.attemptState = attempts.at(-1)?.state ?? null;
	const auditReceipt = receipts.filter(r => r.kind === "audit").at(-1);
	out.auditIssuer = auditReceipt?.issuer ?? null;
	out.auditVerdict = auditReceipt?.verdict ?? null;
	const settleAttested = nextAttestation();
	await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "review body" });
	await settleAttested;
	out.attestCalls = attestCalls.length;
	out.attestStatus = attestCalls[0]?.status ?? null;
} else if (mode === "summary-push-fail") {
	await setNow();
	await approve(planA);
	fs.writeFileSync(path.join(probe, "work.txt"), "candidate work\n");
	const origin = Bun.spawnSync(["git", "remote", "get-url", "origin"], { cwd: probe }).stdout.toString().trim();
	Bun.spawnSync(["git", "remote", "remove", "origin"], { cwd: probe });
	await enterSummary();
	const frozen = (items.get("HOME-1") ?? initialItem).candidate;
	out.beginAfterPushFailure = beginCalls.length;
	out.frozenAfterPushFailure = frozen?.kind ?? null;
	out.pushReceiptsAfterFailure = receipts.filter(receipt => receipt.kind === "push").length;
	out.failureNotice = uiCalls.at(-1) ?? null;

	Bun.spawnSync(["git", "remote", "add", "origin", origin], { cwd: probe });
	await enterSummary();
	const recovered = (items.get("HOME-1") ?? initialItem).candidate;
	const branch = Bun.spawnSync(["git", "rev-parse", "--abbrev-ref", "HEAD"], { cwd: probe }).stdout.toString().trim();
	const remoteCommit = Bun.spawnSync(["git", "ls-remote", "origin", `refs/heads/${branch}`], { cwd: probe }).stdout.toString().trim().split(/\s+/)[0];
	out.beginAfterPushRetry = beginCalls.length;
	out.pushReceiptsAfterRetry = receipts.filter(receipt => receipt.kind === "push").length;
	out.candidateCommit = recovered?.commit_sha ?? null;
	out.remoteCommit = remoteCommit || null;
} else if (mode === "summary-reauth") {
	await setNow();
	await approve(planA);
	// A staged index entry makes the candidate freeze REFUSE before any
	// attempt begins — the state that used to deadlock every later /summary.
	fs.writeFileSync(path.join(probe, "work.txt"), "candidate work\n");
	Bun.spawnSync(["git", "add", "work.txt"], { cwd: probe });
	await enterSummary();
	out.beginAfterRefused = beginCalls.length;
	// Owner remediates; another literal /skill:summary must recover without a
	// session restart.
	Bun.spawnSync(["git", "reset", "-q"], { cwd: probe });
	await enterSummary();
	out.beginAfterSkillRetry = beginCalls.length;
	// Unrelated owner messages never authorize anything.
	await runner.emit({
		type: "message_start",
		message: { role: "custom", customType: "checkpoint", attribution: "system", content: "interleaved", timestamp: Date.now() },
	} as never);
	out.beginAfterUnrelated = beginCalls.length;
	// The raw channel also re-authorizes: one more begin, and the service
	// supersedes to keep exactly one live attempt.
	await runner.emitInput("/summary", undefined, "interactive");
	out.beginAfterRaw = beginCalls.length;
} else if (mode === "summary-stale-final") {
	await setNow();
	await approve(planA);
	fs.writeFileSync(path.join(probe, "work.txt"), "candidate work\n");
	await enterSummary();
	const initialFrozen = (items.get("HOME-1") ?? initialItem).candidate;
	const commitA = initialFrozen?.commit_sha;
	const beginCallsBeforeDrift = beginCalls.length;
	const pushReceiptsBeforeDrift = receipts.filter(receipt => receipt.kind === "push").length;

	// Simulate commit B (e.g. manual amend or separate commit that moves HEAD away from candidate commit A)
	fs.writeFileSync(path.join(probe, "extra.txt"), "drifted commit\n");
	Bun.spawnSync(["git", "add", "extra.txt"], { cwd: probe });
	Bun.spawnSync(["git", "commit", "-q", "-m", "drifted commit B"], { cwd: probe });
	const commitB = Bun.spawnSync(["git", "rev-parse", "HEAD"], { cwd: probe }).stdout.toString().trim();

	// Invoke summary again
	await enterSummary();

	out.commitA = commitA;
	out.commitB = commitB;
	out.beginCallsBeforeDrift = beginCallsBeforeDrift;
	out.beginCallsAfterDrift = beginCalls.length;
	out.pushReceiptsBeforeDrift = pushReceiptsBeforeDrift;
	out.pushReceiptsAfterDrift = receipts.filter(receipt => receipt.kind === "push").length;
	out.driftNotice = uiCalls.at(-1) ?? null;
	out.headAfterDriftSummary = Bun.spawnSync(["git", "rev-parse", "HEAD"], { cwd: probe }).stdout.toString().trim();
	out.dirtyAfterDriftSummary = Bun.spawnSync(["git", "status", "--porcelain"], { cwd: probe }).stdout.toString().trim();
} else if (mode === "done-cancel" || mode === "done-cancel-decline") {
	await setNow();
	await approve(planA);
	await enterSummary();
	out.verify = await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "tests pass" });
	const attempt = attempts.at(-1);
	if (!attempt) throw new Error("no attempt after /summary");
	attempt.state = "audited";
	attempt.accepted_report_count = 1;
	receipts.push({
		receipt_id: "rec-a",
		work_id: attempt.work_id,
		revision_id: attempt.revision_id,
		candidate_id: attempt.candidate_id,
		kind: "audit",
		verdict: "PASS",
		independent: true,
		payload: { report: "VERDICT: PASS" },
		payload_sha256: "0".repeat(64),
		issuer: "work-service/auditor-settle",
		issued_at: new Date().toISOString(),
		candidate_sha256: attempt.candidate_sha256,
		candidate_commit: attempt.candidate_commit,
	});
	out.review = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "complete" });
	attempt.state = "closeout_requested";
	attempt.closeout_requested_at = new Date().toISOString();

	const done = extension.commands.get("done");
	if (!done) throw new Error("done command missing");
	const canonical = fs.realpathSync(probe);
	const cwdHash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
	const homeDir = process.env.HOME || "/tmp";
	const batchDir = path.join(homeDir, ".omp", "agent", "work-cancel-batches");
	fs.mkdirSync(batchDir, { recursive: true, mode: 0o700 });
	const batchFile = path.join(batchDir, `${cwdHash}.json`);
	fs.writeFileSync(batchFile, JSON.stringify([{ key: "HOME-2", reason: "superseded by HOME-1" }]), { mode: 0o600 });
	fs.chmodSync(batchFile, 0o600);
	uiCalls.length = 0;
	await done.handler("", runner.createCommandContext());
	out.doneUi = [...uiCalls];
	out.doneWrites = { ...writes };
	out.home2State = items.get("id-2")?.state;
	out.batchFileExists = fs.existsSync(batchFile);
	out.consumedFiles = fs.readdirSync(batchDir).filter(f => f.includes(".consumed-"));
} else if (mode === "ledger-reads") {
	out.missing = await execute({ action: "list_work" });
	out.unknown = await execute({ action: "list_work", project: "No Such Project" });
	out.empty = await execute({ action: "list_work", project: "Empty Surface" });
	out.listing = await execute({ action: "list_work", project: "The Bookends" });
	// Full-width identities: truncation of candidate/revision/commit in any
	// render is detectable, unlike short synthetic tokens.
	const ledgerCandidate = "aaaaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee";
	const ledgerRevision = "11111111-2222-7333-8444-555555555555";
	const ledgerCommit = "f".repeat(40);
	receipts.push({
		receipt_id: "rec-ledger-handoff",
		work_id: "id-1",
		revision_id: ledgerRevision,
		candidate_id: ledgerCandidate,
		kind: "handoff",
		independent: false,
		payload: { body: planA },
		payload_sha256: "1".repeat(64),
		issuer: "fixture/ledger-reads",
		issued_at: "2026-08-26T05:00:00+00:00",
		candidate_sha256: "0".repeat(64),
		candidate_commit: ledgerCommit,
	});
	receipts.push({
		receipt_id: "rec-ledger-verification",
		work_id: "id-1",
		revision_id: ledgerRevision,
		candidate_id: ledgerCandidate,
		kind: "verification",
		independent: false,
		payload: { body: "tests pass" },
		payload_sha256: "2".repeat(64),
		issuer: "fixture/ledger-reads",
		issued_at: "2026-08-26T05:01:00+00:00",
		candidate_sha256: "0".repeat(64),
		candidate_commit: ledgerCommit,
	});
	receipts.push({
		receipt_id: "rec-ledger-audit",
		work_id: "id-1",
		revision_id: ledgerRevision,
		candidate_id: ledgerCandidate,
		kind: "audit",
		verdict: "PASS",
		independent: true,
		payload: { report: "VERDICT: PASS" },
		payload_sha256: "5".repeat(64),
		issuer: "work-service/auditor-settle",
		issued_at: "2026-08-26T05:02:00+00:00",
		candidate_sha256: "0".repeat(64),
		candidate_commit: ledgerCommit,
	});
	attempts.push({
		attempt_id: "att-1",
		work_id: "id-1",
		revision_id: ledgerRevision,
		candidate_id: ledgerCandidate,
		plan_receipt_id: "plan-receipt-1",
		candidate_sha256: "0".repeat(64),
		candidate_commit: ledgerCommit,
		owner_session_id: "session-ledger",
		owner_session_started_at: "2026-08-26T04:00:00+00:00",
		owner_session_start_commit: "start-commit",
		repository: "fixture-repo",
		diff_sha256: "3".repeat(64),
		starting_dirty_paths: [],
		authorization_kind: "summary",
		authorization_ref: "summary-ledger",
		launch_count: 0,
		cancelled_launch_count: 0,
		accepted_report_count: 0,
		in_flight_launch_id: null,
		state: "audit_ready",
		terminal_reason: null,
		requested_at: "2026-08-26T05:03:00+00:00",
		closeout_requested_at: null,
		completed_at: null,
		completion_authorization_ref: null,
	});
	manifests.push({
		manifest_id: "manifest-1",
		work_id: "id-1",
		attempt_id: "att-1",
		manifest_version: 1,
		plan_receipt_id: "plan-receipt-1",
		verification_receipt_id: "rec-ledger-verification",
		candidate_id: ledgerCandidate,
		candidate_sha256: "0".repeat(64),
		candidate_commit: ledgerCommit,
		task_body: "AUDIT BODY BYTES",
		task_sha256: "4".repeat(64),
		section_hashes: {},
		created_at: "2026-08-26T05:04:00+00:00",
	});
	out.detail = await execute({ action: "get_work", work: "HOME-1" });
} else if (mode === "ledger-reads-subagent") {
	out.listing = await execute({ action: "list_work", project: "The Bookends" });
	out.writeRefusal = await execute({ action: "set_now", work: "HOME-1" });
} else if (mode === "closeout-pending-recovery") {
	await setNow();
	fs.writeFileSync(path.join(probe, "dirty.txt"), "dirty\n");
	const done = extension.commands.get("done");
	if (!done) throw new Error("done command missing");
	await done.handler("", runner.createCommandContext());
	await approve(planA);
	await done.handler("", runner.createCommandContext());
	await enterSummary();
	await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "tests pass" });
	const attempt = attempts.at(-1);
	if (!attempt) throw new Error("no attempt after /summary");
	attempt.state = "audited";
	attempt.accepted_report_count = 1;
	receipts.push({
		receipt_id: "rec-a",
		work_id: attempt.work_id,
		revision_id: attempt.revision_id,
		candidate_id: attempt.candidate_id,
		kind: "audit",
		verdict: "PASS",
		independent: true,
		payload: { report: "VERDICT: PASS" },
		payload_sha256: "0".repeat(64),
		issuer: "work-service/auditor-settle",
		issued_at: new Date().toISOString(),
		candidate_sha256: attempt.candidate_sha256,
		candidate_commit: attempt.candidate_commit,
	});
	// The stale checkpoint belongs to a real SUPERSEDED attempt (the OMP-147
	// incident shape: dead candidate, undelivered required event, no rows).
	attempts.push({
		attempt_id: "att-stale",
		work_id: "id-1",
		revision_id: attempt.revision_id,
		candidate_id: "cand-dead",
		plan_receipt_id: "plan-receipt-1",
		candidate_sha256: "9".repeat(64),
		candidate_commit: "dead-commit",
		owner_session_id: "session-dead",
		owner_session_started_at: "2026-08-26T02:00:00+00:00",
		owner_session_start_commit: "dead-start",
		repository: "fixture-repo",
		diff_sha256: "8".repeat(64),
		starting_dirty_paths: [],
		authorization_kind: "summary",
		authorization_ref: "summary-dead",
		launch_count: 0,
		cancelled_launch_count: 0,
		accepted_report_count: 0,
		in_flight_launch_id: null,
		state: "superseded",
		terminal_reason: "superseded_by_new_summary",
		requested_at: "2026-08-26T02:01:00+00:00",
		closeout_requested_at: null,
		completed_at: null,
		completion_authorization_ref: null,
	});
	const staleEvent = mockEvent("id-1", "att-stale", "attempt_superseded", "attempt_superseded", true);
	const staleAttested = nextAttestation();
	out.first = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "complete" });
	out.closeoutReceiptsAfterFirst = receipts.filter(receipt => receipt.kind === "closeout").length;
	out.attemptStateAfterFirst = attempt.state;
	await staleAttested;
	out.staleDeliveries = deliveries.filter(delivery => delivery.event_id === staleEvent.event_id).map(delivery => delivery.status);
	out.waivedCount = attestCalls.filter(call => call.status === "waived").length;
	const closeoutAttested = nextAttestation();
	out.second = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "complete" });
	out.closeoutReceipts = receipts.filter(receipt => receipt.kind === "closeout").length;
	out.attemptState = attempt.state;
	await closeoutAttested;
	const closeoutEvent = closeEvents.find(event => event.attempt_id === attempt.attempt_id && event.event_type === "closeout_review_recorded");
	out.newCheckpointDelivered = closeoutEvent !== undefined && deliveries.some(delivery => delivery.event_id === closeoutEvent.event_id && delivery.status === "delivered");
	out.totalCloseoutReceipts = receipts.filter(receipt => receipt.kind === "closeout").length;
} else if (mode === "ledger") {
	// OMP-69 smoke: installed shape only — owner agent_start → terminal
	// agent_end with real work yields exactly one displayed nextTurn message
	// and zero Work Ledger mutations. No @advisor role is configured here, so
	// the note fails open to the literal unavailable line.
	await runner.emit({ type: "agent_start" } as never);
	await runner.emit({
		type: "agent_end",
		messages: [
			{ role: "user", content: "please fix the thing", timestamp: Date.now() },
			{
				role: "assistant",
				content: [{ type: "text", text: "Fixed the thing." }],
				api: "anthropic-messages",
				provider: "anthropic",
				model: "claude-fable-5",
				usage: {},
				stopReason: "stop",
				timestamp: Date.now(),
			},
		],
	} as never);
	out.sent = sentMessages;
	out.writes = { ...writes };
} else {
	await setNow();
	fs.writeFileSync(path.join(probe, "dirty.txt"), "dirty\n");
	const done = extension.commands.get("done");
	if (!done) throw new Error("done command missing");
	await done.handler("", runner.createCommandContext());
	out.beforePlanUi = [...uiCalls];
	out.beforePlanWrites = { ...writes, comments: comments.length };
	await approve(planA);
	uiCalls.length = 0;
	await done.handler("", runner.createCommandContext());
	out.beforeReviewUi = [...uiCalls];
	out.beforeReviewWrites = { ...writes, comments: comments.length };
	await enterSummary();
	out.pushReceiptsAfterSummary = receipts.filter(receipt => receipt.kind === "push").length;
	// Verification append (seals the manifest server-side).
	out.verify = await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "tests pass" });
	// Simulate the accepted PASS settle the service would perform.
	const attempt = attempts.at(-1);
	if (!attempt) throw new Error("no attempt after /summary");
	attempt.state = "audited";
	attempt.accepted_report_count = 1;
	receipts.push({
		receipt_id: "rec-a",
		work_id: attempt.work_id,
		revision_id: attempt.revision_id,
		candidate_id: attempt.candidate_id,
		kind: "audit",
		verdict: "PASS",
		independent: true,
		payload: { report: "VERDICT: PASS" },
		payload_sha256: "0".repeat(64),
		issuer: "work-service/auditor-settle",
		issued_at: new Date().toISOString(),
		candidate_sha256: attempt.candidate_sha256,
		candidate_commit: attempt.candidate_commit,
	});
	out.review = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "complete" });
	// The review checkpoint delivered; the routine closeout request lands.
	attempt.state = "closeout_requested";
	attempt.closeout_requested_at = new Date().toISOString();
	const commentsBeforeDone = comments.length;
	uiCalls.length = 0;
	await done.handler("", runner.createCommandContext());
	out.pushReceiptsAfterDone = receipts.filter(receipt => receipt.kind === "push").length;
	out.doneUi = [...uiCalls];
	out.doneWrites = { ...writes, verdictComments: comments.length - commentsBeforeDone };
	out.doneAuthorization = attempts.at(-1)?.completion_authorization_ref ?? null;
	out.now = await execute({ action: "my_now" });
	await done.handler("", runner.createCommandContext());
	out.afterSecondDone = { ...writes };
}

process.stdout.write(JSON.stringify(out));
