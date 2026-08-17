// HOME-131 model bookends: /intake auto-routing to the intake role and the
// fail-closed /summary audit gate (one fresh auditor, five required input
// sections, full report structure, verbatim report forwarding before settlement).
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import {
	AUDIT_CONTRACT,
	decodeJsonQuoted,
	extractAuditReport,
	missingAuditSections,
	missingReportParts,
	parseAuditVerdict,
	reportForwarded,
} from "../extensions/model-bookends";
const repoRoot = path.resolve(import.meta.dir, "../..");
const extPath = path.join(repoRoot, "session-system/extensions/model-bookends.ts");

const FULL_AUDIT_TASK = [
	"Approved plan: add three install links to install.sh and mirror them in install.test.ts LINKS.",
	"Acceptance criteria: AC-1 installer places all three artifacts; AC-2 LINKS mirrors them; AC-3 installer stays idempotent.",
	"Starting state: commit abc123; pre-existing dirty files: none.",
	"Final diff:",
	"```diff",
	"--- a/session-system/install.sh",
	"+++ b/session-system/install.sh",
	"@@ -19,0 +20,3 @@",
	'+place extensions/model-bookends.ts "$HOME/.omp/agent/extensions/model-bookends.ts"',
	"```",
	"Verification: bun test session-system/tests/install.test.ts → 2 pass, 0 fail.",
].join("\n");

const report = (verdict: string, findings = "(none)") =>
	[
		`VERDICT: ${verdict}`,
		"",
		"FINDINGS",
		findings,
		"",
		"ACCEPTANCE COVERAGE",
		"| AC-1 | met | tests |",
		"",
		"OUT OF SCOPE",
		"none",
		"",
		"CHECKS RUN",
		"bun test → 10 pass",
		"",
		"REMAINING QUESTIONS",
		"none",
	].join("\n");

const PASS_REPORT = report("PASS");

interface Harness {
	runner: ExtensionRunner;
	setModelCalls: Array<{ provider: string; id: string }>;
	thinkingLevels: string[];
	notifies: string[];
}

async function makeHarness(depth = 0, opts: { intakeConfigured?: boolean; hasCredential?: boolean; auditRole?: string } = {}): Promise<Harness> {
	const { intakeConfigured = true, hasCredential = true, auditRole = "openai/gpt-5.2" } = opts;
	const result = await loadExtensions([extPath], repoRoot);
	if (result.errors.length > 0) throw new Error(result.errors.map(e => e.error).join("; "));
	const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
	// HOME-147: the audit gate refuses same-family auditors — the harness session
	// runs on a different provider family than @audit resolves to.
	const gptModel = { id: "gpt-5.2", provider: "openai", name: "GPT 5.2", api: "openai-responses" };
	const fakeRegistry = { getAvailable: () => [fableModel, gptModel], hasProvider: () => true };
	const fakeSettings = {
		getModelRole: (role: string) => {
			if (intakeConfigured && role === "intake") return "anthropic/claude-fable-5:high";
			if (role === "audit") return auditRole;
			return undefined;
		},
		get: () => undefined,
		getStorage: () => undefined,
	};
	const runner = new ExtensionRunner(
		result.extensions,
		result.runtime,
		repoRoot,
		{ getCwd: () => repoRoot, getBranch: () => [] } as never,
		fakeRegistry as never,
		undefined,
		fakeSettings as never,
		undefined,
		undefined,
		depth,
	);
	const setModelCalls: Array<{ provider: string; id: string }> = [];
	const thinkingLevels: string[] = [];
	const notifies: string[] = [];
	runner.initialize(
		{
			getCommands: () => [],
			setModel: async (model: { provider: string; id: string }) => {
				setModelCalls.push({ provider: model.provider, id: model.id });
				return hasCredential;
			},
			getThinkingLevel: () => "high",
			setThinkingLevel: (level: string) => {
				thinkingLevels.push(level);
			},
		} as never,
		{
			getModel: () => fableModel,
			isIdle: () => true,
			abort: () => {},
			hasPendingMessages: () => false,
			shutdown: () => {},
			getSystemPrompt: () => [],
		} as never,
		undefined,
		{
			theme: { fg: (_c: string, t: string) => t },
			setStatus: () => {},
			notify: (msg: string) => notifies.push(msg),
		} as never,
	);
	await runner.emit({ type: "session_start" } as never);
	return { runner, setModelCalls, thinkingLevels, notifies };
}

