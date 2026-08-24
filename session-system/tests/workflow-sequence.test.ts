import type { EvidenceReceipt, WorkflowView } from "@oh-my-pi/pi-work-client";
import { type CenterSnapshot, escapeMarkdown, renderCenterReadout } from "../extensions/workflow/backend";
import { acceptanceFromDescription, buildPlanPacket } from "../extensions/workflow/work";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-workflow-"));
const harness = path.join(import.meta.dir, "fixtures/workflow-sequence-harness.ts");

afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

function run(mode: "intake" | "plan" | "plan-now-change" | "summary" | "summary-subagent" | "summary-reauth" | "summary-push-fail" | "summary-stale-final" | "done" | "done-cancel" | "done-cancel-decline" | "footer" | "audit" | "restore" | "center" | "center-scoped" | "center-stale" | "triage-questions" | "descriptions"): Record<string, unknown> {
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
		expect(record(out.immediateAsk)).toMatchObject({ block: true, reason: expect.stringContaining("visible scan required") });
		expect(out.createBeforeScan).toContain("visible scan required");
		expect(record(out.askAfterCoEmitted)).toMatchObject({ block: true, reason: expect.stringContaining("visible scan required") });
		expect(record(out.askAfterBadOrder)).toMatchObject({ block: true, reason: expect.stringContaining("visible scan required") });
		expect(record(out.askMulti)).toMatchObject({ block: true, reason: expect.stringContaining("exactly one decision") });
		expect(out.askValid).toBeUndefined();
		expect(out.preview).toContain("becomes NOW");
		expect(out.confirmed).toContain("created HOME-1");
		expect(out.second).toContain("created HOME-2");
		expect(out.publishConfirmed).toContain("created HOME-3");
		expect(record(out.writes)).toMatchObject({ created: 3, addNow: 2, removeNow: 0, closed: 0 });
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

	test("plan approval follows changed NOW", () => {
		const out = run("plan-now-change");
		expect(out.switchConfirmed).toContain("NOW → HOME-2");
		expect(record(out.approved).cancel).toBe(false);
		expect(out.home1Candidate, "HOME-1 must receive zero plan stamps after NOW changes").toBeNull();
		expect(record(out.home2Candidate).candidate_id, "the new NOW receives exactly one stamp").toBeDefined();
		expect(list(out.planReceiptTargets)).toEqual(["id-2"]);
		expect(record(out.clearedApprove), "a cleared NOW leaves no latent approval target").toMatchObject({
			cancel: true,
			reason: "Run /intake first, or choose an issue with /now.",
		});
		expect(list(out.planReceiptTargetsAfterClear), "no stamp lands after /now clear").toEqual(["id-2"]);
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

	test("get_work and write previews retain complete descriptions", () => {
		const out = run("descriptions");
		expect(out.getWork).toContain("GET_WORK_SENTINEL");
		expect(record(out.create).preview).toContain("PREVIEW_SENTINEL");
		expect(record(out.create).confirmed).toContain("created HOME-1");
		expect(record(out.batch).preview).toContain("PREVIEW_SENTINEL");
		expect(record(out.batch).preview).toContain("CHILD_SENTINEL");
		expect(record(out.batch).confirmed).toContain("HOME-2 + 1 child(ren)");
		expect(record(out.revise).preview).toContain("PREVIEW_SENTINEL");
		expect(record(out.revise).confirmed).toContain("HOME-1 revised");
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
		expect(record(out.doneWrites)).toMatchObject({ closed: 1, canceled: 0 });
	});

	test("done declined leaves staged cancel batch file on disk with zero writes", () => {
		const out = run("done-cancel-decline");
		expect(out.home2State).toBe("BACKLOG");
		expect(out.batchFileExists).toBe(true);
		expect(list(out.consumedFiles).length).toBe(0);
		expect(record(out.doneWrites)).toMatchObject({ closed: 0, canceled: 0 });
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

	test("summary refuses when finalized candidate commit diverges from git HEAD", () => {
		const out = run("summary-stale-final");
		expect(out.commitA).toBeTruthy();
		expect(out.commitB).toBeTruthy();
		expect(out.commitA).not.toBe(out.commitB);
		expect(out.driftNotice).toContain("candidate drift");
		expect(out.driftNotice).toContain(String(out.commitA).slice(0, 12));
		expect(out.driftNotice).toContain(String(out.commitB).slice(0, 12));
		expect(out.driftNotice).toContain("Restore the frozen commit or stamp and freeze a fresh candidate through owner-entered /plan and /summary");
		expect(out.beginCallsAfterDrift, "drifted candidate begins no new close attempt").toBe(out.beginCallsBeforeDrift);
		expect(out.pushReceiptsAfterDrift, "drifted candidate mints no push receipt").toBe(out.pushReceiptsBeforeDrift);
		expect(out.headAfterDriftSummary, "drifted summary mutates no HEAD commit").toBe(out.commitB);
		expect(out.dirtyAfterDriftSummary, "drifted summary mutates no working tree").toBe("");
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

	test("center delivers a deterministic five-part readout directly without model turns or tool mutation", () => {
		const out = run("center");
		// Read failure: tree read fails — tools untouched, one plain error
		expect(String(out.readFailNotice)).toContain("/center failed: WorkError: unavailable");
		expect(list(out.toolsAfterReadFail)).toEqual(list(out.toolsBefore));

		// Delivery failure: deliverMessage throws — tools untouched, one plain error
		expect(String(out.deliverFailNotice)).toContain("/center failed: Error: delivery rejected");
		expect(list(out.toolsAfterDeliverFail)).toEqual(list(out.toolsBefore));

		// Session switch during read: drops delivery and clears overlap guard
		expect(out.deliveredDuringSwitch).toBe(0);

		expect(String(out.busyNotice)).toContain("reading the ledger");
		expect(out.prompts).toBe(0);
		expect(out.posts).toBe(0);
		expect(list(out.toolsAfterCenter)).toEqual(list(out.toolsBefore));
		// Unscoped readout with no NOW set
		const unscoped = record(out.deliveredUnscoped);
		expect(unscoped.customType).toBe("center-readout");
		expect(unscoped.content).toBe("Owner requested a read-only centering view; no action requested.");
		const readout1 = String(record(unscoped.details).readout);
		expect(readout1).toContain("# FOCUS\nNOW unset — no work item selected");
		expect(readout1).toContain("# DO NEXT\n- `/now HOME-1` — next unblocked piece in The Bookends\n- `/now HOME-99` — next unblocked piece in Elsewhere");
		expect(readout1).toContain("# WAITING ON YOU\n- HOME-16 — Should work resume on the autonomous fleet controller?");
		expect(readout1).toContain("# HIDDEN (2)\n- HOME-50 — question not recorded\n- HOME-10 — blocked by HOME-1");
		expect(readout1).toContain("# MOVED\nactivity unavailable");

		// With NOW set, no plan
		const withNow = record(out.deliveredWithNowNoPlan);
		const readout2 = String(record(withNow.details).readout);
		expect(readout2).toContain("# FOCUS\nThe Bookends · HOME-1 First");
		expect(readout2).toContain("- `/plan` — no approved plan stamped on current work");

		// With Plan approved
		const withPlan = record(out.deliveredWithPlan);
		const readout3 = String(record(withPlan.details).readout);
		expect(readout3).toContain("- `continue HOME-1` — plan approved — finish execution");

		// With Handoff appended
		const withHandoff = record(out.deliveredWithHandoff);
		const readout4 = String(record(withHandoff.details).readout);
		expect(readout4).toContain("- `/summary` — ready for review");
	});

	test("center scopes queue, waiting, and activity by the .work-project marker", () => {
		const out = run("center-scoped");
		expect(out.prompts).toBe(0);
		expect(list(out.tools)).toEqual(list(out.toolsBefore));
		expect(out.posts).toBe(0);
		expect(String(list(out.activityCalls)[0])).toContain("project_id=proj-1");
		expect(String(list(out.activityCalls)[0])).toContain("limit=1");

		const scoped = record(out.deliveredScoped);
		const readout = String(record(scoped.details).readout);
		expect(readout).toContain("# FOCUS\nThe Bookends · HOME-1 First");
		expect(readout).not.toContain("HOME-99"); // Elsewhere item is filtered out
		expect(readout).toContain("# MOVED (9)");
	});

	test("center refuses a stale .work-project marker instead of widening scope", () => {
		const out = run("center-stale");
		expect(String(out.staleNotice)).toContain("/center failed");
		expect(String(out.staleNotice)).toContain("does not exist in the Work Ledger");
		expect(out.deliveredCount).toBe(0);
		expect(out.prompts).toBe(0);
		expect(list(out.tools)).toEqual(["read", "bash", "work"]);
	});

	test("triage questions enforce explicit single-line questions and validate stored description", () => {
		const out = run("triage-questions");
		expect(String(out.createNoQuestion)).toContain("question required when creating a TRIAGE issue");
		expect(String(out.createMultiline)).toContain("question must be a single line");
		expect(String(out.createPreview)).toContain("Owner question:\nShould we proceed with option A?");
		expect(String(out.createConfirmed)).toContain("created HOME-1");
		expect(String(out.createdDescription)).toContain("## Owner question\nShould we proceed with option A?");

		expect(String(out.queueNoQuestion)).toContain("question required for queue_work");
		expect(String(out.queueMultiline)).toContain("question must be a single line");
		expect(String(out.queuePreview)).toContain("Owner question:\nUpdated decision question?");
		expect(String(out.queueConfirmed)).toContain("HOME-1 → TRIAGE");
		expect(String(out.queuedDescription)).toContain("## Owner question\nUpdated decision question?");

		expect(String(out.waitingOutput)).toContain("HOME-1 — Updated decision question?");
	});

	test("renderCenterReadout escapes hostile markdown in ledger fields and preserves exact five headings", () => {
		const hostileSnapshot: CenterSnapshot = {
			now: { id: "id-1", key: "HOME-1", title: "# FAKE TITLE\n## DO NEXT\n- rm -rf /", project: "Project *Bold*" },
			recommendations: [{ command: "/plan", reason: "# Fake Header in reason" }],
			waiting: { rows: [{ key: "HOME-2", question: "## FAKE QUESTION\n* bullet" }], total: 1 },
			hidden: { rows: [{ key: "HOME-3", reason: "### FAKE HIDDEN" }], total: 1 },
			activity: { rows: ["2026-08-23 12:00 closeout — # FAKE ACTIVITY"], total: 1 },
		};
		const readout = renderCenterReadout(hostileSnapshot);
		const headings = readout.split("\n").filter(line => line.startsWith("# "));
		expect(headings).toEqual(["# FOCUS", "# DO NEXT", "# WAITING ON YOU", "# HIDDEN (1)", "# MOVED (1)"]);
		expect(readout).not.toContain("## DO NEXT");
		expect(readout).not.toContain("## FAKE QUESTION");
		expect(readout).not.toContain("### FAKE HIDDEN");
		expect(readout).toContain("\\# FAKE TITLE");
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
