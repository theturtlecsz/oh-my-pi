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

function run(mode: "intake" | "plan" | "plan-now-change" | "summary" | "summary-subagent" | "summary-reauth" | "summary-push-fail" | "summary-stale-final" | "summary-final-reuse" | "summary-begin-refused" | "summary-refusal-durable" | "summary-rider-refusal-durable" | "stop-continuation-states" | "atomic-child" | "done" | "done-cancel" | "done-cancel-decline" | "footer" | "audit" | "restore" | "now-canceled" | "center" | "center-scoped" | "center-stale" | "triage-questions" | "ledger-reads" | "ledger-reads-subagent" | "closeout-pending-recovery" | "descriptions" | "omp140-audit-states" | "omp140-restart-flow" | "omp140-failed-checkpoint" | "omp140-terminal-guidance"): Record<string, unknown> {
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
		// Pin XDG_CONFIG_HOME to the temp HOME: hosted CI images export it globally,
		// and ompWorkConfigDir() prefers it over $HOME/.config (OMP-254).
		env: { ...process.env, HOME: home, XDG_CONFIG_HOME: path.join(home, ".config"), OMP_WORK_BEARER: "test-token", PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent") },
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
		expect(out.missingBlueprint).toContain("intake_blueprint_missing: save and lint local://intake-{slug}.md before publishing");
		expect(record(out.writesAfterMissing)).toMatchObject({ created: 0, addNow: 0, removeNow: 0, closed: 0 });
		expect(out.symlinkRefusal).toContain("intake_blueprint_missing: save and lint local://intake-{slug}.md before publishing");
		expect(record(out.writesAfterSymlink)).toMatchObject({ created: 0, addNow: 0, removeNow: 0, closed: 0 });
		expect(out.mismatchPayload).toContain("intake_blueprint_mismatch: description must exactly match local://intake-first.md; save the changed bytes and re-run intake lint");
		expect(record(out.writesAfterMismatch)).toMatchObject({ created: 0, addNow: 0, removeNow: 0, closed: 0 });
		expect(out.preview).toContain("becomes NOW");
		expect(out.confirmAfterDrift).toContain("intake_blueprint_mismatch: description must exactly match local://intake-first.md; save the changed bytes and re-run intake lint");
		expect(record(out.writesAfterDrift)).toMatchObject({ created: 0, addNow: 0, removeNow: 0, closed: 0 });
		expect(out.confirmed).toContain("created HOME-1");
		expect(record(out.writesAfterFirst)).toMatchObject({ created: 1, addNow: 1, removeNow: 0, closed: 0 });
		expect(out.omp166Refusal).toContain("intake_decomposition_required: blueprint declares multiple deliverables without native blocking relations; save one local://intake-{slug}.md per independent complaint, or publish one linked batch when the slices truly block each other");
		expect(record(out.writesAfterOmp166)).toMatchObject({ created: 1, addNow: 1, removeNow: 0, closed: 0 });
		expect(out.unlinkedBatchRefusal).toContain('intake_decomposition_required: batch entry [2] "Child 2" has no blocking relations; publish as a separate single-issue blueprint');
		expect(record(out.writesAfterUnlinkedBatch)).toMatchObject({ created: 1, addNow: 1, removeNow: 0, closed: 0 });
		expect(out.linkedBatchPreview).toContain("Model wants to publish a BATCH — 1 parent + 3 children");
		expect(out.linkedBatchConfirmed).toContain("HOME-2 + 3 child(ren)");
		expect(record(out.writesAfterLinkedBatch)).toMatchObject({ created: 5, addNow: 1, removeNow: 0, closed: 0 });
		expect(out.second).toContain("created HOME-6");
		expect(out.staleBlueprintRefusal).toContain("intake_blueprint_mismatch: description must exactly match local://intake-second.md");
		expect(out.publishConfirmed).toContain("created HOME-7");
		expect(record(out.writes)).toMatchObject({ created: 7, addNow: 2, removeNow: 0, closed: 0 });
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
		// OMP-155: the receipt body is the exact approved plan bytes.
		expect(out.firstReceiptBody, "receipt body is the exact submitted plan").toBe(out.submittedPlan);
		expect(Bun.SHA256.hash(String(out.firstReceiptBody), "hex"), "stored bytes hash to the approved plan sha").toBe(out.hashA);
		const getWork = String(out.firstGetWork);
		expect(getWork).toContain("plan body (exact stored bytes):");
		expect(getWork, "nested bullet the old summary dropped survives").toContain("- nested detail the summary dropped");
		expect(getWork, "extra section the old summary dropped survives").toContain("## Assumptions & contingencies");
		expect(getWork, "multibyte content survives byte-exact").toContain("café rollback stays reversible");
		expect(record(out.oversized).cancel, "oversized valid plan is refused at approval").toBe(true);
		expect(String(record(out.oversized).reason)).toContain("over the 32768-byte limit; shorten the plan or acceptance criteria");
		expect(out.planReceiptsAfterOversized, "oversized approval writes no plan receipt").toBe(2);
		expect(out.commentsAfterOversized, "oversized approval writes no stamp comment").toBe(2);
		expect(out.candidateAfterOversized, "oversized approval mints no candidate").toBe(out.candidateAfterChanged);
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

	test("summary requires a current plan and literal owner invocation", () => {
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

	test("refused close-attempt begin keeps review writes locked until a later /summary applies one", () => {
		const out = run("summary-begin-refused");
		// First /summary: exactly one begin, refused with no attempt — verification
		// stays locked and persists no receipt.
		expect(out.beginCallsAfterFirst, "first /summary issued exactly one begin").toBe(1);
		expect(out.firstVerify, "a refused begin must name the actual recognized /summary failure").toContain(
			"/summary was recognized but did not complete: the ledger refused the close attempt (plan_receipt_missing)",
		);
		expect(out.verificationReceiptsAfterFirst, "no verification receipt persisted after the refused begin").toBe(0);
		// Second /summary in the same session recovers: a second begin applies and
		// verification seals the audit manifest.
		expect(out.beginCallsAfterSecond, "second /summary issued a second begin").toBe(2);
		expect(out.secondVerify).toContain("audit manifest sealed");
	});

	test("a summary refusal survives a fresh session and clears after a successful retry (OMP-137)", () => {
		const out = run("summary-refusal-durable");
		expect(out.beginCallsAfterFirst, "first /summary issued exactly one begin").toBe(1);
		// Fresh session: the persisted refusal re-enters model context with the
		// COMPLETE service-rendered event text, not a paraphrase.
		expect(String(out.noticeAfterRestart)).toContain("The last /summary on HOME-1 was refused and is still unresolved");
		expect(String(out.noticeAfterRestart)).toContain("plan_receipt_missing: mock");
		// Successful retry resolves it: the next fresh session carries no refusal notice.
		expect(out.beginCallsAfterRetry, "retry /summary issued a second begin").toBe(2);
		expect(String(out.noticeAfterRetry)).not.toContain("was refused and is still unresolved");
	});

	test("an invalid staged rider batch refusal survives a fresh session and clears after a successful retry (OMP-149)", () => {
		const out = run("summary-rider-refusal-durable");
		expect(out.beginCallsAfterFirst, "invalid staged batch must refuse before issuing a begin").toBe(0);
		// Fresh session: the persisted refusal retains the specific host
		// validator error rather than the old generic rider-batch category.
		expect(String(out.noticeAfterRestart)).toContain("The last /summary on HOME-1 was refused and is still unresolved");
		expect(String(out.noticeAfterRestart)).toContain("staged batch must be mode 0600 exactly");
		expect(String(out.noticeAfterRestart)).not.toContain("the staged rider batch was rejected");
		// Removing the invalid batch permits a clean begin and resolves the refusal.
		expect(out.beginCallsAfterRetry, "retry /summary issued its first begin").toBe(1);
		expect(String(out.noticeAfterRetry)).not.toContain("was refused and is still unresolved");
	});

	test("the closeout continuation fires only from a live audited attempt (OMP-134)", () => {
		const out = run("stop-continuation-states");
		// active / audit_ready / auditor_in_flight: no continuation, obligation
		// stays armed and unblocked for a later valid audited state.
		expect(out.stopWhileActive).toBeNull();
		expect(out.stopWhileAuditReady).toBeNull();
		expect(out.stopWhileInFlight).toBeNull();
		// An unreadable workflow stays silent and retryable.
		expect(out.stopWhileUnreadable).toBeNull();
		// audited: exactly one continuation carrying the service-rendered event.
		const fired = record(out.stopWhenAudited);
		expect(fired.continue).toBe(true);
		expect(String(fired.additionalContext)).toContain('kind:"closeout"');
		expect(String(fired.additionalContext)).toContain("auditor_launch_settled");
		expect(out.stopAfterFired).toBeNull();
	});

	test("the atomic same-session filing refuses authority fields pre-preview and lands one command (OMP-139)", () => {
		const out = run("atomic-child");
		// Unsupported authority fields are refused BEFORE any preview or write.
		expect(String(out.rejectBatch)).toContain("rejects batch");
		expect(String(out.rejectQueue)).toContain("rejects queue");
		expect(String(out.rejectQuestion)).toContain("rejects question");
		expect(String(out.rejectProject)).toContain("rejects project");
		expect(String(out.rejectKind)).toContain("atomic same-session form");
		expect(String(out.rejectNoWork)).toContain("existing parent key");
		expect(String(out.rejectNoBody)).toContain("## Finding");
		expect(String(out.rejectHalfSections)).toContain("## Finding");
		expect(out.postsDuringRejections, "no service command during rejections").toBe(0);
		expect(out.sscCallsAfterRejections).toBe(0);
		expect(out.confirmUiDuringRejections, "no confirm dialog during rejections").toBe(0);
		// Two-phase preview names parent, child title, finding, and verification.
		expect(String(out.preview)).toContain("CONFIRM REQUIRED");
		expect(String(out.preview)).toContain("HOME-1");
		expect(String(out.preview)).toContain('"atomic child"');
		expect(String(out.preview)).toContain("bug found in-session");
		expect(String(out.preview)).toContain("fix proven in-session");
		// One confirmed filing emits exactly one service command.
		expect(String(out.confirmed)).toContain("created HOME-77 as a same-session child of HOME-1");
		expect(out.sscCallsAfterConfirm).toBe(1);
		const payload = record(out.sscPayload);
		expect(payload.parent_work_id).toBe("id-1");
		expect(payload.owner_session_id).toBe("session-test");
		expect(payload.finding).toBe("bug found in-session");
		expect(payload.verification).toBe("fix proven in-session");
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

	test("closed work never becomes or stays NOW (owner ruling 2026-08-25)", () => {
		const out = run("now-canceled");
		// A focus slot left pointing at canceled work is never resurrected.
		expect(out.now).toBe("NOW unset");
		// set_now on a canceled key refuses before any owner prompt or focus write.
		expect(String(out.refusal)).toContain("can't be NOW");
		// The literal keyed /now command hits the shared setNow guard.
		expect(String(out.nowCommandNotices)).toContain("can't be NOW");
		expect(out.addNowWrites).toBe(0);
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

	test("a literal /skill:summary recovers from a refused freeze and re-authorizes", () => {
		const out = run("summary-reauth");
		expect(out.beginAfterRefused, "a refused freeze begins nothing").toBe(0);
		// The state that used to deadlock: recognized invocation, no begun attempt.
		// Another literal /skill:summary must recover after remediation.
		expect(out.beginAfterSkillRetry, "literal retry begins a fresh attempt").toBe(1);
		expect(out.beginAfterUnrelated, "unrelated owner messages never authorize").toBe(1);
		// The /summary alias also re-authorizes; the service supersedes to keep
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

		// Descendant drift (fixes on top of commitA)
		const descNotice = String(out.descendantDriftNotice);
		expect(descNotice).toContain("Code changed after the reviewed snapshot. Run /plan to approve the current code, then run /summary.");
		expect(descNotice).toContain(`Details: reviewed commit ${out.commitA}; current commit ${out.commitB}.`);
		expect(descNotice.toLowerCase()).not.toContain("restore");
		expect(out.beginCallsAfterDescendant, "descendant candidate begins no new close attempt").toBe(out.beginCallsBeforeDrift);
		expect(out.pushReceiptsAfterDescendant, "descendant candidate mints no push receipt").toBe(out.pushReceiptsBeforeDrift);
		expect(out.headAfterDescendant, "descendant summary mutates no HEAD commit").toBe(out.commitB);
		expect(out.dirtyAfterDescendant, "descendant summary mutates no working tree").toBe("");

		// Unrelated drift (orphan branch)
		const unrelNotice = String(out.unrelatedDriftNotice);
		expect(unrelNotice).toContain("Current code is on a different history from the reviewed snapshot. Restore the reviewed snapshot, or run /plan to approve the current code and then run /summary.");
		expect(unrelNotice).toContain(`Details: reviewed commit ${out.commitA}; current commit ${out.commitOrphan}.`);
		expect(out.beginCallsAfterUnrelated, "unrelated candidate begins no new close attempt").toBe(out.beginCallsBeforeDrift);
		expect(out.pushReceiptsAfterUnrelated, "unrelated candidate mints no push receipt").toBe(out.pushReceiptsBeforeDrift);
		expect(out.headAfterUnrelated, "unrelated summary mutates no HEAD commit").toBe(out.commitOrphan);
		expect(out.dirtyAfterUnrelated, "unrelated summary mutates no working tree").toBe("");
	});
	test("summary persists service-selected final candidate identity on reuse", () => {
		const out = run("summary-final-reuse");
		expect(out.carrierCandidateId).toBe("reused-final-candidate");
	});


	test("the ledger-sealed audit task drives one exact-byte bounded launch (OMP-168)", () => {
		const out = run("audit");
		// Pre-summary close-ritual writes are refused.
		expect(out.unauthorized, "closeout is a close-ritual kind").toContain("literally enter /summary");
		// The literal /summary began ONE ledger attempt with host-computed identity.
		expect(out.beginCallsAfterNearMiss, "similarly prefixed skills do not authorize closeout").toBe(0);
		expect(out.beginCallsAfterRewrite, "input rewrites cannot mint closeout authority").toBe(0);
		expect(out.beginCallsBeforeSummaryMessage, "/skill:summary authorizes before prompt streaming").toBe(1);
		expect(out.beginCalls).toBe(1);
		expect(record(out.beginSession)).toEqual({ hasStartCommit: true, hasDiffSha: true, hasAuthorization: true });
		// Verification append sealed the manifest; get_work renders the next action banner without task body bytes.
		expect(out.verify).toContain("audit manifest sealed");
		expect(out.nextActionInGetWork).toBe(true);
		expect(out.getWorkStartsWithStatus).toBe(true);
		expect(out.nextActionCount).toBe(1);
		expect(out.noSealedTaskBytes).toBe(true);
		expect(String(out.refusedWhileAuditReady).startsWith("STATUS: CLOSE ATTEMPT audit_ready")).toBe(true);
		expect(String(out.refusedWhileAuditReady)).toContain('NEXT REQUIRED ACTION: work action:"run_audit", work:"HOME-1"');
		// The service minted the audit receipt itself and the outcome reached the owner as an attested checkpoint.
		expect(out.settlePayload).toContain("VERDICT: PASS");
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

	test("get_work renders exhaustive next action banners for live states (OMP-168)", () => {
		const out = run("omp140-audit-states");
		expect(String(out.noAttemptGetWork)).not.toContain("STATUS: CLOSE ATTEMPT");
		expect(String(out.noAttemptGetWork)).not.toContain("SEALED AUDITOR TASK");
		expect(String(out.activeGetWork).indexOf("STATUS: CLOSE ATTEMPT active")).toBe(0);
		expect((String(out.activeGetWork).match(/NEXT REQUIRED ACTION:/g) || []).length).toBe(1);
		expect(String(out.activeGetWork)).toContain('NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"verification"');
		expect(String(out.activeGetWork)).toContain('BLOCKED ACTIONS: run_audit, append_evidence kind:"closeout", /done');
		// OMP-168 remediation: a foreign/nonexistent explicit target must not
		// suppress or redirect HOME-1's live NOW recovery banner on the refusal.
		expect(String(out.foreignWorkRefusal).indexOf("STATUS: CLOSE ATTEMPT active")).toBe(0);
		expect((String(out.foreignWorkRefusal).match(/NEXT REQUIRED ACTION:/g) || []).length).toBe(1);
		expect(String(out.foreignWorkRefusal)).toContain('NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"verification"');
		expect(String(out.foreignWorkRefusal)).toContain('BLOCKED ACTIONS: run_audit, append_evidence kind:"closeout", /done');
		expect(String(out.foreignWorkRefusal)).toContain('REFUSED — named work item "HOME-404" must be current NOW ("HOME-1").');
		expect(String(out.auditReadyGetWork).indexOf("STATUS: CLOSE ATTEMPT audit_ready")).toBe(0);
		expect((String(out.auditReadyGetWork).match(/NEXT REQUIRED ACTION:/g) || []).length).toBe(1);
		expect(String(out.auditReadyGetWork)).toContain('NEXT REQUIRED ACTION: work action:"run_audit", work:"HOME-1"');
		expect(String(out.auditReadyGetWork)).toContain('BLOCKED ACTIONS: append_evidence kind:"closeout", /done');
		expect(String(out.auditedGetWork).indexOf("STATUS: CLOSE ATTEMPT audited")).toBe(0);
		expect((String(out.auditedGetWork).match(/NEXT REQUIRED ACTION:/g) || []).length).toBe(1);
		expect(String(out.auditedGetWork)).toContain('NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"closeout"');
		expect(String(out.auditedGetWork)).toContain("BLOCKED ACTIONS: run_audit, /done");
		expect(String(out.closeoutRequestedGetWork).indexOf("STATUS: CLOSE ATTEMPT closeout_requested")).toBe(0);
		expect((String(out.closeoutRequestedGetWork).match(/NEXT REQUIRED ACTION:/g) || []).length).toBe(1);
		expect(String(out.closeoutRequestedGetWork)).toContain("NEXT REQUIRED ACTION: owner /done closes this work");
		expect(String(out.closeoutRequestedGetWork)).toContain("BLOCKED ACTIONS: run_audit, append_evidence");
	});

	test("audited attempt survives restart, resumes cleanly without audit duplication, and enables fresh-session /done (OMP-140)", () => {
		const out = run("omp140-restart-flow");
		// Session 2 start: digestExtras and centerSnapshot reflect WorkService audited state
		const session2Extras = list(out.session2Extras);
		expect(session2Extras.some(l => l.includes("CLOSE ATTEMPT: audited — PASS audit saved; enter /summary to resume close review—nothing will be erased"))).toBe(true);
		const session2Center = record(out.session2Center);
		const recs2 = session2Center.recommendations as Array<{ command: string; reason: string }>;
		expect(recs2.some(r => r.command === "/summary" && r.reason.includes("PASS audit saved"))).toBe(true);

		// Resuming /summary under session 2 re-authorizes cleanly and records closeout review
		expect(out.beginCallsAfterResume).toBe(2);
		expect(String(out.session2Review)).toContain("closeout receipt recorded on HOME-1");
		expect(out.attemptStateAfterReview).toBe("closeout_requested");

		// Session 3 start: digestExtras and centerSnapshot reflect closeout_requested state
		const session3Extras = list(out.session3Extras);
		expect(session3Extras.some(l => l.includes("CLOSE ATTEMPT: closeout_requested — close review saved; enter /done"))).toBe(true);
		const session3Center = record(out.session3Center);
		const recs3 = session3Center.recommendations as Array<{ command: string; reason: string }>;
		expect(recs3.some(r => r.command === "/done" && r.reason.includes("closeout review complete"))).toBe(true);

		// Fresh-session /done completes the work item directly
		expect(out.doneState).toBe("DONE");
	});

	test("pending checkpoint delivery blocks /done and renders honest bookend guidance (OMP-140)", () => {
		const out = run("omp140-failed-checkpoint");
		const extras = list(out.extrasWithPending);
		expect(extras.some(l => l.includes("CHECKPOINT DELIVERY PENDING (1): /done remains blocked until delivered or owner-waived."))).toBe(true);
	});

	test("terminal guidance persists across checkpoint deliveries and refusals (OMP-140)", () => {
		const out = run("omp140-terminal-guidance");
		// NEEDS_FIX settlement event
		const settleEvent = record(out.settleEvent);
		expect(settleEvent.legal_next_actions).toEqual([
			"fix the findings",
			"after fixing: if code changed, enter /plan then /summary; otherwise enter /summary",
		]);
		expect(String(settleEvent.rendered_text)).toContain(
			"next: fix the findings; after fixing: if code changed, enter /plan then /summary; otherwise enter /summary",
		);
		expect(settleEvent.requires_fresh_authorization).toBe(true);

		// Unchanged HEAD re-entry guidance
		const unchangedExtras = list(out.terminalExtras);
		expect(
			unchangedExtras.some(l =>
				l.includes("CLOSE ATTEMPT: remediation_required (needs_fix) — code still matches the reviewed snapshot — enter /summary for a fresh attempt"),
			),
		).toBe(true);

		// Descendant remediation commit re-entry guidance
		const descendantExtras = list(out.descendantExtras);
		expect(
			descendantExtras.some(l =>
				l.includes("CLOSE ATTEMPT: remediation_required (needs_fix) — code changed since the reviewed snapshot — enter /plan, then /summary"),
			),
		).toBe(true);

		// Sibling terminal state (budget_exhausted) receives descendant guidance
		const budgetExtras = list(out.budgetExtras);
		expect(
			budgetExtras.some(l =>
				l.includes("CLOSE ATTEMPT: budget_exhausted (auditor_budget_exhausted) — code changed since the reviewed snapshot — enter /plan, then /summary"),
			),
		).toBe(true);
	});
});

describe("OMP-154 ledger reads and closeout recovery", () => {
	test("provides bounded project ledger reads and detailed receipt context", () => {
		const out = run("ledger-reads");
		const candidate = "aaaaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee";
		const revision = "11111111-2222-7333-8444-555555555555";
		const commit = "f".repeat(40);
		expect(out.missing).toContain("project required");
		expect(out.unknown).toContain('project "No Such Project" does not exist');
		expect(out.empty).toContain('Project "Empty Surface" — no open items');
		expect(out.listing).toBe("HOME-1 | IN_PROGRESS | First\nHOME-2 | BACKLOG | Zulu open item");
		const detail = String(out.detail);
		expect(detail).toContain("RECEIPTS:");
		// Exact full-width receipt lines: any identity truncation or dropped
		// optional-verdict token fails these, not just substring presence.
		expect(detail).toContain(`handoff 2026-08-26T05:00:00 candidate ${candidate} revision ${revision} ${"1".repeat(12)}`);
		expect(detail).toContain(`verification 2026-08-26T05:01:00 candidate ${candidate} revision ${revision} ${"2".repeat(12)}`);
		expect(detail).toContain(`audit 2026-08-26T05:02:00 candidate ${candidate} revision ${revision} PASS ${"5".repeat(12)}`);
		expect(detail).toContain(`CLOSE ATTEMPT: state audit_ready · candidate ${candidate} · commit ${commit} · launches left 3 · reports left 2`);
		expect(detail).toContain("STATUS: CLOSE ATTEMPT audit_ready");
		expect(detail).toContain('NEXT REQUIRED ACTION: work action:"run_audit", work:"HOME-1"');
		expect(detail).not.toContain("----- SEALED AUDITOR TASK BEGIN -----");
	});

	test("allows bounded ledger reads from a subagent while refusing writes", () => {
		const out = run("ledger-reads-subagent");
		expect(out.listing).toBe("HOME-1 | IN_PROGRESS | First");
		expect(out.writeRefusal).toContain("owner-session");
	});

	test("queues an undelivered checkpoint before recording closeout and recovers on retry", () => {
		const out = run("closeout-pending-recovery");
		expect(out.first).toContain("closeout receipt not recorded yet — 1 pending checkpoint(s) queued");
		expect(out.first).toContain("attempt_superseded");
		expect(out.closeoutReceiptsAfterFirst).toBe(0);
		expect(out.attemptStateAfterFirst).toBe("audited");
		expect(list(out.staleDeliveries)).toEqual(["delivered"]);
		expect(out.waivedCount).toBe(0);
		expect(out.second).toContain("closeout receipt recorded on HOME-1");
		expect(out.second).toContain("Yield the turn now");
		expect(out.closeoutReceipts).toBe(1);
		expect(out.attemptState).toBe("closeout_requested");
		expect(out.newCheckpointDelivered).toBe(true);
		expect(out.totalCloseoutReceipts).toBe(1);
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