const taskCall = (id: string, input: Record<string, unknown>) =>
	({ type: "tool_call", toolName: "task", toolCallId: id, input }) as never;
const auditorCall = (id: string, task: unknown = FULL_AUDIT_TASK) =>
	taskCall(id, { context: "audit the completed work", tasks: [{ agent: "auditor", task }] });
const workForward = (id: string, body: string) =>
	({ type: "tool_call", toolName: "work", toolCallId: id, input: { action: "append_evidence", kind: "audit", body } }) as never;
/** Mirrors the real task tool result: content wraps the report in <task-result>
 *  transport metadata; details.results[0].output carries the exact report
 *  (observed in the HOME-131 dry-run transcript). */
const taskResult = (id: string, text: string, isError = false) =>
	({
		type: "tool_result",
		toolName: "task",
		toolCallId: id,
		input: {},
		content: [
			{
				type: "text",
				text: `<task-result id="Aud" agent="auditor" status="completed" duration="28s">\n<meta lines="9" size="1KB" />\n<output>\n${text}\n</output>\n</task-result>`,
			},
		],
		details: isError ? undefined : { results: [{ output: text }] },
		isError,
	}) as never;
const workResult = (id: string, body: string, success = true) =>
	({
		type: "tool_result",
		toolName: "work",
		toolCallId: id,
		input: { action: "append_evidence", kind: "audit", body },
		content: [{ type: "text", text: success ? "ok" : "REFUSED" }],
		details: { success },
		isError: false,
	}) as never;
const sessionStop = () => ({ type: "session_stop", messages: [], turn_id: 1 }) as never;

async function armSummary(h: Harness): Promise<void> {
	await h.runner.emitInput("/summary", undefined, "interactive");
}

/** Run a complete valid audit: spawn accepted, structured report returned. */
async function runAuditor(h: Harness, id = "aud-1", reportText = PASS_REPORT): Promise<void> {
	const call = await h.runner.emitToolCall(auditorCall(id));
	expect(call?.block).toBeUndefined();
	await h.runner.emitToolResult(taskResult(id, reportText));
}

