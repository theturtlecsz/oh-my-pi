import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-workflow-"));
const harness = path.join(import.meta.dir, "fixtures/workflow-sequence-harness.ts");

afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

function run(mode: "intake" | "plan" | "summary" | "summary-subagent" | "done" | "footer" | "audit" | "restore"): Record<string, unknown> {
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
	Bun.spawnSync(["git", "init", "-q"], { cwd: probe });
	Bun.spawnSync(["git", "config", "user.email", "test@example.com"], { cwd: probe });
	Bun.spawnSync(["git", "config", "user.name", "Test"], { cwd: probe });
	fs.writeFileSync(path.join(probe, "init.txt"), "init\n");
	Bun.spawnSync(["git", "add", "init.txt"], { cwd: probe });
	Bun.spawnSync(["git", "commit", "-q", "-m", "init"], { cwd: probe });
	Bun.spawnSync(["git", "remote", "add", "origin", remote], { cwd: probe });
	Bun.spawnSync(["git", "push", "-q", "-u", "origin", "HEAD:main"], { cwd: probe });
	const child = Bun.spawnSync([process.execPath, harness, probe, mode], {
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
	});

	test("subagent summary provenance cannot authorize the review", () => {
		const out = run("summary-subagent");
		expect(out.beforeInvocation).toContain("literally enter /summary");
		expect(out.afterPaste).toContain("literally enter /summary");
		expect(out.afterStructured).toContain("literally enter /summary");
		expect(list(out.reviewBodies)).toHaveLength(0);
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
		const doneUi = list(out.doneUi);
		expect(doneUi.filter(call => call.startsWith("confirm:This is your verdict"))).toHaveLength(1);
		expect(doneUi.some(call => call.startsWith("select:"))).toBe(false);
		expect(record(out.doneWrites)).toMatchObject({ closed: 1, removeNow: 1, verdictComments: 1 });
		expect(out.now).toBe("NOW unset");
		expect(record(out.afterSecondDone)).toMatchObject({ closed: 1, removeNow: 1 });
	});

	test("the audit bridge binds verbatim bytes to exactly one recorded receipt", () => {
		const out = run("audit");
		expect(out.spawnBlocked, "the auditor spawn clears the gate").toBe(false);
		expect(out.unauthorized, "audit is a close-ritual kind").toContain("literally enter /summary");
		expect(out.edited, "edited bytes never match the receipt").toContain("no fresh auditor receipt matches");
		expect(out.exact).toContain("audit receipt recorded on HOME-1 (verdict PASS)");
		expect(out.replay, "the receipt is consumed by the first match").toContain("no fresh auditor receipt matches");
		expect(out.auditBodies, "exactly one audit comment landed").toBe(1);
		expect(out.repeatSummaryNotice).toContain("No plan is stamped on this work");
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
});
