import type { EvidenceReceipt, WorkflowView } from "@oh-my-pi/pi-work-client";
import { acceptanceFromDescription, buildPlanPacket } from "../extensions/workflow/work";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-workflow-"));
const harness = path.join(import.meta.dir, "fixtures/workflow-sequence-harness.ts");

afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

function run(mode: "intake" | "plan" | "summary" | "summary-subagent" | "summary-reauth" | "summary-push-fail" | "done" | "done-cancel" | "footer" | "audit" | "restore" | "center" | "center-scoped" | "center-stale"): Record<string, unknown> {
	const root = path.join(tempRoot, mode);
	const home = path.join(root, "home");
	const probe = path.join(root, "repo");
	fs.mkdirSync(path.join(home, ".omp", "agent"), { recursive: true });
	fs.mkdirSync(path.join(home, ".config", "omp-work"), { recursive: true });
	fs.mkdirSync(probe, { recursive: true });
	fs.writeFileSync(
		path.join(home, ".config", "omp-work", "client.json"),
		JSON.stringify({
			base_url: "http://127.0.0.1:54322",
			workspace_id: "00000000-0000-7000-8000-000000000001",
			owner_id: "00000000-0000-7000-8000-000000000002",
		}),
	);
	const remote = path.join(root, "remote.git");
	Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
	Bun.spawnSync(["git", "init", "-q", "-b", "main"], { cwd: probe });
	Bun.spawnSync(["git", "config", "user.email", "test@example.com"], { cwd: probe });
	Bun.spawnSync(["git", "config", "user.name", "Test"], { cwd: probe });
	fs.writeFileSync(path.join(probe, "init.txt"), "init\n");
	Bun.spawnSync(["git", "add", "init.txt"], { cwd: probe });
	Bun.spawnSync(["git", "commit", "-q", "-m", "init"], { cwd: probe });
	Bun.spawnSync(["git", "remote", "add", "origin", remote], { cwd: probe });
	Bun.spawnSync(["git", "push", "-q", "-u", "origin", "main"], { cwd: probe });
	const child = Bun.spawnSync([process.execPath, harness, probe, mode], {
		cwd: probe,
		env: { ...process.env, HOME: home, OMP_WORK_BEARER: "test-token" },
	});
	expect(child.exitCode, child.stderr.toString()).toBe(0);
	return JSON.parse(child.stdout.toString()) as Record<string, unknown>;
}

function record(value: unknown): Record<string, unknown> {
	return value as Record<string, unknown>;
}

function list(value: unknown): string[] {
	return value as string[];
}

