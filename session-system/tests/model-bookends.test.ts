// HOME-131 model bookends: /intake auto-routing to the intake role and the
// fail-closed /summary audit gate (one fresh auditor, five required input
// sections, full report structure, verbatim report forwarding before settlement).
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import {
	AUDIT_CONTRACT,
	auditTaskFinalCommits,
	auditTaskPlanReceipts,
	decodeJsonQuoted,
	extractAuditReport,
	missingAuditSections,
	missingReportParts,
	normalizeAuditReport,
	parseAuditVerdict,
	reportForwarded,
} from "../extensions/model-bookends";
import {
	type AuditBinding,
	claimAuditReceipt,
	clearAuditBinding,
	registerAuditBinding,
	releaseAuditReceipt,
	reportSha256,
} from "../extensions/workflow/audit-bridge";
const repoRoot = path.resolve(import.meta.dir, "../..");
const extPath = path.join(repoRoot, "session-system/extensions/model-bookends.ts");

/** The candidate binding a real /summary would register (OMP-38). The commit
 *  doubles as the manifest's Final commit so both diff forms bind cleanly. */
const BOUND_COMMIT = "b".repeat(40);
const PLAN_RECEIPT_SHA = "d".repeat(64);
const DEFAULT_BINDING: AuditBinding = {
	candidateId: "candidate-1",
	candidateSha256: "e".repeat(64),
	commitSha: BOUND_COMMIT,
	planReceiptSha256: PLAN_RECEIPT_SHA,
};

