// HOME-122 harness: drive the real work-now extension through its public
// events, command, and tool with a deterministic in-memory WorkService REST API.
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { ExtensionRunner, loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { confirmRoundTrip } from "./two-phase";
import { currentTranscriptRef } from "../../extensions/workflow/transcript";

const probe = process.argv[2];
const mode = process.argv[3];
const MODES = ["intake", "plan", "summary", "summary-subagent", "summary-reauth", "summary-push-fail", "done", "done-cancel", "footer", "audit", "restore", "center", "center-scoped", "center-stale", "ledger"];
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
const writes = { created: 0, addNow: 0, removeNow: 0, closed: 0 };
let nowSelected = mode === "restore";
let nowId: string | null = mode === "restore" ? "id-1" : null;
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
function mockEvent(workId: string, attemptId: string | null, eventType: string, reasonCode: string, requiresDelivery: boolean): Record<string, unknown> {
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
		legal_next_actions: [],
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
		description: "",
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
if (mode === "done-cancel") {
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
}
let commandPosts = 0;
const activityCalls: string[] = [];
// The first six activity reads fail in unscoped center mode (the failure
// scenarios each take a snapshot before the real first run) — proves the
// fourth section degrades honestly while the other three survive.
let activityFailuresLeft = mode === "center" ? 6 : 0;
let idle = true;
// One-shot: the agent "goes busy" during the snapshot's activity read.
let busyDuringSnapshot = false;

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
		return new Response(
			JSON.stringify({
				projects: [
					{ project_id: "proj-1", workspace_id: WORKSPACE_ID, name: "The Bookends", health: "onTrack" },
					...(mode === "center" || mode === "center-scoped" ? [{ project_id: "proj-2", workspace_id: WORKSPACE_ID, name: "Elsewhere", health: "onTrack" }] : []),
				],
				items: Array.from(new Set(items.values())),
				relations: [],
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
		const env = JSON.parse(init?.body ?? "{}") as { command: { type: string; payload: Record<string, unknown> } };
		const cmdType = env.command?.type;
		const payload = env.command?.payload ?? {};
		if (cmdType === "create_work_batch") {
			const batchItems = (payload.items as Array<{ title: string; description?: string }>) ?? [];
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
				createdList.push({ client_ref: "p", work_id: created.work_id, revision_id: created.revision.revision_id, key: created.alias.key, state: created.state, row_version: 1 });
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
		if (cmdType === "begin_close_attempt") {
			beginCalls.push(payload);
			const it = items.get(payload.work_id as string) ?? initialItem;
			if (!it.candidate || !("commit_sha" in it.candidate) || !(it.candidate as { commit_sha?: string }).commit_sha) {
				const event = mockEvent(it.work_id, null, "close_attempt_refused", "candidate_not_final", true);
				return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-begin-${eventSeq}` }, result: { type: "begin_close_attempt", status: "refused", attempt: null, event } }), { status: 200 });
			}
			for (const attempt of attempts) {
				if (["active", "audit_ready", "auditor_in_flight", "audited", "closeout_requested"].includes(attempt.state)) {
					attempt.state = "superseded";
					attempt.terminal_reason = "superseded_by_new_summary";
				}
			}
			const candidate = it.candidate as { candidate_id: string; candidate_sha256: string; commit_sha?: string };
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
			const event = mockEvent(attempt.work_id, attempt.attempt_id, "auditor_launch_settled", `verdict_${verdict.toLowerCase()}`, true);
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-set-${eventSeq}` }, result: { type: "settle_auditor_launch", status: "applied", attempt, receipt, verdict, event } }), { status: 200 });
		}
		if (cmdType === "attest_checkpoint_delivery") {
			attestCalls.push(payload);
			const target = closeEvents.find(e => e.event_id === payload.event_id);
			if (!target) throw new Error("attest without event");
			deliveries.push({ delivery_id: `del-${deliveries.length + 1}`, event_id: payload.event_id, delivery_sequence: deliveries.filter(d => d.event_id === payload.event_id).length + 1, owner_session_id: payload.owner_session_id, rendered_sha256: payload.rendered_sha256, status: payload.status, authorization_ref: payload.authorization_ref ?? null, created_at: new Date().toISOString() });
			const event = mockEvent(target.work_id as string, (target.attempt_id as string) ?? null, "checkpoint_delivery_recorded", `delivery_${payload.status}`, false);
			return new Response(JSON.stringify({ receipt: { state: "applied", operation_id: `op-att-${eventSeq}` }, result: { type: "attest_checkpoint_delivery", status: "applied", delivery: deliveries.at(-1), event } }), { status: 200 });
		}
		if (cmdType === "request_closeout") {
			const attempt = attempts.find(a => a.attempt_id === payload.attempt_id);
			if (!attempt) throw new Error("request_closeout without attempt");
			attempt.state = "closeout_requested";
			attempt.closeout_requested_at = new Date().toISOString();
			const event = mockEvent(attempt.work_id, attempt.attempt_id, "closeout_requested", "closeout_requested", false);
			return new Response(
				JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000015" },
					result: { type: "request_closeout", status: "applied", attempt, event },
				}),
				{ status: 200 },
			);
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
const EXTENSION_FILES =
	mode === "audit"
		? ["session-system/extensions/work-now.ts", "session-system/extensions/model-bookends.ts"]
		: ["session-system/extensions/work-now.ts"];
const loaded = await loadExtensions(EXTENSION_FILES.map(file => path.join(repoRoot, file)), probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("work-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool missing");

const uiCalls: string[] = [];
const statuses: string[] = [];
const statusCalls: { key: string; text: string | null; placement: string }[] = [];
let activeTools = ["read", "bash", "work"];
const sentUserMessages: string[] = [];
const sentMessages: Array<{ message: unknown; options: unknown }> = [];
let throwNextSend = false;
let throwNextSetTools = false;
let abortCalls = 0;
const depth = mode === "summary-subagent" ? 1 : 0;
const inheritedNow =
	mode === "summary-subagent"
		? [{ type: "custom", customType: "work-now", data: { backend: "work", issueId: "id-1", identifier: "HOME-1", title: "First", setAt: Date.now() } }]
		: [];
const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
const gptModel = { id: "gpt-5.2", provider: "openai", name: "GPT 5.2", api: "openai-responses" };
const runner = new ExtensionRunner(
	loaded.extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch: () => inheritedNow, getSessionId: () => "session-test" } as never,
	{ getAvailable: () => [fableModel, gptModel], hasProvider: () => true } as never,
	undefined,
	{ getModelRole: (role: string) => (role === "audit" ? "openai/gpt-5.2" : undefined), get: () => undefined, getStorage: () => undefined } as never,
	undefined,
	undefined,
	depth,
);
runner.initialize(
	{
		appendEntry: () => {},
		getSessionId: () => "session-test",
		deliverMessage: async () => {},
		setModel: async () => true,
		getThinkingLevel: () => "high",
		setThinkingLevel: () => {},
		sendMessage: (message: unknown, options?: unknown) => {
			sentMessages.push({ message, options });
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

const planA = "# Work\n\n## Approach\n1. Change the shared path\n\n## Verification\n1. Run the focused check\n";
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
const pastedSummary = {
	type: "message_start",
	message: { role: "user", content: "[IMPORTANT: User invoked the summary skill]", timestamp: Date.now() },
};

const out: Record<string, unknown> = {};
if (mode === "intake") {
	await runner.emit(intakeMessage as never);
	const first = await confirmRoundTrip(execute, { action: "create_work", title: "First", description: "one", project: "The Bookends" });
	out.preview = first.preview;
	out.confirmed = first.confirmed;
	const second = await confirmRoundTrip(execute, { action: "create_work", title: "Second", description: "two", project: "The Bookends" });
	out.second = second.confirmed;
	const stop = await extension.handlers.get("session_stop")?.[0]?.({ type: "session_stop", stop_hook_active: false }, ctx);
	out.stop = stop ?? null;
	out.writes = writes;
	out.nowSelected = nowSelected;
} else if (mode === "plan") {
	out.noNow = await runner.emitInput("/plan", undefined, "interactive");
	await setNow();
	out.first = await approve(planA);
	out.commentsAfterFirst = comments.length;
	out.invalid = await approve("# Missing required sections\n");
	out.commentsAfterInvalid = comments.length;
	out.firstBody = comments[0]?.body;
	out.same = await approve(planA);
	out.commentsAfterSame = comments.length;
	out.changed = await approve(planB);
	out.commentsAfterChanged = comments.length;
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
		await runner.emit(summaryMessage as never);
		out.noPlanNotice = uiCalls.at(-1);
		out.noPlanReview = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "premature" });
	}
	await approve(planA);
	out.beforeInvocation = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "before" });
	await runner.emit(pastedSummary as never);
	out.afterPaste = await execute({ action: "append_evidence", work: "HOME-1", kind: "closeout", body: "pasted" });
	await runner.emit(summaryMessage as never);
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
} else if (mode === "restore") {
	out.now = await execute({ action: "my_now" });
} else if (mode === "center" || mode === "center-scoped") {
	// OMP-25: fresh four-section prompts, no POSTs, tools flipped only inside
	// the centering turn, write refusal, no stop continuation, exact restoration.
	const center = extension.commands.get("center");
	if (!center) throw new Error("center command missing");
	const cmdCtx = runner.createCommandContext();
	const stopHandler = extension.handlers.get("session_stop")?.[0];
	const toolsBefore = [...activeTools];
	if (mode === "center") {
		// (a) Synchronous injection failure: tools untouched, /center recovers.
		throwNextSend = true;
		await center.handler("", cmdCtx);
		out.syncFailNotice = uiCalls.at(-1);
		out.toolsAfterSyncFail = [...activeTools];
		// (b) Lost injection: the prompt was sent but its turn never starts.
		// While pending, /center refuses; fresh owner input clears the wedge and
		// the next /center recovers WITHOUT any intervening turn (audit LOW fix).
		await center.handler("", cmdCtx);
		out.lostPrompts = sentUserMessages.length;
		await center.handler("", cmdCtx);
		out.wedgedRefusal = uiCalls.at(-1);
		out.promptsWhileWedged = sentUserMessages.length;
		await runner.emitInput("unrelated owner input", undefined, "interactive");
		await center.handler("", cmdCtx);
		out.promptsAfterRecovery = sentUserMessages.length;
		out.toolsAfterLostInjection = [...activeTools];
		// Drop the recovery run's pending injection the same proven way, then
		// also prove the unrelated-turn path still clears it.
		await runner.emitInput("second owner input", undefined, "interactive");
		await runner.emit({ type: "before_agent_start", prompt: "unrelated user prompt", systemPrompt: [] } as never);
		sentUserMessages.length = 0;
		// (d) Steer race: the agent goes busy during the snapshot reads — the
		// post-read idle re-check refuses and nothing is sent.
		busyDuringSnapshot = true;
		await center.handler("", cmdCtx);
		out.steerRaceNotice = uiCalls.at(-1);
		out.steerRacePrompts = sentUserMessages.length;
		idle = true;
		// (c) Isolation failure: setActiveTools([]) refuses — the turn is
		// aborted (fail closed), tools stay exactly as they were.
		throwNextSetTools = true;
		await center.handler("", cmdCtx);
		await runner.emit({ type: "before_agent_start", prompt: sentUserMessages[0] ?? "", systemPrompt: [] } as never);
		out.isolationFailNotice = uiCalls.at(-1);
		out.abortsAfterIsolationFail = abortCalls;
		out.toolsAfterIsolationFail = [...activeTools];
		out.stopAfterIsolationFail = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
		sentUserMessages.length = 0;
	}
	const postsBefore = commandPosts;
	await center.handler("", cmdCtx);
	out.firstPrompt = sentUserMessages[0] ?? null;
	out.toolsAfterCommand = [...activeTools]; // still untouched — the turn has not started
	await runner.emit({ type: "before_agent_start", prompt: sentUserMessages[0] ?? "", systemPrompt: [] } as never);
	out.toolsDuringTurn = [...activeTools];
	out.writeRefusal = await execute({ action: "create_work", title: "must be refused mid-center" });
	await center.handler("", cmdCtx); // overlap: must not start a second turn
	out.promptsAfterOverlap = sentUserMessages.length;
	out.overlapNotice = uiCalls.at(-1);
	out.stopDuringCenter = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
	await runner.emit({ type: "agent_end", messages: [] } as never);
	out.toolsAfterTurn = [...activeTools];
	out.toolsBefore = toolsBefore;
	out.postsDuringCenter = commandPosts - postsBefore;
	if (mode === "center") {
		// Second run: NOW set + an armed handoff obligation. The centering turn
		// still suppresses the checkpoint continuation; a normal stop resumes it.
		await setNow();
		await approve(planA);
		const promptsBefore = sentUserMessages.length;
		await center.handler("", cmdCtx);
		out.secondPrompt = sentUserMessages[promptsBefore] ?? null;
		await runner.emit({ type: "before_agent_start", prompt: sentUserMessages[promptsBefore] ?? "", systemPrompt: [] } as never);
		out.stopDuringSecondCenter = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
		await runner.emit({ type: "agent_end", messages: [] } as never);
		out.stopAfterCenter = (await stopHandler?.({ type: "session_stop", stop_hook_active: false }, ctx)) ?? null;
	} else {
		out.activityCalls = activityCalls;
	}
} else if (mode === "center-stale") {
	// OMP-25 fail closed: a marker naming a nonexistent project must refuse
	// with one honest error — never widen to the whole workspace.
	const center = extension.commands.get("center");
	if (!center) throw new Error("center command missing");
	await center.handler("", runner.createCommandContext());
	out.staleNotice = uiCalls.filter(call => call.includes("/center failed")).at(-1) ?? null;
	out.prompts = sentUserMessages.length;
	out.tools = [...activeTools];
} else if (mode === "footer") {
	out.initialCalls = [...statusCalls];
	await setNow();
	out.callsAfterSetNow = [...statusCalls];
} else if (mode === "audit") {
	// OMP-47 sealed-flow end-to-end: the ledger seals the auditor task after
	// verification; get_work renders it byte-for-byte; model-bookends reserves
	// one launch against the EXACT bytes and settles with the untouched
	// transport payload; the service mints the audit receipt itself.
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
	// Pre-summary close-ritual writes are refused (audit is not even a kind).
	out.unauthorized = await execute({ action: "append_evidence", work: "HOME-1", kind: "audit", body: REPORT });
	// Production shape: the client delivers /summary as a structured
	// skill-prompt only — one command, one authorization, one begin.
	await runner.emit(summaryMessage as never);
	out.beginCalls = beginCalls.length;
	out.beginSession = beginCalls[0] ? { hasStartCommit: typeof beginCalls[0].owner_session_start_commit === "string", hasDiffSha: typeof beginCalls[0].diff_sha256 === "string", hasAuthorization: String(beginCalls[0].authorization_ref ?? "").startsWith("summary:") } : null;
	// Verification append seals the manifest.
	out.verify = await execute({ action: "append_evidence", work: "HOME-1", kind: "verification", body: "bun test → pass" });
	const getWork = await execute({ action: "get_work", work: "HOME-1" });
	out.getWork = getWork;
	const sealedBody = /----- SEALED AUDITOR TASK BEGIN -----\n([\s\S]*?)\n----- SEALED AUDITOR TASK END -----/.exec(getWork)?.[1] ?? "";
	out.sealedBodyPresent = sealedBody.length > 0;
	out.sealedHasManifest = sealedBody.includes("Mode: git-range-sha256") && /Final commit: [0-9a-f]{40}/.test(sealedBody);
	// Wrong bytes: blocked BEFORE spawn, zero slot burn.
	const wrong = await runner.emitToolCall({
		type: "tool_call",
		toolName: "task",
		toolCallId: "aud-0",
		input: { context: "audit", tasks: [{ agent: "auditor", task: `${sealedBody} tampered` }] },
	} as never);
	out.wrongBlocked = wrong && typeof wrong === "object" && "block" in wrong ? Boolean(wrong.block) : false;
	out.wrongReason = wrong && typeof wrong === "object" && "reason" in wrong ? String(wrong.reason) : "";
	out.launchCountAfterWrong = attempts.at(-1)?.launch_count ?? -1;
	// outputSchema is refused.
	const schema = await runner.emitToolCall({
		type: "tool_call",
		toolName: "task",
		toolCallId: "aud-s",
		input: { context: "audit", tasks: [{ agent: "auditor", task: sealedBody, outputSchema: { type: "object" } }] },
	} as never);
	out.schemaBlocked = schema && typeof schema === "object" && "block" in schema ? Boolean(schema.block) : false;
	// A task result with no started spawn cancels its reservation without budget.
	await runner.emitToolCall({
		type: "tool_call",
		toolName: "task",
		toolCallId: "aud-cancel",
		input: { context: "audit", tasks: [{ agent: "auditor", task: sealedBody }] },
	} as never);
	const cancelled = await runner.emitToolResult({
		type: "tool_result",
		toolName: "task",
		toolCallId: "aud-cancel",
		input: {},
		content: [{ type: "text", text: "Task execution failed before start" }],
		details: { results: [], totalDurationMs: 0 },
		isError: false,
	} as never);
	out.cancelCalls = cancelCalls.length;
	out.cancelledLaunchCount = attempts.at(-1)?.cancelled_launch_count ?? -1;
	out.effectiveLaunchesAfterCancel = (attempts.at(-1)?.launch_count ?? 0) - (attempts.at(-1)?.cancelled_launch_count ?? 0);
	out.cancelAppended = JSON.stringify(cancelled).includes("reservation cancelled");
	// A later beforeToolCall block has no tool_result; tool_execution_end fallback cancels.
	await runner.emitToolCall({
		type: "tool_call",
		toolName: "task",
		toolCallId: "aud-blocked",
		input: { context: "audit", tasks: [{ agent: "auditor", task: sealedBody }] },
	} as never);
	await runner.emit({ type: "tool_execution_end", toolName: "task", toolCallId: "aud-blocked", result: { content: [{ type: "text", text: "blocked" }] }, isError: true } as never);
	out.cancelCallsAfterBlocked = cancelCalls.length;
	out.effectiveLaunchesAfterBlocked = (attempts.at(-1)?.launch_count ?? 0) - (attempts.at(-1)?.cancelled_launch_count ?? 0);
	// Exact bytes: reserved, spawn proceeds.
	const exact = await runner.emitToolCall({
		type: "tool_call",
		toolName: "task",
		toolCallId: "aud-1",
		input: { context: "audit", tasks: [{ agent: "auditor", task: sealedBody }] },
	} as never);
	out.exactBlocked = exact && typeof exact === "object" && "block" in exact ? Boolean(exact.block) : false;
	out.launchCountAfterExact = attempts.at(-1)?.launch_count ?? -1;
	// The tool result settles with the UNTOUCHED transport payload.
	const settled = (await runner.emitToolResult({
		type: "tool_result",
		toolName: "task",
		toolCallId: "aud-1",
		input: {},
		content: [{ type: "text", text: `<task-result id="Aud" agent="auditor" status="completed">\n<output>\n${REPORT}\n</output>\n</task-result>` }],
		details: { results: [{ output: REPORT }] },
		isError: false,
	} as never)) as { content?: Array<{ type: string; text?: string }> } | undefined;
	out.settleAppended = (settled?.content ?? []).map(part => (part.type === "text" ? (part.text ?? "") : "")).join("\n");
	out.settlePayload = settleCalls[0]?.transport_payload ?? null;
	out.attemptState = attempts.at(-1)?.state ?? null;
	const auditReceipt = receipts.filter(r => r.kind === "audit").at(-1);
	out.auditIssuer = auditReceipt?.issuer ?? null;
	out.auditVerdict = auditReceipt?.verdict ?? null;
	out.attestCalls = attestCalls.length;
	// The settle outcome event was delivered and attested through the shared path.
	out.attestStatus = attestCalls[0]?.status ?? null;
} else if (mode === "summary-push-fail") {
	await setNow();
	await approve(planA);
	fs.writeFileSync(path.join(probe, "work.txt"), "candidate work\n");
	const origin = Bun.spawnSync(["git", "remote", "get-url", "origin"], { cwd: probe }).stdout.toString().trim();
	Bun.spawnSync(["git", "remote", "remove", "origin"], { cwd: probe });
	await runner.emit(summaryMessage as never);
	const frozen = (items.get("HOME-1") ?? initialItem).candidate;
	out.beginAfterPushFailure = beginCalls.length;
	out.frozenAfterPushFailure = frozen?.kind ?? null;
	out.pushReceiptsAfterFailure = receipts.filter(receipt => receipt.kind === "push").length;
	out.failureNotice = uiCalls.at(-1) ?? null;

	Bun.spawnSync(["git", "remote", "add", "origin", origin], { cwd: probe });
	await runner.emit(summaryMessage as never);
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
	await runner.emit(summaryMessage as never);
	out.beginAfterRefused = beginCalls.length;
	// Owner remediates; the SAME structured channel must recover without a
	// raw input event and without a session restart.
	Bun.spawnSync(["git", "reset", "-q"], { cwd: probe });
	await runner.emit(summaryMessage as never);
	out.beginAfterStructuredRetry = beginCalls.length;
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
} else if (mode === "done-cancel") {
	await setNow();
	await approve(planA);
	await runner.emit(summaryMessage as never);
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
	await runner.emit(summaryMessage as never);
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