describe("HOME-122 workflow sequence", () => {
	test("intake publication selects only the first issue and creates no execution debt", () => {
		const out = run("intake");
		expect(out.preview).toContain("becomes NOW");
		expect(out.confirmed).toContain("created HOME-1");
		expect(out.second).toContain("created HOME-2");
		expect(record(out.writes)).toMatchObject({ created: 2, addNow: 1, removeNow: 0, closed: 0 });
		expect(out.nowSelected).toBe(true);
		expect(out.stop, "intake selection must not masquerade as execution").toBeNull();
	});

	test("plan approval is fail-closed, deterministic, idempotent, and arms one handoff", () => {
		const out = run("plan");
		expect(record(out.noNow).handled).toBe(true);
		expect(record(out.first).cancel).toBe(false);
		expect(out.commentsAfterFirst, "approval must await its Linear stamp").toBe(1);
		expect(out.firstBody).toContain("**Plan approved**");
		expect(out.firstBody).toContain(`SHA-256: \`${out.hashA}\``);
		expect(record(out.invalid)).toMatchObject({
			cancel: true,
			reason: "Plan approval requires non-empty ## Approach and ## Verification lists.",
		});
		expect(out.commentsAfterInvalid).toBe(1);
		expect(out.firstBody).toContain("## Approach\n1. Change the shared path");
		expect(out.firstBody).toContain("## Verification\n1. Run the focused check");
		expect(out.commentsAfterSame, "same bytes must not duplicate the stamp").toBe(1);
		expect(out.commentsAfterChanged, "changed bytes require a fresh stamp").toBe(2);
		expect(out.firstBody).not.toContain(String(out.hashB));
		expect(record(out.stopFirst).additionalContext).toContain("Post one silent workflow checkpoint");
		expect(out.stopSecond, "continuation is injected only once").toBeNull();
		expect(out.evidence, "ambient untyped notes are refused with the kind menu").toContain("kind required");
		expect(out.statusAfterEvidence, "a refused note cannot settle handoff debt").toContain("⚠");
		expect(out.handoff).toContain("handoff receipt recorded on HOME-1");
		expect(out.statusAfterHandoff).not.toContain("⚠");
		expect(out.stopAfterHandoff).toBeNull();
	});

	test("summary requires a current plan and structured owner invocation", () => {
		const out = run("summary");
		expect(out.noPlanNotice).toContain("No plan is stamped on this work");
		expect(out.noPlanReview).toContain("Run /plan first");
		expect(out.afterPaste).toContain("literally enter /summary");
		expect(out.afterStructured).toContain("closeout receipt recorded on HOME-1");
		const reviews = list(out.reviewBodies);
		expect(reviews).toHaveLength(1);
		expect(reviews[0]).toContain("**Session review**");
		expect(reviews[0]).toContain("Plan SHA-256:");
		// OMP-47/OMP-43: owner lifecycle resets the shared transcript ref; a new
		// literal /summary begins exactly one fresh ledger attempt per candidate.
		expect(record(out.lifecycleAfterStart)).toEqual({ transcriptChanged: true });
		expect(record(out.ownerSwitchLifecycle)).toEqual({ transcriptChanged: true });
	});

	test("subagent summary provenance cannot authorize the review", () => {
		const out = run("summary-subagent");
		expect(out.beforeInvocation).toContain("literally enter /summary");
		expect(out.afterPaste).toContain("literally enter /summary");
		expect(out.afterStructured).toContain("literally enter /summary");
		expect(list(out.reviewBodies)).toHaveLength(0);
		// OMP-43: the subagent's own lifecycle events (the auditor's session_start,
		// any switch) must leave the owner's shared transcript ref untouched.
		expect(record(out.lifecycleAfterStart)).toEqual({ transcriptChanged: false });
		expect(record(out.lifecycleAfterSwitch)).toEqual({ transcriptChanged: false });
	});

	test("fresh sessions restore the backend focus without a local cache", () => {
		const out = run("restore");
		expect(out.now).toContain("HOME-1 First");
	});

	test("done refuses early, then closes reviewed NOW once and reaches commit step", () => {
		const out = run("done");
		const beforePlanUi = list(out.beforePlanUi);
		expect(beforePlanUi).toContain("notify:Run /plan first.");
		expect(beforePlanUi.some(call => call.startsWith("confirm:") || call.startsWith("select:"))).toBe(false);
		expect(record(out.beforePlanWrites)).toMatchObject({ closed: 0, comments: 0, removeNow: 0 });
		const beforeReviewUi = list(out.beforeReviewUi);
		expect(beforeReviewUi).toContain("notify:Run /summary first.");
		expect(beforeReviewUi.some(call => call.startsWith("confirm:") || call.startsWith("select:"))).toBe(false);
		expect(record(out.beforeReviewWrites)).toMatchObject({ closed: 0, comments: 1, removeNow: 0 });
		expect(out.verify, "verification append seals the audit manifest").toContain("audit manifest sealed");
		expect(out.pushReceiptsAfterSummary, "summary records the first verified push").toBe(1);
		const doneUi = list(out.doneUi);
		expect(doneUi.filter(call => call.startsWith("confirm:This is your verdict"))).toHaveLength(1);
		expect(doneUi.some(call => call.startsWith("select:"))).toBe(false);
		expect(record(out.doneWrites)).toMatchObject({ closed: 1, removeNow: 1, verdictComments: 1 });
		expect(out.pushReceiptsAfterDone, "done re-verifies remote state without duplicating the receipt").toBe(1);
		expect(String(out.doneAuthorization), "the literal /done mints a fresh single-use authorization").toStartWith("done:");
		expect(out.now).toBe("NOW unset");
		expect(record(out.afterSecondDone)).toMatchObject({ closed: 1, removeNow: 1 });
	});

	test("done with staged cancel batch cancels target atomically and consumes batch file", () => {
		const out = run("done-cancel");
		const doneUi = list(out.doneUi);
		expect(doneUi.some(call => call.includes("close HOME-1 and cancel 1 item(s)?"))).toBe(true);
		expect(doneUi.some(call => call.includes('HOME-2 — "superseded by HOME-1"'))).toBe(true);
		expect(doneUi.some(call => call.includes("Cancellation batch SHA-256:"))).toBe(true);
		expect(out.home2State).toBe("CANCELED");
		expect(out.batchFileExists).toBe(false);
		expect(list(out.consumedFiles).length).toBe(1);
	});

	test("a structured-only /summary recovers from a refused freeze and re-authorizes", () => {
		const out = run("summary-reauth");
		expect(out.beginAfterRefused, "a refused freeze begins nothing").toBe(0);
		// The state that used to deadlock: authorized, no begun attempt. The
		// SAME structured channel must recover after remediation — no raw
		// input event, no session restart.
		expect(out.beginAfterStructuredRetry, "structured retry begins a fresh attempt").toBe(1);
		expect(out.beginAfterUnrelated, "unrelated owner messages never authorize").toBe(1);
		// The raw channel also re-authorizes; the service supersedes to keep
		// exactly one live attempt.
		expect(out.beginAfterRaw, "raw /summary re-authorizes").toBe(2);
	});

	test("summary pushes the frozen candidate before beginning and retries a recoverable failure", () => {
		const out = run("summary-push-fail");
		expect(out.frozenAfterPushFailure).toBe("final");
		expect(out.beginAfterPushFailure, "a failed push begins no close attempt").toBe(0);
		expect(out.pushReceiptsAfterFailure).toBe(0);
		expect(out.failureNotice).toContain("push unverified");
		expect(out.beginAfterPushRetry, "the frozen candidate retries without another freeze").toBe(1);
		expect(out.pushReceiptsAfterRetry).toBe(1);
		expect(out.remoteCommit).toBe(out.candidateCommit);
	});

	test("the ledger-sealed audit task drives one exact-byte bounded launch", () => {
		const out = run("audit");
		// Pre-summary close-ritual writes are refused.
		expect(out.unauthorized, "audit is a close-ritual kind").toContain("literally enter /summary");
		// The literal /summary began ONE ledger attempt with host-computed identity.
		expect(out.beginCalls).toBe(1);
		expect(record(out.beginSession)).toEqual({ hasStartCommit: true, hasDiffSha: true, hasAuthorization: true });
		// Verification append sealed the manifest; get_work renders the exact task.
		expect(out.verify).toContain("audit manifest sealed");
		expect(out.getWork).toContain("AUDIT TASK (sealed");
		expect(out.sealedBodyPresent).toBe(true);
		expect(out.sealedHasManifest, "the Final diff carries the git-range-sha256 manifest").toBe(true);
		// Changed bytes refuse BEFORE spawn with zero slot burn; schema refused.
		expect(out.wrongBlocked).toBe(true);
		expect(String(out.wrongReason)).toContain("manifest_task_mismatch");
		expect(out.launchCountAfterWrong).toBe(0);
		expect(out.schemaBlocked).toBe(true);
		// A pre-start Task failure cancels its reservation and preserves budget.
		expect(out.cancelCalls).toBe(1);
		expect(out.cancelledLaunchCount).toBe(1);
		expect(out.effectiveLaunchesAfterCancel).toBe(0);
		expect(out.cancelAppended).toBe(true);
		expect(out.cancelCallsAfterBlocked).toBe(2);
		expect(out.effectiveLaunchesAfterBlocked).toBe(0);
		// Exact bytes reserve the next physical launch and the spawn proceeds.
		expect(out.exactBlocked).toBe(false);
		expect(out.launchCountAfterExact).toBe(3);
		// The tool result settled with the UNTOUCHED transport payload; the
		// service minted the audit receipt itself and the outcome reached the
		// model in-band plus the owner as an attested checkpoint.
		expect(out.settlePayload).toContain("VERDICT: PASS");
		expect(String(out.settleAppended)).toContain("launch settled by the ledger");
		expect(out.attemptState).toBe("audited");
		expect(out.auditIssuer).toBe("work-service/auditor-settle");
		expect(out.auditVerdict).toBe("PASS");
		expect(out.attestCalls).toBeGreaterThanOrEqual(1);
		expect(out.attestStatus).toBe("delivered");
	});

	test("footer splits the inline current issue from the lower summary", () => {
		const out = run("footer");
		type StatusCall = { key: string; text: string | null; placement: string };
		const initial = (out.initialCalls as StatusCall[]).filter(call => call.key === "work-now-current");
		// The no-NOW session-start pass clears the inline slot, never a placeholder.
		expect(initial.length).toBeGreaterThan(0);
		expect(initial.every(call => call.text === null && call.placement === "inline")).toBe(true);

		const calls = out.callsAfterSetNow as StatusCall[];
		const current = calls.filter(call => call.key === "work-now-current").at(-1);
		expect(current).toEqual({ key: "work-now-current", text: "▶ HOME-1 First", placement: "inline" });

		const lower = calls.filter(call => call.key === "work-now").at(-1);
		expect(lower?.placement).toBe("footer");
		expect(lower?.text).not.toContain("HOME-1");
		expect(lower?.text).not.toContain("First");
	});

	test("center injects one fresh read-only four-section orientation turn", () => {
		const out = run("center");
		// Injection failure paths never touch the tool set.
		expect(String(out.syncFailNotice)).toContain("/center failed");
		expect(list(out.toolsAfterSyncFail)).toEqual(["read", "bash", "work"]);
		// Lost injection: wedged /center refuses, fresh owner input clears it,
		// and the next /center recovers without any intervening turn.
		expect(out.lostPrompts).toBe(1);
		expect(String(out.wedgedRefusal)).toContain("already running");
		expect(out.promptsWhileWedged).toBe(1);
		expect(out.promptsAfterRecovery).toBe(2);
		expect(list(out.toolsAfterLostInjection)).toEqual(["read", "bash", "work"]);
		// Steer race: agent went busy during the snapshot reads — refused, nothing sent.
		expect(String(out.steerRaceNotice)).toContain("run /center again");
		expect(out.steerRacePrompts).toBe(0);
		// Isolation failure fails closed: turn aborted, tools untouched, state clear.
		expect(String(out.isolationFailNotice)).toContain("tool isolation refused");
		expect(out.abortsAfterIsolationFail).toBe(1);
		expect(list(out.toolsAfterIsolationFail)).toEqual(["read", "bash", "work"]);
		expect(out.stopAfterIsolationFail).toBeNull();
		// The prompt carries the snapshot header and all four mandatory sections.
		const first = String(out.firstPrompt);
		expect(first).toContain("── /center — centering snapshot @");
		for (const heading of ["**Where I am**", "**What's next**", "**Stuck on you**", "**What just moved**"]) {
			expect(first).toContain(heading);
		}
		// No focus: says so and points to /now without selecting anything.
		expect(first).toContain("NOW: unset — no focus is selected");
		expect(first).toContain("point Chris to /now");
		// Bounded sections with honest totals; activity failure degrades only itself.
		expect(first).toContain("HOME-99 Elsewhere item"); // unscoped covers the whole workspace
		expect(first).toContain("STUCK ON CHRIS (1 total)");
		expect(first).toContain("HOME-50 Parked decision");
		expect(first).toContain("WHAT JUST MOVED: unavailable this run");
		// Tools flip only inside the centering turn; writes are refused during it.
		expect(list(out.toolsAfterCommand)).toEqual(["read", "bash", "work"]);
		expect(list(out.toolsDuringTurn)).toEqual([]);
		expect(String(out.writeRefusal)).toContain("REFUSED — /center is read-only");
		// Overlap never starts a second turn; the run performs zero POSTs.
		expect(out.promptsAfterOverlap).toBe(1);
		expect(String(out.overlapNotice)).toContain("already running");
		expect(out.postsDuringCenter).toBe(0);
		expect(out.stopDuringCenter).toBeNull();
		expect(list(out.toolsAfterTurn)).toEqual(list(out.toolsBefore));
		// Second run: fresh snapshot names the global NOW; the armed handoff
		// continuation stays suppressed during centering and resumes after.
		const second = String(out.secondPrompt);
		expect(second).toContain("NOW: The Bookends · HOME-1 First");
		expect(second).toContain("WHAT JUST MOVED (");
		expect(out.stopDuringSecondCenter).toBeNull();
		expect(record(out.stopAfterCenter).continue).toBe(true);
	});

	test("center scopes queue, waiting, and activity by the .work-project marker", () => {
		const out = run("center-scoped");
		const first = String(out.firstPrompt);
		expect(first).toContain('Scope: project "The Bookends" (.work-project)');
		expect(first).toContain("HOME-1 First");
		expect(first).not.toContain("HOME-99"); // the Elsewhere project is filtered out
		expect(first).toContain("WHAT JUST MOVED (9 total)");
		expect(first).toContain("… and 8 more");
		expect(String(list(out.activityCalls)[0])).toContain("project_id=proj-1");
		expect(String(list(out.activityCalls)[0])).toContain("limit=8");
		expect(list(out.toolsDuringTurn)).toEqual([]);
		expect(out.postsDuringCenter).toBe(0);
		expect(list(out.toolsAfterTurn)).toEqual(list(out.toolsBefore));
	});

	test("center refuses a stale .work-project marker instead of widening scope", () => {
		const out = run("center-stale");
		expect(String(out.staleNotice)).toContain("/center failed");
		expect(String(out.staleNotice)).toContain("does not exist in the Work Ledger");
		expect(out.prompts).toBe(0);
		expect(list(out.tools)).toEqual(["read", "bash", "work"]);
	});
});