const FULL_AUDIT_TASK = [
	"Approved plan: add three install links to install.sh and mirror them in install.test.ts LINKS.",
	`Plan receipt SHA-256: ${PLAN_RECEIPT_SHA}`,
	"Acceptance criteria: AC-1 installer places all three artifacts; AC-2 LINKS mirrors them; AC-3 installer stays idempotent.",
	"Starting state: commit abc123; pre-existing dirty files: none.",
	"Final diff:",
	`Final commit: ${BOUND_COMMIT}`,
	"```diff",
	"--- a/session-system/install.sh",
	"+++ b/session-system/install.sh",
	"@@ -19,0 +20,3 @@",
	'+place extensions/model-bookends.ts "$HOME/.omp/agent/extensions/model-bookends.ts"',
	"```",
	"Verification: bun test session-system/tests/install.test.ts → 2 pass, 0 fail.",
].join("\n");
const MANIFEST = [
	"Mode: git-range-sha256",
	"Repository: /home/thetu/oh-my-pi",
	`Start commit: ${"a".repeat(40)}`,
	`Final commit: ${"b".repeat(40)}`,
	`SHA-256: ${"c".repeat(64)}`,
].join("\n");
const manifestTask = (manifest: string) => FULL_AUDIT_TASK.replace(/Final diff:[\s\S]*?```\n/, `Final diff:\n${manifest}\n`);

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
/** Mirrors the real task tool result for a user-interrupted auditor: the
 *  envelope reports status="cancelled" and details carries aborted:true. */
const abortedTaskResult = (id: string) =>
	({
		type: "tool_result",
		toolName: "task",
		toolCallId: id,
		input: {},
		content: [
			{
				type: "text",
				text: `<task-result id="Aud" agent="auditor" status="cancelled" duration="8s">\n<abort-reason>user interrupt</abort-reason>\n</task-result>`,
			},
		],
		details: { results: [{ output: "", aborted: true }] },
		isError: true,
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

/** Arm the gate the way a real /summary does: register the candidate binding
 *  (the host's job after a successful summary gate), then the owner input.
 *  `binding: null` arms WITHOUT a binding — the missing-binding refusal path. */
async function armSummary(h: Harness, binding: AuditBinding | null = DEFAULT_BINDING): Promise<void> {
	if (binding) registerAuditBinding(binding);
	else clearAuditBinding();
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
			"Verification",
		]);
		const missing = missingAuditSections(
			"Approved plan: rewrite the frobnicator config end to end.\nFinal diff: the full diff follows below verbatim.",
		);
		expect(missing).toContain("Acceptance criteria");
		expect(missing).toContain("Starting state (commit + pre-existing dirty files)");
		expect(missing).toContain("Verification");
		// pointer-only Final diff carries no auditable bytes (OMP-11)
		expect(missing.some(entry => entry.startsWith("Final diff (needs inline git-diff markers"))).toBe(true);
		expect(missing).toHaveLength(4);
	});

	test("missingAuditSections accepts markdown-dressed labels (OMP-11)", () => {
		const markdown = [
			"# Approved plan",
			"add three install links to install.sh and mirror them in install.test.ts LINKS.",
			"## **Acceptance criteria:**",
			"AC-1 installer places all three artifacts; AC-2 LINKS mirrors them.",
			"3. **Starting state (commit + pre-existing dirty files)** — commit abc123; none dirty.",
			"### Final diff:",
			"```diff",
			"--- a/session-system/install.sh",
			"+++ b/session-system/install.sh",
			"@@ -19,0 +20,3 @@",
			"+place extensions/model-bookends.ts",
			"```",
			"**Verification**: bun test session-system/tests/install.test.ts → 2 pass, 0 fail.",
		].join("\n");
		expect(missingAuditSections(markdown)).toEqual([]);
	});

	test("indented label-lookalikes in diff context lines never open or terminate sections", () => {
		// A unified-diff CONTEXT line (leading space) that looks like a label must not
		// cut the Final diff body short — here the only @@ hunk sits AFTER it, so an
		// early termination would falsely refuse the section as pointer-only.
		const task = [
			"Approved plan: add three install links to install.sh and mirror them in LINKS.",
			"Acceptance criteria: AC-1 installer places all three artifacts; AC-2 LINKS mirrors them.",
			"Starting state: commit abc123; pre-existing dirty files: none.",
			"Final diff:",
			"```diff",
			"--- a/docs/runbook.md",
			"+++ b/docs/runbook.md",
			" Verification: run the smoke test before shipping.",
			" ## Final diff:",
			" 3. **Starting state (commit + pre-existing dirty files)** — notes.",
			"@@ -19,0 +20,3 @@",
			"+place extensions/model-bookends.ts",
			"```",
			"Verification: bun test session-system/tests/install.test.ts → 2 pass, 0 fail.",
		].join("\n");
		expect(missingAuditSections(task)).toEqual([]);
	});


	test("missingAuditSections accepts a complete git-range-sha256 manifest as the Final diff", () => {
		expect(missingAuditSections(manifestTask(MANIFEST))).toEqual([]);
	});

	test("missingAuditSections rejects every incomplete or malformed manifest variant", () => {
		const variants: Array<[string, string]> = [
			["missing Mode", MANIFEST.replace(/^Mode:.*\n/, "")],
			["missing Repository", MANIFEST.replace(/^Repository:.*\n/m, "")],
			["missing Start commit", MANIFEST.replace(/^Start commit:.*\n/m, "")],
			["missing Final commit", MANIFEST.replace(/^Final commit:.*\n/m, "")],
			["missing SHA-256", MANIFEST.replace(/^SHA-256:.*$/m, "")],
			["relative repository path", MANIFEST.replace("/home/thetu/oh-my-pi", "oh-my-pi")],
			["short start commit", MANIFEST.replace("a".repeat(40), "a".repeat(12))],
			["short final commit", MANIFEST.replace("b".repeat(40), "b".repeat(7))],
			["short digest", MANIFEST.replace("c".repeat(64), "c".repeat(32))],
			["wrong mode", MANIFEST.replace("git-range-sha256", "git-range-md5")],
			["duplicate Start commit lines", `${MANIFEST}\nStart commit: ${"d".repeat(40)}`],
			["duplicate SHA-256 lines", `${MANIFEST}\nSHA-256: ${"e".repeat(64)}`],
			["two full manifests", `${MANIFEST}\n${MANIFEST}`],
			["manifest plus smuggled Command line", `${MANIFEST}\nCommand: rm -rf /`],
			["manifest plus trailing prose", `${MANIFEST}\nreconstruct it yourself as described.`],
			["manifest fields out of order", MANIFEST.split("\n").reverse().join("\n")],
		];
		for (const [name, manifest] of variants) {
			const missing = missingAuditSections(manifestTask(manifest));
			expect(missing.some(entry => entry.startsWith("Final diff (needs inline git-diff markers")), name).toBe(true);
		}
	});

	test("filename-only marker lines never pass; complete marker sets do", () => {
		const pointerCases = [
			// dressed-up filename pointer: header line with prose, no hunks, no metadata
			"diff --git a/src/x.ts b/src/x.ts\nsee the branch for the actual change.",
			// bare header pair without any hunk is filenames only
			"--- a/src/x.ts\n+++ b/src/x.ts",
			// partial rename metadata is filenames only
			"diff --git a/old.txt b/new.txt\nrename from old.txt",
		];
		for (const body of pointerCases) {
			expect(
				missingAuditSections(manifestTask(body)).some(entry => entry.startsWith("Final diff (needs inline git-diff markers")),
				body,
			).toBe(true);
		}
		// a pure rename produces no hunks — its complete metadata set is the diff material
		const rename = manifestTask(
			"diff --git a/old.txt b/new.txt\nsimilarity index 100%\nrename from old.txt\nrename to new.txt",
		);
		expect(missingAuditSections(rename)).toEqual([]);
		// a mode-only change likewise needs both mode lines
		const mode = manifestTask("diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755");
		expect(missingAuditSections(mode)).toEqual([]);
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
		// annotated headings are harmless layout variance (OMP-11)
		expect(missingReportParts(PASS_REPORT.replace("FINDINGS", "FINDINGS (ordered by severity)"))).toEqual([]);
		expect(missingReportParts(PASS_REPORT.replace("CHECKS RUN", "CHECKS RUN:"))).toEqual([]);
		expect(missingReportParts(report("NEEDS_FIX", "(none)"))).toContain("at least one finding under NEEDS_FIX");
		expect(missingReportParts(report("NEEDS_FIX", "- none"))).toContain("at least one finding under NEEDS_FIX");
		expect(missingReportParts(report("NEEDS_FIX", "- [P0] AC-1 src/x.ts:3 — evidence: failure"))).toContain(
			"at least one finding under NEEDS_FIX",
		);
		// a multiline finding block (indented continuations) with all six signals passes
		expect(
			missingReportParts(
				report(
					"NEEDS_FIX",
					"- [P0] AC-1 covers src/x.ts:3-9.\n  evidence: the test fails.\n  impact: broken settlement.\n  minimal fix: guard the branch.",
				),
			),
		).toEqual([]);
		// two separate incomplete bullets do not merge into one qualifying finding
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

	test("decodeJsonQuoted unwraps exact single-key text/report envelopes and nothing else (OMP-22)", () => {
		// OMP-22 regression: a live auditor emitting {"text": report} was refused and
		// burned a bounded attempt while {"report": report} was accepted (2026-08-19).
		expect(decodeJsonQuoted(JSON.stringify({ text: PASS_REPORT }))).toBe(PASS_REPORT);
		expect(missingReportParts(extractAuditReport({ results: [{ output: JSON.stringify({ text: PASS_REPORT }) }] }, ""))).toEqual([]);
		// Security review conditions: only the exact single-key envelope shape unwraps,
		// and every ambiguous shape is REFUSED end-to-end by gate validation — not
		// merely left undecoded.
		const extraKeys = JSON.stringify({ text: PASS_REPORT, status: "error" });
		expect(decodeJsonQuoted(extraKeys)).toBe(extraKeys);
		expect(missingReportParts(extractAuditReport({ results: [{ output: extraKeys }] }, "")).length).toBeGreaterThan(0);
		const bothKeys = JSON.stringify({ report: PASS_REPORT, text: "other" });
		expect(decodeJsonQuoted(bothKeys)).toBe(bothKeys);
		expect(missingReportParts(extractAuditReport({ results: [{ output: bothKeys }] }, "")).length).toBeGreaterThan(0);
		const nonString = JSON.stringify({ text: 42 });
		expect(decodeJsonQuoted(nonString)).toBe(nonString);
		expect(missingReportParts(extractAuditReport({ results: [{ output: nonString }] }, "")).length).toBeGreaterThan(0);
		// A structured report carrying an incidental text field is never collapsed to it.
		const structured = JSON.stringify({ verdict: "PASS", findings: [], text: "incidental" });
		expect(decodeJsonQuoted(structured)).toBe(structured);
		expect(normalizeAuditReport(structured)).toBe(structured);
	});

	test("normalizeAuditReport strips transport quoting and preamble byte-for-byte from VERDICT onward", () => {
		// OMP-8/OMP-11 regression: JSON-quoted output with provider preamble before VERDICT.
		const wrapped = JSON.stringify(`I inspected the work and here is my assessment.\n\n${PASS_REPORT}`);
		expect(normalizeAuditReport(wrapped)).toBe(PASS_REPORT);
		// idempotent: normalized text normalizes to itself
		expect(normalizeAuditReport(normalizeAuditReport(wrapped))).toBe(PASS_REPORT);
		expect(normalizeAuditReport(PASS_REPORT)).toBe(PASS_REPORT);
		// structured JSON reports pass through untouched for separate validation
		const jsonReport = '{"verdict": "PASS", "findings": []}';
		expect(normalizeAuditReport(jsonReport)).toBe(jsonReport);
		// no VERDICT anywhere: decoded text is returned trimmed, never invented
		expect(normalizeAuditReport("  just some prose  ")).toBe("just some prose");
		// preamble-wrapped reports validate and parse after normalization
		expect(missingReportParts(wrapped)).toEqual([]);
		expect(parseAuditVerdict(wrapped)).toBe("PASS");
		expect(extractAuditReport({ results: [{ output: wrapped }] }, "")).toBe(PASS_REPORT);
	});

	test("auditTaskFinalCommits reads Final commit lines from the Final diff section only (OMP-38)", () => {
		expect(auditTaskFinalCommits(FULL_AUDIT_TASK)).toEqual([BOUND_COMMIT]);
		expect(auditTaskFinalCommits(manifestTask(MANIFEST))).toEqual([BOUND_COMMIT]);
		// a Final commit line OUTSIDE the Final diff section does not bind
		const outside = `Final commit: ${"9".repeat(40)}\n${FULL_AUDIT_TASK.replace(`Final commit: ${BOUND_COMMIT}\n`, "")}`;
		expect(auditTaskFinalCommits(outside)).toEqual([]);
		// conflicting values surface both — the gate requires exactly one
		const conflicted = FULL_AUDIT_TASK.replace("```diff", `Final commit: ${"9".repeat(40)}\n\`\`\`diff`);
		expect(auditTaskFinalCommits(conflicted)).toEqual([BOUND_COMMIT, "9".repeat(40)]);
		// duplicate-identical lines are NOT deduped — the gate requires exactly one
		const duplicated = FULL_AUDIT_TASK.replace(`Final commit: ${BOUND_COMMIT}`, `Final commit: ${BOUND_COMMIT}\nFinal commit: ${BOUND_COMMIT}`);
		expect(auditTaskFinalCommits(duplicated)).toEqual([BOUND_COMMIT, BOUND_COMMIT]);
		// uppercase hex is not the canonical byte-exact form
		expect(auditTaskFinalCommits(FULL_AUDIT_TASK.replace(BOUND_COMMIT, BOUND_COMMIT.toUpperCase()))).toEqual([]);
	});

	test("auditTaskPlanReceipts requires the canonical line inside Approved plan (OMP-38)", () => {
		expect(auditTaskPlanReceipts(FULL_AUDIT_TASK)).toEqual([PLAN_RECEIPT_SHA]);
		// the hash placed only inside the Final diff section is not a citation
		const buried = FULL_AUDIT_TASK.replace(`Plan receipt SHA-256: ${PLAN_RECEIPT_SHA}\n`, "").replace(
			`Final commit: ${BOUND_COMMIT}`,
			`Final commit: ${BOUND_COMMIT}\nPlan receipt SHA-256: ${PLAN_RECEIPT_SHA}`,
		);
		expect(auditTaskPlanReceipts(buried)).toEqual([]);
	});

	test("a stored plan body embedded in Approved plan cannot satisfy or truncate later sections (OMP-38)", () => {
		// Real stamp bodies carry their own ## Approach / ## Verification headings.
		const stampBody =
			"**Plan approved**\n\n# Work\n- Plan: `local://work-plan.md`\n\n## Approach\n1. Change the shared path\n\n## Verification\n1. Run the focused check";
		const embedded = FULL_AUDIT_TASK.replace(
			"Approved plan: add three install links to install.sh and mirror them in install.test.ts LINKS.",
			`Approved plan:\n${stampBody}`,
		);
		expect(missingAuditSections(embedded)).toEqual([]);
		expect(auditTaskFinalCommits(embedded)).toEqual([BOUND_COMMIT]);
		// dropping the OUTER Verification section must still be caught — the plan
		// body's own `## Verification` heading satisfies nothing
		const withoutOuter = embedded.replace(/\nVerification: bun test.*$/, "");
		expect(missingAuditSections(withoutOuter)).toEqual(["Verification"]);
	});

	test("multiline and star-bullet findings pass after whitespace flattening (OMP-38)", () => {
		// signal split across lines: "minimal\n  fix" must still count
		expect(
			missingReportParts(
				report(
					"NEEDS_FIX",
					"- [P0] AC-1 covers src/x.ts:3-9.\n  evidence: the test fails.\n  impact: broken settlement.\n  minimal\n  fix: guard the branch.",
				),
			),
		).toEqual([]);
		// star bullets with indented sub-bullet continuations form one finding block
		expect(
			missingReportParts(
				report(
					"NEEDS_FIX",
					"* [P1] AC-2 src/y.ts:12\n  - evidence: observed drift\n  - impact: stale receipt\n  - minimal fix: rebind",
				),
			),
		).toEqual([]);
	});

	test("plain provider chatter before VERDICT is sliced off unquoted reports (OMP-18)", () => {
		const chatty = `⚠ provider warning: approaching tool budget\nOkay — auditing now.\n\n${PASS_REPORT}`;
		expect(normalizeAuditReport(chatty)).toBe(PASS_REPORT);
		expect(missingReportParts(chatty)).toEqual([]);
		expect(parseAuditVerdict(chatty)).toBe("PASS");
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
		expect(res?.reason).toContain("Verification");
	});

	test("armed: leaves ordinary batches alone and accepts a singleton auditor batch", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const ordinary = await h.runner.emitToolCall(taskCall("t0", { context: "c", tasks: [{ agent: "scout", task: "inspect" }] }));
		expect(ordinary?.block).toBeUndefined();
		expect((await h.runner.emitToolCall(auditorCall("t1")))?.block).toBeUndefined();
	});

	test("armed: accepts a same-family configured auditor — independence is fresh context, not family inequality (OMP-11)", async () => {
		const h = await makeHarness(0, { auditRole: "anthropic/claude-fable-5" });
		await armSummary(h);
		const res = await h.runner.emitToolCall(auditorCall("t1"));
		expect(res?.block).toBeUndefined();
		// the accepted spawn consumes the one-auditor slot as usual
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

	test("manifest packet with preamble-wrapped JSON-quoted report: one spawn, byte-equal receipt, clean settlement (OMP-11)", async () => {
		const h = await makeHarness();
		await armSummary(h);
		// complete git-range-sha256 manifest packet is accepted for spawn
		const call = await h.runner.emitToolCall(auditorCall("aud-1", manifestTask(MANIFEST)));
		expect(call?.block).toBeUndefined();
		// auditor result arrives JSON-quoted with provider preamble before VERDICT
		const refusal = await h.runner.emitToolResult(
			taskResult("aud-1", JSON.stringify(`Reconstructed and verified the manifest diff.\n\n${PASS_REPORT}`)),
		);
		expect(refusal).toBeUndefined(); // accepted, not refused — no appended notice
		// exactly one accepted spawn: the slot is consumed
		expect((await h.runner.emitToolCall(auditorCall("aud-2")))?.block).toBe(true);
		// the registered receipt binds the NORMALIZED report bytes exactly
		const receipt = claimAuditReceipt(reportSha256(PASS_REPORT));
		expect(receipt?.report).toBe(PASS_REPORT);
		releaseAuditReceipt(reportSha256(PASS_REPORT));
		// forward the normalized report verbatim; settlement is clean afterwards
		const body = `Review\n\n${PASS_REPORT}`;
		expect((await h.runner.emitToolCall(workForward("l1", body)))?.block).toBeUndefined();
		await h.runner.emitToolResult(workResult("l1", body));
		expect(await h.runner.emit(sessionStop())).toBeUndefined();
	});

	test("reportSha256 canonicalizes outer whitespace and CRLF, never interior bytes (OMP-38 AC-3)", () => {
		// Live regression 2026-08-20: forwarding the exact report with a trailing
		// newline was refused as "no fresh auditor receipt matches those bytes".
		expect(reportSha256(`${PASS_REPORT}\n`)).toBe(reportSha256(PASS_REPORT));
		expect(reportSha256(`\r\n${PASS_REPORT.replace(/\n/g, "\r\n")} `)).toBe(reportSha256(PASS_REPORT));
		expect(reportSha256(PASS_REPORT.replace("PASS", "FAIL"))).not.toBe(reportSha256(PASS_REPORT));
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

	test("an interrupted auditor run releases the slot without consuming the replacement (OMP-11)", async () => {
		const h = await makeHarness();
		await armSummary(h);
		expect((await h.runner.emitToolCall(auditorCall("aud-1")))?.block).toBeUndefined();
		const released = (await h.runner.emitToolResult(abortedTaskResult("aud-1"))) as
			| { content?: Array<{ type: string; text?: string }> }
			| undefined;
		const notice = released?.content?.map(part => part.text ?? "").join("\n") ?? "";
		expect(notice).toContain("interrupted");
		expect(notice).not.toContain("REFUSED");
		// The interruption burned nothing: a full unusable-report + replacement cycle remains.
		expect((await h.runner.emitToolCall(auditorCall("aud-2")))?.block).toBeUndefined();
		const firstRefusal = (await h.runner.emitToolResult(taskResult("aud-2", "VERDICT: PASS\nno structure"))) as
			| { content?: Array<{ type: string; text?: string }> }
			| undefined;
		const firstAppend = firstRefusal?.content?.map(part => part.text ?? "").join("\n") ?? "";
		expect(firstAppend).toContain("ONE fresh replacement");
		expect(firstAppend).not.toContain("exhausted");
		// A fresh spawn is still available; an error result WITHOUT host abort signals
		// (plain crash) must consume it — bounded accounting is not evadable by text.
		expect((await h.runner.emitToolCall(auditorCall("aud-3")))?.block).toBeUndefined();
		const secondRefusal = (await h.runner.emitToolResult(taskResult("aud-3", "the run was aborted midway", true))) as
			| { content?: Array<{ type: string; text?: string }> }
			| undefined;
		const secondAppend = secondRefusal?.content?.map(part => part.text ?? "").join("\n") ?? "";
		expect(secondAppend).toContain("exhausted");
	});

	test("bounded retries: one replacement after an unusable report, then exhaustion (OMP-11)", async () => {
		const h = await makeHarness();
		await armSummary(h);
		// A host-interrupted spawn (no result) releases without burning the replacement.
		expect((await h.runner.emitToolCall(auditorCall("aud-1")))?.block).toBeUndefined();
		const interrupted = await h.runner.emit(sessionStop());
		expect((interrupted as { continue?: boolean } | undefined)?.continue).toBe(true);
		// First unusable result (task crash) → exactly one replacement is allowed.
		expect((await h.runner.emitToolCall(auditorCall("aud-2")))?.block).toBeUndefined();
		const firstRefusal = (await h.runner.emitToolResult(taskResult("aud-2", "task crashed", true))) as
			| { content?: Array<{ type: string; text?: string }> }
			| undefined;
		const firstAppend = firstRefusal?.content?.map(part => part.text ?? "").join("\n") ?? "";
		expect(firstAppend).toContain("ONE fresh replacement");
		expect(firstAppend).not.toContain("exhausted");
		expect((await h.runner.emitToolCall(auditorCall("aud-3")))?.block).toBeUndefined();
		// Second unusable result (verdict token without report structure) exhausts the attempt.
		const secondRefusal = (await h.runner.emitToolResult(taskResult("aud-3", "VERDICT: PASS\neverything seemed fine"))) as
			| { content?: Array<{ type: string; text?: string }> }
			| undefined;
		const secondAppend = secondRefusal?.content?.map(part => part.text ?? "").join("\n") ?? "";
		expect(secondAppend).toContain("exhausted");
		// Third spawn is refused outright.
		const third = await h.runner.emitToolCall(auditorCall("aud-4"));
		expect(third?.block).toBe(true);
		expect(third?.reason).toContain("exhausted");
		// The review stays blocked (no report exists) and names the exhaustion.
		const blocked = await h.runner.emitToolCall(workForward("l1", "body"));
		expect(blocked?.block).toBe(true);
		expect(blocked?.reason).toContain("exhausted");
		// session_stop ends this /summary honestly blocked — no forced continuation, no receipt.
		expect(await h.runner.emit(sessionStop())).toBeUndefined();
		expect(h.notifies.some(n => n.includes("auditor budget exhausted"))).toBe(true);
		// A fresh owner-entered /summary resets the bounded attempt.
		await armSummary(h);
		expect((await h.runner.emitToolCall(auditorCall("aud-5")))?.block).toBeUndefined();
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

	test("no candidate binding: spawn blocked with /plan + /summary guidance, slot not consumed (OMP-38)", async () => {
		const h = await makeHarness();
		await armSummary(h, null);
		const blocked = await h.runner.emitToolCall(auditorCall("t1"));
		expect(blocked?.block).toBe(true);
		expect(blocked?.reason).toContain("no finalized candidate");
		expect(blocked?.reason).toContain("/summary");
		// the refusal did not reserve the slot: binding arrives, same spawn succeeds
		registerAuditBinding(DEFAULT_BINDING);
		expect((await h.runner.emitToolCall(auditorCall("t2")))?.block).toBeUndefined();
	});

	test("capped plan packet refuses the spawn (OMP-38)", async () => {
		const h = await makeHarness();
		await armSummary(h, { ...DEFAULT_BINDING, capped: true });
		const blocked = await h.runner.emitToolCall(auditorCall("t1"));
		expect(blocked?.block).toBe(true);
		expect(blocked?.reason).toContain("byte ceiling");
	});

	test("Final commit mismatch and missing plan-receipt citation block without consuming the slot (OMP-38)", async () => {
		const h = await makeHarness();
		await armSummary(h, { ...DEFAULT_BINDING, commitSha: "9".repeat(40) });
		const mismatch = await h.runner.emitToolCall(auditorCall("t1"));
		expect(mismatch?.block).toBe(true);
		expect(mismatch?.reason).toContain("Final commit");
		expect(mismatch?.reason).toContain("9".repeat(40));
		// rebind to the task's commit, but drop the plan-receipt citation
		registerAuditBinding(DEFAULT_BINDING);
		const uncited = await h.runner.emitToolCall(
			auditorCall("t2", FULL_AUDIT_TASK.replace(`Plan receipt SHA-256: ${PLAN_RECEIPT_SHA}\n`, "")),
		);
		expect(uncited?.block).toBe(true);
		expect(uncited?.reason).toContain("Plan receipt SHA-256");
		// nothing was consumed: the matching task spawns and completes cleanly
		await runAuditor(h, "t3");
		const body = `Review\n\n${PASS_REPORT}`;
		expect((await h.runner.emitToolCall(workForward("l1", body)))?.block).toBeUndefined();
	});

	test("a rebind after capture permanently invalidates the receipt — A→B→A included (OMP-38)", async () => {
		const h = await makeHarness();
		await armSummary(h);
		await runAuditor(h);
		const sha = reportSha256(PASS_REPORT);
		registerAuditBinding({ ...DEFAULT_BINDING, candidateId: "candidate-2", commitSha: "9".repeat(40) });
		expect(claimAuditReceipt(sha)).toBeNull();
		// restoring identical fields is still a binding change: the old report stays dead
		registerAuditBinding(DEFAULT_BINDING);
		expect(claimAuditReceipt(sha)).toBeNull();
	});

	test("a rebind while the auditor runs refuses its late report without consuming the replacement (OMP-38)", async () => {
		const h = await makeHarness();
		await armSummary(h);
		expect((await h.runner.emitToolCall(auditorCall("aud-1")))?.block).toBeUndefined();
		// /summary reruns mid-flight: fresh freeze, fresh binding
		registerAuditBinding({ ...DEFAULT_BINDING, candidateId: "candidate-2" });
		const refused = (await h.runner.emitToolResult(taskResult("aud-1", PASS_REPORT))) as
			| { content?: Array<{ type: string; text?: string }> }
			| undefined;
		const notice = refused?.content?.map(part => part.text ?? "").join("\n") ?? "";
		expect(notice).toContain("binding changed");
		// nothing registered, nothing consumed: a fresh matching auditor still runs
		expect(claimAuditReceipt(reportSha256(PASS_REPORT))).toBeNull();
		await runAuditor(h, "aud-2");
	});
});