describe("helpers", () => {
	test("missingAuditSections accepts headings but rejects incidental label prose", () => {
		expect(missingAuditSections(FULL_AUDIT_TASK)).toEqual([]);
		const standaloneHeadings = [
			"The approved plan discusses acceptance criteria, starting state, and final diff before the real input sections.",
			"Approved plan",
			"Anchor audit headings so incidental prose cannot start a section body.",
			"Acceptance criteria",
			"Accept exact unindented labels while rejecting incidental prose references.",
			"Starting state",
			"commit abc123; pre-existing dirty files: none; working tree otherwise clean.",
			"Final diff",
			"```diff",
			"--- a/session-system/extensions/model-bookends.ts",
			"+++ b/session-system/extensions/model-bookends.ts",
			"@@ -28,5 +28,5 @@",
			"+Verification results must remain prose inside this unified diff.",
			"```",
			"Verification",
			"bun test session-system/tests/model-bookends.test.ts → 24 pass, 0 fail.",
		].join("\n");
		expect(missingAuditSections(standaloneHeadings)).toEqual([]);
		expect(
			missingAuditSections(
				"The approved plan satisfies acceptance criteria from the starting state through the final diff after verification results.",
			),
		).toEqual([
			"Approved plan",
			"Acceptance criteria",
			"Starting state (commit + pre-existing dirty files)",
			"Final diff",
			"Verification results",
		]);
		const missing = missingAuditSections(
			"Approved plan: rewrite the frobnicator config end to end.\nFinal diff: the full diff follows below verbatim.",
		);
		expect(missing).toContain("Acceptance criteria");
		expect(missing).toContain("Starting state (commit + pre-existing dirty files)");
		expect(missing).toContain("Verification results");
		expect(missing).toHaveLength(3);
	});

	test("missingAuditSections rejects labels with empty or token-thin bodies", () => {
		// all five labels present, every body empty — nothing to audit
		const emptyBodies = "Approved plan:\nAcceptance criteria:\nStarting state:\nFinal diff:\nVerification:";
		const missing = missingAuditSections(emptyBodies);
		expect(missing).toHaveLength(5);
		for (const entry of missing) expect(entry).toContain("label present but no content");
		// four real sections but a bare "Final diff:" pointer with no content
		const noDiff = FULL_AUDIT_TASK.replace(/Final diff:[\s\S]*?```\n/, "Final diff:\n");
		expect(missingAuditSections(noDiff)).toEqual(["Final diff (label present but no content)"]);
	});

	test("parseAuditVerdict extracts each verdict and rejects unstructured text", () => {
		expect(parseAuditVerdict(PASS_REPORT)).toBe("PASS");
		expect(parseAuditVerdict(report("NEEDS_FIX", "- broken"))).toBe("NEEDS_FIX");
		expect(parseAuditVerdict(report("BLOCKED", "missing diff"))).toBe("BLOCKED");
		expect(parseAuditVerdict("looks good to me!")).toBeUndefined();
	});

	test("missingReportParts enforces real shape, not token presence", () => {
		expect(missingReportParts(PASS_REPORT)).toEqual([]);
		expect(missingReportParts(report("PASS", ""))).toEqual([]);
		const verdictOnly = missingReportParts("VERDICT: PASS\nall good");
		expect(verdictOnly).toEqual(["FINDINGS", "ACCEPTANCE COVERAGE", "OUT OF SCOPE", "CHECKS RUN", "REMAINING QUESTIONS"]);
		expect(missingReportParts("FINDINGS\nACCEPTANCE COVERAGE\nOUT OF SCOPE\nCHECKS RUN\nREMAINING QUESTIONS")).toEqual([
			"VERDICT: PASS | NEEDS_FIX | BLOCKED (must be first)",
			"FINDINGS (label present but no content)",
			"ACCEPTANCE COVERAGE (label present but no content)",
			"OUT OF SCOPE (label present but no content)",
			"CHECKS RUN (label present but no content)",
			"REMAINING QUESTIONS (label present but no content)",
		]);
		// advisory regression: a one-line section-name echo must NOT pass
		const echo = "VERDICT: PASS\nfindings acceptance coverage out of scope checks run remaining questions";
		expect(missingReportParts(echo)).not.toEqual([]);
		expect(missingReportParts(PASS_REPORT.replace("FINDINGS", "FINDINGS (ordered by severity)"))).toContain("FINDINGS");
		expect(missingReportParts(report("NEEDS_FIX", "(none)"))).toContain("at least one finding under NEEDS_FIX");
		expect(missingReportParts(report("NEEDS_FIX", "- none"))).toContain("at least one finding under NEEDS_FIX");
		expect(missingReportParts(report("NEEDS_FIX", "- [P0] AC-1 src/x.ts:3 — evidence: failure"))).toContain(
			"at least one finding under NEEDS_FIX",
		);
		expect(
			missingReportParts(
				report("NEEDS_FIX", "- [P0] AC-1 src/x.ts:3 — evidence: failure\n- impact: broken; minimal fix: guard"),
			),
		).toContain("at least one finding under NEEDS_FIX");
		expect(
			missingReportParts(
				report("NEEDS_FIX", "- [P0] AC-1 src/x.ts:3 — evidence: failure; impact: broken; minimal fix: guard"),
			),
		).toEqual([]);
		expect(missingReportParts(report("NEEDS_FIX", "- [0] AC-1 src/x.ts:3 — evidence: failure"))).toContain(
			"at least one finding under NEEDS_FIX",
		);
	});

	test("missingReportParts validates the observed JSON report schema", () => {
		// live dry-run regression: the auditor's raw output was JSON with snake_case keys
		const jsonReport =
			'{"verdict": "PASS", "findings": [], "acceptance_coverage": [{"id": "AC-1", "status": "met", "evidence": "x"}], "out_of_scope": "none", "checks_run": ["bun test → pass"], "remaining_questions": "none"}';
		expect(missingReportParts(jsonReport)).toEqual([]);
		expect(parseAuditVerdict(jsonReport)).toBe("PASS");
		// key echo inside JSON is not enough: types are enforced
		expect(missingReportParts('{"verdict": "PASS", "findings": "fine", "acceptance_coverage": [], "out_of_scope": "n", "checks_run": {}, "remaining_questions": "n"}')).not.toEqual([]);
		// NEEDS_FIX without a single finding is unusable
		expect(
			missingReportParts(
				'{"verdict": "NEEDS_FIX", "findings": [], "acceptance_coverage": [{"id": "AC-1"}], "out_of_scope": "n", "checks_run": [], "remaining_questions": "n"}',
			),
		).toContain("at least one finding under NEEDS_FIX");
		expect(
			missingReportParts(
				'{"verdict": "NEEDS_FIX", "findings": ["none"], "acceptance_coverage": [{"id": "AC-1"}], "out_of_scope": "n", "checks_run": [], "remaining_questions": "n"}',
			),
		).toContain("at least one finding under NEEDS_FIX");
		expect(
			missingReportParts(
				'{"verdict": "NEEDS_FIX", "findings": [{"severity": "", "ac": "", "location": "", "evidence": ""}], "acceptance_coverage": [{"id": "AC-1"}], "out_of_scope": "n", "checks_run": [], "remaining_questions": "n"}',
			),
		).toContain("at least one finding under NEEDS_FIX");
		expect(
			missingReportParts(
				'{"verdict": "NEEDS_FIX", "findings": [{"severity": "P1", "ac": "AC-1", "location": "src/x.ts:3", "evidence": "failure"}], "acceptance_coverage": [{"id": "AC-1"}], "out_of_scope": "n", "checks_run": [], "remaining_questions": "n"}',
			),
		).toContain("at least one finding under NEEDS_FIX");
		// malformed JSON falls back to headed-text validation and fails it
		expect(missingReportParts('{"verdict": "PASS", broken')).not.toEqual([]);
	});

	test("reportForwarded requires the verbatim report, tolerating only outer whitespace/CRLF", () => {
		expect(reportForwarded(`Review intro\n\n${PASS_REPORT}\n\ncharter...`, PASS_REPORT)).toBe(true);
		expect(reportForwarded(PASS_REPORT.replace(/\n/g, "\r\n"), PASS_REPORT)).toBe(true);
		expect(reportForwarded("The auditor said PASS, all good.", PASS_REPORT)).toBe(false);
		expect(reportForwarded("", PASS_REPORT)).toBe(false);
	});

	test("extractAuditReport prefers details output, falls back to the <output> block", () => {
		const wrapped = `<task-result id="A" agent="auditor" status="completed" duration="1s">\n<output>\n${PASS_REPORT}\n</output>\n</task-result>`;
		expect(extractAuditReport({ results: [{ output: PASS_REPORT }] }, wrapped)).toBe(PASS_REPORT);
		expect(extractAuditReport(undefined, wrapped)).toBe(PASS_REPORT);
		expect(extractAuditReport(undefined, PASS_REPORT)).toBe(PASS_REPORT);
	});

	test("decodeJsonQuoted undoes exactly one transport quoting layer and leaves real text alone", () => {
		// HOME-137 regression: string-outputSchema runs delivered the whole report as a
		// JSON string literal on one physical line — captured live 2026-08-15.
		expect(decodeJsonQuoted(JSON.stringify(PASS_REPORT))).toBe(PASS_REPORT);
		expect(decodeJsonQuoted(JSON.stringify({ report: PASS_REPORT }))).toBe(PASS_REPORT);
		expect(decodeJsonQuoted(PASS_REPORT)).toBe(PASS_REPORT);
		expect(decodeJsonQuoted('"unterminated')).toBe('"unterminated');
		expect(extractAuditReport({ results: [{ output: JSON.stringify(PASS_REPORT) }] }, "")).toBe(PASS_REPORT);
		expect(missingReportParts(extractAuditReport({ results: [{ output: JSON.stringify(PASS_REPORT) }] }, ""))).toEqual([]);
	});
});

describe("/intake routing", () => {
	test("switches to the intake role at :high and forwards to /skill:intake", async () => {
		const h = await makeHarness();
		const res = await h.runner.emitInput("/intake new listing idea", undefined, "interactive");
		expect(res.text).toBe("/skill:intake new listing idea");
		expect(h.setModelCalls).toEqual([{ provider: "anthropic", id: "claude-fable-5" }]);
		expect(h.thinkingLevels).toEqual(["high"]);
	});

	test("explicit /skill:intake also switches the model", async () => {
		const h = await makeHarness();
		const res = await h.runner.emitInput("/skill:intake idea", undefined, "interactive");
		expect(res.text).toBeUndefined(); // already in skill form — no rewrite
		expect(h.setModelCalls).toEqual([{ provider: "anthropic", id: "claude-fable-5" }]);
	});

	test("subagent sessions (taskDepth > 0) are untouched", async () => {
		const h = await makeHarness(1);
		const res = await h.runner.emitInput("/intake idea", undefined, "interactive");
		expect(res.text).toBeUndefined();
		expect(h.setModelCalls).toEqual([]);
	});

	test("fails closed when @intake cannot resolve — input consumed, skill never dispatched", async () => {
		const h = await makeHarness(0, { intakeConfigured: false });
		const res = await h.runner.emitInput("/intake idea", undefined, "interactive");
		expect(res.handled).toBe(true);
		expect(res.text).toBeUndefined();
		expect(h.setModelCalls).toEqual([]);
		expect(h.thinkingLevels).toEqual([]);
		expect(h.notifies.some(m => m.includes("/intake refused"))).toBe(true);
	});

	test("fails closed when setModel reports no credential — input consumed, no effort change", async () => {
		const h = await makeHarness(0, { hasCredential: false });
		const res = await h.runner.emitInput("/intake idea", undefined, "interactive");
		expect(res.handled).toBe(true);
		expect(res.text).toBeUndefined();
		expect(h.thinkingLevels).toEqual([]);
		expect(h.notifies.some(m => m.includes("no credential"))).toBe(true);
	});
});

describe("audit gate", () => {
	test("unarmed sessions leave auditor spawns alone (dry-run path)", async () => {
		const h = await makeHarness();
		const res = await h.runner.emitToolCall(auditorCall("t1", "no sections at all"));
		expect(res?.block).toBeUndefined();
	});

	test("armed: injects the audit contract exactly once", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const first = await h.runner.emitBeforeAgentStart("/summary", undefined, []);
		expect(first?.messages?.some(m => typeof m === "object" && m.content === AUDIT_CONTRACT)).toBe(true);
		const second = await h.runner.emitBeforeAgentStart("next turn", undefined, []);
		expect(second).toBeUndefined();
	});

	test("armed: blocks an auditor task missing required sections, naming them", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const res = await h.runner.emitToolCall(auditorCall("t1", "Approved plan: x. Final diff: y."));
		expect(res?.block).toBe(true);
		expect(res?.reason).toContain("Acceptance criteria");
		expect(res?.reason).toContain("Verification results");
	});

	test("armed: leaves ordinary batches alone and accepts a singleton auditor batch", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const ordinary = await h.runner.emitToolCall(taskCall("t0", { context: "c", tasks: [{ agent: "scout", task: "inspect" }] }));
		expect(ordinary?.block).toBeUndefined();
		expect((await h.runner.emitToolCall(auditorCall("t1")))?.block).toBeUndefined();
	});

	test("armed: refuses a same-family auditor before spawn (HOME-147 independence)", async () => {
		const h = await makeHarness(0, { auditRole: "anthropic/claude-fable-5" });
		await armSummary(h);
		const res = await h.runner.emitToolCall(auditorCall("t1"));
		expect(res?.block).toBe(true);
		expect(res?.reason).toContain("same model family");
		// the refused spawn must not consume the one-auditor slot
		expect((await h.runner.emitToolCall(auditorCall("t2")))?.block).toBe(true);
	});

	test("armed: fails closed when @audit cannot be resolved", async () => {
		const h = await makeHarness(0, { auditRole: "openai/not-registered" });
		await armSummary(h);
		const res = await h.runner.emitToolCall(auditorCall("t1"));
		expect(res?.block).toBe(true);
		expect(res?.reason).toContain("could not resolve @audit");
	});

	test("armed: rejects an auditor mixed with other tasks", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const mixed = await h.runner.emitToolCall(
			taskCall("t1", { context: "c", tasks: [{ agent: "auditor", task: FULL_AUDIT_TASK }, { agent: "scout", task: "inspect" }] }),
		);
		expect(mixed?.block).toBe(true);
		expect(mixed?.reason).toContain("only task");
	});

	test("armed: blocks a singleton auditor without a string task body", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const malformed = await h.runner.emitToolCall(auditorCall("t1", null));
		expect(malformed?.block).toBe(true);
		expect(malformed?.reason).toContain("string body");
	});

	test("armed: permits exactly one auditor — a second spawn is blocked", async () => {
		const h = await makeHarness();
		await armSummary(h);
		await runAuditor(h);
		const second = await h.runner.emitToolCall(auditorCall("aud-2"));
		expect(second?.block).toBe(true);
		expect(second?.reason).toContain("exactly one auditor");
	});

	test("armed: rejects an auditor spawn carrying outputSchema, naming the canonical format", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const res = await h.runner.emitToolCall(
			taskCall("t1", { context: "c", tasks: [{ agent: "auditor", task: FULL_AUDIT_TASK, outputSchema: { type: "string" } }] }),
		);
		expect(res?.block).toBe(true);
		expect(res?.reason).toContain("outputSchema");
		expect(res?.reason).toContain("headed-text");
		// the rejected spawn must not consume the one-auditor slot
		expect((await h.runner.emitToolCall(auditorCall("t2")))?.block).toBeUndefined();
	});

	test("a JSON-quoted transport wrapper is decoded and its report accepted end to end", async () => {
		// HOME-137 regression: the live task host delivered `"VERDICT: PASS\n…"` as one line.
		const h = await makeHarness();
		await armSummary(h);
		const call = await h.runner.emitToolCall(auditorCall("aud-1"));
		expect(call?.block).toBeUndefined();
		await h.runner.emitToolResult(taskResult("aud-1", JSON.stringify(PASS_REPORT)));
		const verbatim = await h.runner.emitToolCall(workForward("l1", `Review\n\n${PASS_REPORT}`));
		expect(verbatim?.block).toBeUndefined();
	});

	test("a refused report tells the session why, marks refusal state, and frees the slot", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const call = await h.runner.emitToolCall(auditorCall("aud-1"));
		expect(call?.block).toBeUndefined();
		const refusal = (await h.runner.emitToolResult(taskResult("aud-1", "VERDICT: PASS\nlooks fine to me"))) as
			| { content?: Array<{ type: string; text?: string }> }
			| undefined;
		const appended = refusal?.content?.map(part => part.text ?? "").join("\n") ?? "";
		expect(appended).toContain("REFUSED");
		expect(appended).toContain("FINDINGS");
		// review block now names the refusal instead of claiming no audit ran
		const blocked = await h.runner.emitToolCall(workForward("l1", "body"));
		expect(blocked?.block).toBe(true);
		expect(blocked?.reason).toContain("REFUSED");
		// stop guidance distinguishes refusal from no-audit
		const stop = (await h.runner.emit(sessionStop())) as { continue?: boolean; additionalContext?: string } | undefined;
		expect(stop?.continue).toBe(true);
		expect(stop?.additionalContext).toContain("REFUSED");
		expect(stop?.additionalContext).not.toContain("the audit has not run yet");
		// slot is free: a fresh auditor with a usable report completes the attempt
		await runAuditor(h, "aud-2");
		const body = `Review\n\n${PASS_REPORT}`;
		expect((await h.runner.emitToolCall(workForward("l2", body)))?.block).toBeUndefined();
		await h.runner.emitToolResult(workResult("l2", body));
		expect(await h.runner.emit(sessionStop())).toBeUndefined();
	});

	test("failed, verdict-less, structurally incomplete, or interrupted auditor runs release the slot for a fresh retry", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const first = await h.runner.emitToolCall(auditorCall("aud-1"));
		expect(first?.block).toBeUndefined();
		// The host can stop before it delivers any result; that stale reservation must not block a replacement.
		const interrupted = await h.runner.emit(sessionStop());
		expect((interrupted as { continue?: boolean } | undefined)?.continue).toBe(true);
		const retry = await h.runner.emitToolCall(auditorCall("aud-2"));
		expect(retry?.block).toBeUndefined();
		await h.runner.emitToolResult(taskResult("aud-2", "task crashed", true));
		const retry2 = await h.runner.emitToolCall(auditorCall("aud-3"));
		expect(retry2?.block).toBeUndefined();
		// verdict token alone, without the required report sections, is also unusable
		await h.runner.emitToolResult(taskResult("aud-3", "VERDICT: PASS\neverything seemed fine"));
		const retry3 = await h.runner.emitToolCall(auditorCall("aud-4"));
		expect(retry3?.block).toBeUndefined();
		// while no usable report exists, the session must not settle
		const stop = await h.runner.emit(sessionStop());
		expect((stop as { continue?: boolean } | undefined)?.continue).toBe(true);
		const retry4 = await h.runner.emitToolCall(auditorCall("aud-5"));
		expect(retry4?.block).toBeUndefined();
	});

	test("review comment is blocked before the audit and when the report is paraphrased", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const early = await h.runner.emitToolCall(workForward("l1", "review without audit"));
		expect(early?.block).toBe(true);
		await runAuditor(h);
		const paraphrased = await h.runner.emitToolCall(workForward("l2", "auditor passed everything"));
		expect(paraphrased?.block).toBe(true);
		expect(paraphrased?.reason).toContain("VERBATIM");
		const verbatim = await h.runner.emitToolCall(workForward("l3", `Session review...\n\n${PASS_REPORT}\n\nCharter...`));
		expect(verbatim?.block).toBeUndefined();
	});

	test("a REFUSED review write (success:false, no error) does not satisfy the gate", async () => {
		const h = await makeHarness();
		await armSummary(h);
		await runAuditor(h);
		const body = `Review\n\n${PASS_REPORT}`;
		await h.runner.emitToolResult(workResult("l1", body, false));
		const stop = await h.runner.emit(sessionStop());
		expect((stop as { continue?: boolean } | undefined)?.continue).toBe(true);
	});

	test("session cannot settle until the report is forwarded; NEEDS_FIX forwards and ends the attempt", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const stopBeforeAudit = await h.runner.emit(sessionStop());
		expect((stopBeforeAudit as { continue?: boolean } | undefined)?.continue).toBe(true);
		const needsFix = report("NEEDS_FIX", "- [HIGH] AC-1 src/x.ts:3 — evidence: test fails; impact: broken; minimal fix: guard");
		await runAuditor(h, "aud-1", needsFix);
		const stopBeforeForward = await h.runner.emit(sessionStop());
		expect((stopBeforeForward as { continue?: boolean } | undefined)?.continue).toBe(true);
		const body = `Review\n\n${needsFix}`;
		expect((await h.runner.emitToolCall(workForward("l1", body)))?.block).toBeUndefined();
		await h.runner.emitToolResult(workResult("l1", body));
		const stopAfterForward = await h.runner.emit(sessionStop());
		expect(stopAfterForward).toBeUndefined();
	});

	test("structured /skill:summary invocation arms the gate; session_switch resets it", async () => {
		const h = await makeHarness();
		await h.runner.emit({
			type: "message_start",
			message: {
				role: "custom",
				customType: "skill-prompt",
				attribution: "user",
				details: { name: "summary" },
				content: "expanded skill body",
				timestamp: Date.now(),
			},
		} as never);
		const stop = await h.runner.emit(sessionStop());
		expect((stop as { continue?: boolean } | undefined)?.continue).toBe(true);
		await h.runner.emit({ type: "session_switch", reason: "new" } as never);
		const afterSwitch = await h.runner.emit(sessionStop());
		expect(afterSwitch).toBeUndefined();
	});

	test("subagent sessions never gate", async () => {
		const h = await makeHarness(1);
		await armSummary(h);
		const stop = await h.runner.emit(sessionStop());
		expect(stop).toBeUndefined();
	});
});