describe("OMP-38 plan packet", () => {
	const planReceipt = (id: string, issuedAt: string, sha: string, baseCommit?: string, baseDirtyPaths?: string[]): EvidenceReceipt =>
		({
			receipt_id: id,
			work_id: "w1",
			revision_id: "rev-1",
			candidate_id: "cand-1",
			kind: "plan",
			payload: {
				body: "# Plan",
				plan_sha256: "a".repeat(64),
				...(baseCommit ? { base_commit: baseCommit } : {}),
				...(baseDirtyPaths ? { base_dirty_paths: baseDirtyPaths } : {}),
			},
			payload_sha256: sha,
			issuer: "test",
			issued_at: issuedAt,
			independent: false,
		}) as EvidenceReceipt;
	// partial mock shaped for buildPlanPacket's reads only
	const view = (receipts: EvidenceReceipt[], criteria: string[] = [], description = ""): WorkflowView =>
		({
			item: {
				work_id: "w1",
				revision: { revision_id: "rev-1", title: "t", description, scope: "", acceptance_criteria: criteria },
				candidate: {
					candidate_id: "cand-1",
					candidate_sha256: "c".repeat(64),
					commit_sha: "b".repeat(40),
					kind: "final",
				},
			},
			receipts,
			relations: [],
			closeout: [],
		}) as unknown as WorkflowView;

	test("newest plan receipt wins deterministically regardless of row order", () => {
		const older = planReceipt("r-1", "2026-08-19T10:00:00Z", "1".repeat(64));
		const newer = planReceipt("r-2", "2026-08-20T10:00:00Z", "2".repeat(64));
		expect(buildPlanPacket(view([newer, older]))?.planReceiptSha256).toBe("2".repeat(64));
		expect(buildPlanPacket(view([older, newer]))?.planReceiptSha256).toBe("2".repeat(64));
		// issued_at tie breaks on receipt_id
		const tieA = planReceipt("r-a", "2026-08-20T10:00:00Z", "3".repeat(64));
		const tieB = planReceipt("r-b", "2026-08-20T10:00:00Z", "4".repeat(64));
		expect(buildPlanPacket(view([tieB, tieA]))?.planReceiptSha256).toBe("4".repeat(64));
	});

	test("plan packet preserves a valid persisted audit base and dirty snapshot", () => {
		const base = "d".repeat(40);
		const packet = buildPlanPacket(view([planReceipt("r-1", "2026-08-20T10:00:00Z", "1".repeat(64), base, ["before.txt"])]));
		expect(packet?.baseCommit).toBe(base);
		expect(packet?.baseDirtyPaths).toEqual(["before.txt"]);
		expect(buildPlanPacket(view([planReceipt("r-2", "2026-08-20T10:00:00Z", "2".repeat(64), "not-a-commit")]))?.baseCommit).toBeUndefined();
	});

	test("the cap prices the render, so floods of tiny criteria cannot slip under it", () => {
		const flood = Array.from({ length: 20_000 }, () => "");
		const packet = buildPlanPacket(view([planReceipt("r-1", "2026-08-20T10:00:00Z", "1".repeat(64))], flood));
		expect(packet?.capped).toBeDefined();
		expect(packet?.planBody).toBeUndefined();
		expect(packet?.acceptanceCriteria).toEqual([]);
	});

	test("structured criteria win; the description fallback parses only its own section", () => {
		const description = "Intro.\n\n## Acceptance criteria\n- AC-1 first\n- AC-2 second\n\n## Verification\n- not a criterion";
		const structured = buildPlanPacket(view([planReceipt("r-1", "2026-08-20T10:00:00Z", "1".repeat(64))], ["AC-9 structured"], description));
		expect(structured?.acceptanceCriteria).toEqual(["AC-9 structured"]);
		const fallback = buildPlanPacket(view([planReceipt("r-1", "2026-08-20T10:00:00Z", "1".repeat(64))], [], description));
		expect(fallback?.acceptanceCriteria).toEqual(["AC-1 first", "AC-2 second"]);
		expect(acceptanceFromDescription("no section here")).toEqual([]);
	});
});
