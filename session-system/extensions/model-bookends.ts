/**
 * model-bookends — Fable bookends for omp (HOME-131)
 *
 * /intake: resolve the `intake` model role, switch the session to it at :high
 * effort, then forward to the native `/skill:intake` dispatcher — no manual
 * /model switch.
 *
 * /summary: arm a fail-closed audit gate. The gate injects the audit contract,
 * permits exactly ONE fresh `auditor` task whose input carries the five
 * required sections, and refuses to let the session settle until the auditor's
 * verbatim report has been forwarded through the backend-neutral `work` tool as
 * typed `audit` evidence. A NEEDS_FIX report still forwards — it ends the summary
 * attempt; fixes happen afterward and the next owner-entered /summary starts a
 * fresh auditor (state resets on session switch).
 */
import { ThinkingLevel } from "@oh-my-pi/pi-agent-core";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { prompt } from "@oh-my-pi/pi-utils";
import { currentAuditBinding, currentBindingGeneration, registerAuditReceipt } from "./workflow/audit-bridge";
// Static audit instructions (HOME-131) — installed alongside this extension so the
// relative import resolves in both link and copy modes.
import auditContract from "./model-bookends-audit.md" with { type: "text" };
import refusedNotice from "./model-bookends-refused.md" with { type: "text" };
import schemaRefused from "./model-bookends-schema-refused.md" with { type: "text" };
import stopNoAudit from "./model-bookends-stop-no-audit.md" with { type: "text" };
import stopRefused from "./model-bookends-stop-refused.md" with { type: "text" };
import stopNotForwarded from "./model-bookends-stop-not-forwarded.md" with { type: "text" };

/** Effort pinned by HOME-131: /intake always runs Fable at :high. */
export const INTAKE_THINKING_LEVEL = ThinkingLevel.High;

/** Optional markdown dressing before a canonical label: list number, heading marks, bold.
 *  NO indent tolerance: unified-diff context lines start with one space, so an indented
 *  label-lookalike inside an inlined diff must never open or terminate a section. */
const LABEL_PREFIX = String.raw`(?:\d+[.)][ \t]+)?(?:#{1,6}[ \t]+)?\**`;
/** Optional bold close, one optional parenthetical annotation, and an optional colon/dash suffix after a canonical label. */
const LABEL_SUFFIX = String.raw`\**(?:[ \t]*\([^)\n]*\))?\**[ \t]*(?:$|[:—-])`;

/** The five labeled sections every auditor task must inline (HOME-131 input contract). */
export const AUDIT_INPUT_SECTIONS: ReadonlyArray<{ label: string; pattern: RegExp }> = [
	{ label: "Approved plan", pattern: new RegExp(`^${LABEL_PREFIX}approved plan${LABEL_SUFFIX}`, "gim") },
	{ label: "Acceptance criteria", pattern: new RegExp(`^${LABEL_PREFIX}acceptance criteria${LABEL_SUFFIX}`, "gim") },
	{ label: "Starting state (commit + pre-existing dirty files)", pattern: new RegExp(`^${LABEL_PREFIX}starting (?:state|commit)${LABEL_SUFFIX}`, "gim") },
	{ label: "Final diff", pattern: new RegExp(`^${LABEL_PREFIX}final diff${LABEL_SUFFIX}`, "gim") },
	{ label: "Verification", pattern: new RegExp(`^${LABEL_PREFIX}verification(?: results)?${LABEL_SUFFIX}`, "gim") },
];

/** Bodies of the five sections, position-aligned with AUDIT_INPUT_SECTIONS.
 *  Parsed in CANONICAL ORDER: each label is searched only after the previous
 *  found label, and a body runs to the next found label (or end). A lookalike
 *  heading embedded in an earlier body — e.g. a stored plan's own
 *  `## Verification` — can neither terminate a section early nor satisfy a
 *  later one (OMP-38). Out-of-order sections read as missing: fail closed. */
function auditSectionBodies(task: string): Array<string | undefined> {
	const found: Array<{ index: number; end: number } | undefined> = [];
	let cursor = 0;
	for (const section of AUDIT_INPUT_SECTIONS) {
		section.pattern.lastIndex = cursor;
		const match = section.pattern.exec(task);
		if (match) {
			found.push({ index: match.index, end: match.index + match[0].length });
			cursor = match.index + match[0].length;
		} else {
			found.push(undefined);
		}
	}
	return found.map((hit, i) => {
		if (!hit) return undefined;
		const next = found.slice(i + 1).find(later => later !== undefined);
		return task.slice(hit.end, next ? next.index : task.length).replace(/^[\s:—-]+/, "");
	});
}

/** The manifest is EXACTLY these five lines, in order (blank and code-fence
 *  lines around them are tolerated). Anything else beside them — extra fields,
 *  duplicates, prose, a smuggled `Command:` line — fails: the auditor builds
 *  its own fixed argv from these values and must never see competing text. */
const MANIFEST_LINES: ReadonlyArray<RegExp> = [
	/^Mode:[ \t]*git-range-sha256$/,
	/^Repository:[ \t]*\/\S+$/,
	/^Start commit:[ \t]*[0-9a-f]{40}$/,
	/^Final commit:[ \t]*[0-9a-f]{40}$/,
	/^SHA-256:[ \t]*[0-9a-f]{64}$/,
];

/** Inline diff material or one complete git-range-sha256 manifest. Filenames
 *  and "attached below" pointers fail: the auditor audits only bytes it can
 *  obtain and verify. Filename-bearing marker lines count only as COMPLETE
 *  sets — a hunk header for text diffs, a binary marker, or the full metadata
 *  set of a pure rename/copy/mode-change diff; a lone `diff --git` header, a
 *  bare `---`/`+++` pair, or partial rename metadata is just filenames. */
function hasValidFinalDiff(body: string): boolean {
	const content =
		/^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@/m.test(body) ||
		/^GIT binary patch$/m.test(body) ||
		/^Binary files .* differ$/m.test(body);
	const metadataOnly =
		/^diff --git /m.test(body) &&
		((/^similarity index /m.test(body) && /^rename from /m.test(body) && /^rename to /m.test(body)) ||
			(/^similarity index /m.test(body) && /^copy from /m.test(body) && /^copy to /m.test(body)) ||
			(/^old mode /m.test(body) && /^new mode /m.test(body)));
	if (content || metadataOnly) return true;
	// Manifest mode (HOME-131 large/binary candidates): exactly the five field
	// lines, in order, nothing else.
	const lines = body.split("\n").map(line => line.trim()).filter(line => line !== "" && !line.startsWith("```"));
	return lines.length === MANIFEST_LINES.length && MANIFEST_LINES.every((field, index) => field.test(lines[index]));
}

/**
 * Sections absent OR empty in an auditor task body ([] = complete). The label
 * alone is not enough: each section must carry actual content — the plan text,
 * the criteria, the commit + dirty files, the diff, the results — because the
 * auditor audits only what it receives. The Final diff section must carry real
 * diff material: inline git-diff markers, or a complete hash-verified git
 * manifest (Mode: git-range-sha256, absolute Repository, 40-hex Start/Final
 * commits, 64-hex SHA-256).
 */
export function missingAuditSections(task: string): string[] {
	const missing: string[] = [];
	const bodies = auditSectionBodies(task);
	for (const [i, section] of AUDIT_INPUT_SECTIONS.entries()) {
		const body = bodies[i];
		if (body === undefined) {
			missing.push(section.label);
		} else if (!/(?:\S\s*){8,}/.test(body)) {
			missing.push(`${section.label} (label present but no content)`);
		} else if (section.label === "Final diff" && !hasValidFinalDiff(body)) {
			missing.push(
				"Final diff (needs inline git-diff markers, or a complete git-range-sha256 manifest: Mode, absolute Repository, 40-hex Start commit, 40-hex Final commit, 64-hex SHA-256 — pointers like a bare filename or 'attached below' are not auditable)",
			);
		}
	}
	return missing;
}

/** Every `Final commit:` line in the task's Final diff section, byte-exact and
 *  in order — no case folding, no dedupe: duplicate-identical or uppercase
 *  lines read as invalid. Exactly one, byte-equal to the bound candidate
 *  commit, is required before the auditor slot is reserved (OMP-38). The
 *  manifest form carries the line natively; the inline-diff form adds it
 *  beside the diff. */
export function auditTaskFinalCommits(task: string): string[] {
	const body = auditSectionBodies(task)[AUDIT_INPUT_SECTIONS.findIndex(section => section.label === "Final diff")];
	if (body === undefined) return [];
	return [...body.matchAll(/^Final commit:[ \t]*([0-9a-f]{40})\b/gm)].map(match => match[1]);
}

/** Every `Plan receipt SHA-256:` line in the task's Approved plan section,
 *  byte-exact. Exactly one, byte-equal to the bound plan receipt hash, is
 *  required — a hash buried in diff bytes or prose is not a citation. */
export function auditTaskPlanReceipts(task: string): string[] {
	const body = auditSectionBodies(task)[AUDIT_INPUT_SECTIONS.findIndex(section => section.label === "Approved plan")];
	if (body === undefined) return [];
	return [...body.matchAll(/^Plan receipt SHA-256:[ \t]*([0-9a-f]{64})\b/gm)].map(match => match[1]);
}

/** True when the auditor task run was interrupted/aborted before yielding a
 *  report: the host marks the result aborted (details.results[].aborted) or
 *  renders its transport envelope — always at byte 0 of the content — with a
 *  cancelled/aborted status. Host-authored signals ONLY: error text is never
 *  consulted, so a failing auditor whose message mentions "aborted" still
 *  consumes its bounded attempt. An interrupted run is not a refused report —
 *  it releases the slot without consuming the bounded replacement (OMP-11). */
export function auditorRunInterrupted(details: unknown, contentText: string): boolean {
	const results = (details as { results?: Array<{ aborted?: boolean }> } | undefined)?.results;
	if (Array.isArray(results) && results.some(result => result?.aborted === true)) return true;
	return /^<task-result [^>]*status="(?:cancelled|aborted)"/.test(contentText);
}

export type AuditVerdict = "PASS" | "NEEDS_FIX" | "BLOCKED";

/** Semantic signals a NEEDS_FIX finding must carry (HOME-131). Layout is free —
 *  a finding may span lines and use any punctuation — but every signal must be
 *  present in the finding's bullet block: severity tag, AC id, file:line or
 *  file:start-end, evidence, impact, minimal fix. */
const FINDING_SIGNALS: ReadonlyArray<RegExp> = [
	/\[[a-z][a-z0-9]*\]/i,
	/\bAC-\S+/i,
	/\S+:\d+(?:-\d+)?/,
	/\bevidence\b/i,
	/\bimpact\b/i,
	/\bminimal fix\b/i,
];

function hasStructuredFinding(finding: unknown): boolean {
	if (typeof finding === "string") {
		// Each column-0 Markdown bullet (- or *) opens one finding block; indented
		// lines — prose continuations and sub-bullets alike — extend it. Each block
		// is whitespace-flattened before the signal checks so a signal split across
		// lines ("minimal\n  fix") still counts (OMP-38).
		return finding
			.trim()
			.split(/^(?=[-*]\s)/m)
			.some(block => {
				if (!/^[-*]\s/.test(block)) return false;
				const flat = block.replace(/\s+/g, " ");
				return FINDING_SIGNALS.every(signal => signal.test(flat));
			});
	}
	if (!finding || typeof finding !== "object" || Array.isArray(finding)) return false;
	const record = finding as Record<string, unknown>;
	return [record.severity, record.ac, record.location, record.evidence, record.impact, record.minimal_fix].every(
		value => typeof value === "string" && value.trim().length > 0,
	);
}

/** Canonical headed-text sections (auditor.md report template). Case-sensitive and
 *  line-anchored so a one-line token echo cannot satisfy the structure check; one
 *  parenthetical annotation or colon/dash suffix after the label is tolerated. */
const REPORT_HEADING_SUFFIX = String.raw`(?:[ \t]*\([^)\n]*\))?[ \t]*[:—-]?[ \t]*$`;
export const REPORT_SECTIONS: ReadonlyArray<{ label: string; pattern: RegExp }> = [
	{ label: "FINDINGS", pattern: new RegExp(String.raw`^\s*(?:#+\s*)?FINDINGS${REPORT_HEADING_SUFFIX}`, "m") },
	{ label: "ACCEPTANCE COVERAGE", pattern: new RegExp(String.raw`^\s*(?:#+\s*)?ACCEPTANCE COVERAGE${REPORT_HEADING_SUFFIX}`, "m") },
	{ label: "OUT OF SCOPE", pattern: new RegExp(String.raw`^\s*(?:#+\s*)?OUT OF SCOPE${REPORT_HEADING_SUFFIX}`, "m") },
	{ label: "CHECKS RUN", pattern: new RegExp(String.raw`^\s*(?:#+\s*)?CHECKS RUN${REPORT_HEADING_SUFFIX}`, "m") },
	{ label: "REMAINING QUESTIONS", pattern: new RegExp(String.raw`^\s*(?:#+\s*)?REMAINING QUESTIONS${REPORT_HEADING_SUFFIX}`, "m") },
];

/** First verdict token in a report, or undefined when the report is not verdict-structured. */
export function parseAuditVerdict(text: string): AuditVerdict | undefined {
	const match = /VERDICT["']?\s*:\s*["']?(PASS|NEEDS_FIX|BLOCKED)\b/i.exec(normalizeAuditReport(text));
	return (match?.[1]?.toUpperCase() as AuditVerdict) ?? undefined;
}

/** Required keys of the JSON report shape the live dry-run auditor emitted
 *  (session transcript 2026-08-14, HOME-131: `details.results[0].output` was JSON
 *  with snake_case keys). */
function missingJsonReportParts(obj: Record<string, unknown>): string[] {
	const missing: string[] = [];
	const verdict = typeof obj.verdict === "string" ? parseAuditVerdict(`VERDICT: ${obj.verdict}`) : undefined;
	if (!verdict) missing.push("verdict (PASS | NEEDS_FIX | BLOCKED)");
	if (!Array.isArray(obj.findings)) missing.push("findings array");
	else if (verdict === "NEEDS_FIX" && !obj.findings.some(hasStructuredFinding)) {
		missing.push("at least one finding under NEEDS_FIX");
	}
	if (!Array.isArray(obj.acceptance_coverage) || obj.acceptance_coverage.length === 0) missing.push("acceptance_coverage entries");
	if (obj.out_of_scope === undefined) missing.push("out_of_scope");
	if (!Array.isArray(obj.checks_run)) missing.push("checks_run array");
	if (obj.remaining_questions === undefined) missing.push("remaining_questions");
	return missing;
}

/**
 * Report parts absent from an auditor report ([] = full required structure).
 * Accepts exactly the two real shapes: the auditor.md headed-text template
 * (uppercase headers at line start) or the observed JSON schema. Token
 * presence alone never passes — a `VERDICT: PASS` line followed by a section-name
 * echo is rejected.
 */
function reportSectionBody(text: string, section: { label: string; pattern: RegExp }): string | undefined {
	const match = section.pattern.exec(text);
	if (!match) return undefined;
	const labelEnd = match.index + match[0].lastIndexOf(section.label) + section.label.length;
	const bodyStart = text.indexOf("\n", labelEnd);
	if (bodyStart < 0) return "";
	const nextHeader = new RegExp(
		`^\\s*(?:#+\\s*)?(?:${REPORT_SECTIONS.map(candidate => candidate.label).join("|")})\\b`,
		"m",
	);
	const body = text.slice(bodyStart + 1);
	const nextHeaderIndex = body.search(nextHeader);
	return (nextHeaderIndex < 0 ? body : body.slice(0, nextHeaderIndex)).trim();
}

export function missingReportParts(text: string): string[] {
	const trimmed = normalizeAuditReport(text);
	if (trimmed.startsWith("{")) {
		try {
			const parsed: unknown = JSON.parse(trimmed);
			if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
				return missingJsonReportParts(parsed as Record<string, unknown>);
			}
		} catch {
			/* fall through to headed-text validation */
		}
	}
	const verdict = parseAuditVerdict(trimmed);
	const missing = REPORT_SECTIONS.flatMap(section => {
		const body = reportSectionBody(trimmed, section);
		if (body === undefined) return [section.label];
		if (body.length === 0 && !(verdict === "PASS" && section.label === "FINDINGS")) {
			return [`${section.label} (label present but no content)`];
		}
		return [];
	});
	if (!/^\s*VERDICT\s*:\s*(?:PASS|NEEDS_FIX|BLOCKED)\b/.test(trimmed)) {
		missing.unshift("VERDICT: PASS | NEEDS_FIX | BLOCKED (must be first)");
	} else if (!verdict) {
		missing.unshift("VERDICT: PASS | NEEDS_FIX | BLOCKED");
	}
	const findings = reportSectionBody(trimmed, REPORT_SECTIONS[0]);
	if (verdict === "NEEDS_FIX" && !hasStructuredFinding(findings ?? "")) {
		missing.push("at least one finding under NEEDS_FIX");
	}
	return missing;
}

/**
 * Whether `body` carries the auditor report verbatim. Line endings are
 * normalized and outer whitespace trimmed; interior bytes must match exactly.
 */
export function reportForwarded(body: string, report: string): boolean {
	const normalize = (s: string) => s.replace(/\r\n/g, "\n").trim();
	const needle = normalize(report);
	return needle.length > 0 && normalize(body).includes(needle);
}


/**
 * Undo task-result transport wrappers (HOME-137/OMP-22): reports can arrive as
 * a JSON string literal or as the agent host's `{"report":"..."}` or
 * `{"text":"..."}` envelope — both keys observed live, and the gate must not
 * accept one while refusing the other. Per the OMP-22 security review, only the
 * EXACT single-key envelope shape unwraps (one own key, `report` or `text`,
 * string value); anything else — extra keys, both keys, non-string values,
 * structured reports — stays on the structured-JSON path and fails closed in
 * missingReportParts rather than being recovered as prose.
 */
export function decodeJsonQuoted(text: string): string {
	const trimmed = text.trim();
	if ((!trimmed.startsWith('"') || !trimmed.endsWith('"')) && !trimmed.startsWith("{")) return trimmed;
	try {
		const parsed: unknown = JSON.parse(trimmed);
		if (typeof parsed === "string") return parsed.trim();
		if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
			const keys = Object.keys(parsed);
			if (keys.length === 1 && (keys[0] === "report" || keys[0] === "text")) {
				const inner = (parsed as Record<string, unknown>)[keys[0]];
				if (typeof inner === "string") return inner.trim();
			}
		}
	} catch {
		/* not a transport wrapper — use as-is */
	}
	return trimmed;
}

/**
 * Canonical report bytes from any transport shape (HOME-137/OMP-8): first undo
 * JSON transport quoting, then — for plain headed text — slice from the first
 * line-anchored `VERDICT:` so provider/tool preamble never reaches validation
 * or the receipt. Structured JSON reports pass through for missingReportParts
 * to validate separately. Idempotent: normalized text normalizes to itself.
 */
export function normalizeAuditReport(text: string): string {
	const decoded = decodeJsonQuoted(text);
	if (decoded.startsWith("{")) return decoded;
	const verdict = /^[ \t]*VERDICT\s*:\s*(?:PASS|NEEDS_FIX|BLOCKED)\b/im.exec(decoded);
	return verdict ? decoded.slice(verdict.index).trim() : decoded;
}
/**
 * Bare auditor report from a task tool result. The task tool wraps subagent
 * output in `<task-result ...><output>...</output></task-result>` transport
 * metadata (observed in the HOME-131 dry-run transcript); the exact report
 * lives in `details.results[0].output`. Prefer details; fall back to the
 * `<output>` block, then the whole text.
 */
export function extractAuditReport(details: unknown, contentText: string): string {
	const detailOutput = (details as { results?: Array<{ output?: unknown }> } | undefined)?.results?.[0]?.output;
	if (typeof detailOutput === "string" && detailOutput.trim()) return normalizeAuditReport(detailOutput);
	const wrapped = /<output>\n?([\s\S]*?)\n?<\/output>/.exec(contentText);
	if (wrapped?.[1]?.trim()) return normalizeAuditReport(wrapped[1]);
	return normalizeAuditReport(contentText);
}

export const AUDIT_CONTRACT = auditContract.trim();

/** Retry guidance rendered into the refusal prompts ({{retry}}): one bounded
 *  replacement per /summary attempt, then honest exhaustion. */
const RETRY_ALLOWED =
	"Spawn ONE fresh replacement `auditor` task with the same five inlined sections, requiring the canonical plain headed-text report: first line `VERDICT: PASS | NEEDS_FIX | BLOCKED`, then FINDINGS, ACCEPTANCE COVERAGE, OUT OF SCOPE, CHECKS RUN, REMAINING QUESTIONS. Never JSON; never pass outputSchema. Exactly ONE replacement is allowed per /summary attempt.";
const RETRY_EXHAUSTED =
	"The auditor budget for this /summary attempt is exhausted (the one replacement was already used) — do NOT spawn another auditor. This /summary ends blocked without an audit receipt; the next owner-entered /summary starts a fresh bounded attempt.";

interface AuditGate {
	armed: boolean;
	contractInjected: boolean;
	/** Tool-call id of the accepted auditor spawn (undefined = none yet). */
	auditorCallId?: string;
	/** Bridge binding generation snapshotted when the spawn was accepted — a
	 *  rebind while the auditor runs refuses its late report (OMP-38 TOCTOU). */
	auditorBindingGeneration?: number;
	/** Accepted auditor spawns this attempt (a host-interrupted spawn that never
	 *  produced a result is released and does not count). */
	auditorAttempts: number;
	/** Two structurally unusable reports — no further auditor spawns this attempt. */
	exhausted: boolean;
	/** Verdict-structured report text, once received. */
	report?: string;
	verdict?: AuditVerdict;
	/** Missing-parts summary of the last REFUSED report (undefined = none refused). */
	lastRefusal?: string;
	forwarded: boolean;
}

const freshGate = (): AuditGate => ({ armed: false, contractInjected: false, forwarded: false, auditorAttempts: 0, exhausted: false });

export default function modelBookends(pi: ExtensionAPI) {
	// Owner session only; fail closed on hosts predating ctx.taskDepth (same rule as work-now).
	const ownerSession = (ctx: { taskDepth?: number } | undefined): boolean => ctx?.taskDepth === 0;

	let gate = freshGate();

	/** Switch to the intake role at pinned effort; false = switch impossible (fail closed). */
	async function switchToIntake(ctx: ExtensionContext): Promise<boolean> {
		const model = ctx.models.resolve("@intake");
		if (!model) {
			ctx.ui.notify("model-bookends: /intake refused — could not resolve @intake; fix modelRoles.intake and retry", "error");
			return false;
		}
		if (!(await pi.setModel(model))) {
			ctx.ui.notify(`model-bookends: /intake refused — no credential for ${model.provider}/${model.id}; log in and retry`, "error");
			return false;
		}
		pi.setThinkingLevel(INTAKE_THINKING_LEVEL);
		return true;
	}

	pi.on("session_start", async () => {
		gate = freshGate();
	});
	pi.on("session_switch", async () => {
		gate = freshGate(); // a fresh /summary means a fresh auditor — never carry an attempt across transcripts
	});

	pi.on("input", async (event, ctx) => {
		if (!ownerSession(ctx) || event.source === "extension") return undefined;
		const intake = /^\s*\/(?:skill:)?intake\b(.*)$/s.exec(event.text);
		if (intake) {
			// HOME-131: /intake runs on Fable-high or not at all — never forward on the wrong model.
			if (!(await switchToIntake(ctx))) return { handled: true };
			return { text: `/skill:intake${intake[1]}` };
		}
		if (/^\s*\/(?:skill:)?summary\b/.test(event.text)) {
			gate = { ...freshGate(), armed: true };
		}
		return undefined;
	});

	// Structured skill invocation (host-composed /skill:summary) also arms the gate.
	pi.on("message_start", async (event, ctx) => {
		if (!ownerSession(ctx) || gate.armed) return;
		const m = event.message as { role?: string; customType?: string; attribution?: string; details?: { name?: string } };
		if (m.role === "custom" && m.customType === "skill-prompt" && m.attribution === "user" && m.details?.name === "summary") {
			gate = { ...freshGate(), armed: true };
		}
	});

	pi.on("before_agent_start", async () => {
		if (!gate.armed || gate.contractInjected) return undefined;
		gate.contractInjected = true;
		return { message: { customType: "audit-contract", content: AUDIT_CONTRACT } };
	});

	pi.on("tool_call", async (event, ctx) => {
		if (!ownerSession(ctx) || !gate.armed) return undefined;

		if (event.toolName === "task") {
			const input = event.input as { tasks?: Array<{ agent?: string; task?: unknown; outputSchema?: unknown }> };
			if (!Array.isArray(input.tasks)) return undefined;
			const auditors = input.tasks.filter(task => task?.agent === "auditor");
			if (auditors.length === 0) return undefined;
			if (input.tasks.length !== 1 || auditors.length !== 1) {
				return { block: true, reason: "Audit gate: the auditor must be the only task in its batch." };
			}
			const [auditor] = auditors;
			if (typeof auditor.task !== "string") {
				return { block: true, reason: "Audit gate: auditor task must include a string body with all five required sections." };
			}
			// HOME-137: schema serialization mangles the plain-text report (JSON-quoted
			// one-liner) — the auditor returns canonical headed text, never schema output.
			if (auditor.outputSchema !== undefined && auditor.outputSchema !== null) {
				return { block: true, reason: schemaRefused.trim() };
			}
			if (gate.exhausted) {
				return { block: true, reason: `Audit gate: ${RETRY_EXHAUSTED}` };
			}
			if (gate.auditorCallId !== undefined) {
				return {
					block: true,
					reason:
						"Audit gate: exactly one auditor per /summary attempt. One already ran (or is running) — forward its report; a NEEDS_FIX ends this attempt and the next owner-entered /summary gets a fresh auditor.",
				};
			}
			const missing = missingAuditSections(auditor.task);
			if (missing.length > 0) {
				return {
					block: true,
					reason: `Audit gate: auditor task is missing required sections: ${missing.join("; ")}. Inline all five labeled sections in the task text.`,
				};
			}
			// OMP-38: the auditor audits ONE bound finalized candidate. No binding, a
			// capped packet, a wrong Final commit, or a missing plan-receipt citation
			// refuses the spawn WITHOUT reserving the bounded slot.
			const binding = currentAuditBinding();
			if (!binding) {
				return {
					block: true,
					reason:
						"Audit gate: BLOCKED — no finalized candidate is bound to this /summary attempt. Run /plan to stamp the plan, rerun /summary to freeze + finalize the candidate, then rebuild the auditor task from work get_work's PLAN PACKET.",
				};
			}
			if (binding.capped) {
				return {
					block: true,
					reason:
						"Audit gate: BLOCKED — the ledger plan packet exceeds its byte ceiling, so the audit task cannot be reconstructed from bounded ledger data. Restamp a smaller plan with /plan, rerun /summary, then audit.",
				};
			}
			const taskCommits = auditTaskFinalCommits(auditor.task);
			if (taskCommits.length !== 1 || taskCommits[0] !== binding.commitSha) {
				return {
					block: true,
					reason: `Audit gate: BLOCKED — the Final diff section must carry exactly one "Final commit: <sha>" line naming the finalized candidate commit ${binding.commitSha} byte-for-byte (found ${taskCommits.length ? taskCommits.join(", ") : "none"}). Rebuild the task from work get_work's PLAN PACKET; if the candidate moved, rerun /plan and /summary.`,
				};
			}
			const taskReceipts = auditTaskPlanReceipts(auditor.task);
			if (taskReceipts.length !== 1 || taskReceipts[0] !== binding.planReceiptSha256) {
				return {
					block: true,
					reason: `Audit gate: BLOCKED — the Approved plan section must carry exactly one "Plan receipt SHA-256: <hex>" line naming the bound plan receipt ${binding.planReceiptSha256} (the PLAN PACKET's "plan receipt sha256" value — a hash buried elsewhere is not a citation). Rebuild the task from work get_work; if the plan changed, rerun /plan and /summary.`,
				};
			}
			// Fail closed on an unresolvable audit role: the auditor's independence is
			// its fresh blocking context plus the call-bound receipt — any configured
			// @audit model (same family included) is acceptable.
			if (!ctx.models.resolve("@audit")) {
				return { block: true, reason: "Audit gate: could not resolve @audit — fix modelRoles.audit and retry." };
			}
			gate.auditorAttempts += 1;
			gate.auditorCallId = event.toolCallId;
			gate.auditorBindingGeneration = currentBindingGeneration();
			return undefined;
		}

		if (event.toolName === "work") {
			const input = event.input as { action?: string; kind?: string; body?: string };
			// Unified surface (HOME-147): BOTH backends expose the `work` tool; the
			// forward is exactly action:"append_evidence" kind:"audit".
			if (!(input.action === "append_evidence" && input.kind === "audit")) return undefined;
			if (!gate.report) {
				return {
					block: true,
					reason: gate.lastRefusal
						? prompt.render(refusedNotice, { reasons: gate.lastRefusal, retry: gate.exhausted ? RETRY_EXHAUSTED : RETRY_ALLOWED })
						: "Audit gate: run the auditor before posting the review — the review must carry its verbatim report.",
				};
			}
			if (!reportForwarded(typeof input.body === "string" ? input.body : "", gate.report)) {
				return { block: true, reason: "Audit gate: the review body must include the auditor's report VERBATIM — copy it unmodified." };
			}
			// Forwarding is confirmed on tool_result (the write may still be refused elsewhere).
			return undefined;
		}

		return undefined;
	});

	pi.on("tool_result", async (event, ctx) => {
		if (!ownerSession(ctx) || !gate.armed) return undefined;

		if (event.toolCallId === gate.auditorCallId && gate.report === undefined) {
			const contentText = event.content
				.map(part => (part.type === "text" ? part.text : ""))
				.join("\n")
				.trim();
			if (auditorRunInterrupted(event.details, contentText)) {
				// Interrupted run, not a refused report: release the reservation without
				// consuming the bounded replacement (mirrors the session_stop stale-
				// reservation path) and tell the session plainly.
				gate.auditorCallId = undefined;
				gate.auditorAttempts = Math.max(0, gate.auditorAttempts - 1);
				return {
					content: [
						...event.content,
						{
							type: "text",
							text: "Audit gate: the auditor run was interrupted before it reported — nothing was refused and no replacement was consumed. Spawn a fresh auditor task when ready.",
						},
					],
				};
			}
			if (gate.auditorBindingGeneration !== currentBindingGeneration()) {
				// OMP-38 TOCTOU: the candidate binding changed while the auditor ran —
				// its report audits a stale candidate. Release the reservation without
				// consuming the bounded replacement; a fresh /summary spawns fresh.
				gate.auditorCallId = undefined;
				gate.auditorAttempts = Math.max(0, gate.auditorAttempts - 1);
				return {
					content: [
						...event.content,
						{
							type: "text",
							text: "Audit gate: the candidate binding changed while the auditor ran — its report audits a stale candidate and was NOT accepted (no replacement consumed). Rerun /summary and spawn a fresh auditor from the new PLAN PACKET.",
						},
					],
				};
			}
			const text = extractAuditReport(event.details, contentText);
			const missingParts = event.isError ? ["(auditor task failed)"] : missingReportParts(text);
			if (missingParts.length > 0) {
				// Failed or structurally incomplete run: tell the SESSION why (HOME-137) — a
				// side-channel-only warning left the model believing no audit ran, looping
				// paid retries. The first unusable report frees the slot for ONE replacement;
				// the second exhausts the attempt (OMP-11 bounded retries).
				gate.auditorCallId = undefined;
				gate.lastRefusal = missingParts.join("; ");
				if (gate.auditorAttempts >= 2) gate.exhausted = true;
				return {
					content: [
						...event.content,
						{
							type: "text",
							text: prompt.render(refusedNotice, { reasons: gate.lastRefusal, retry: gate.exhausted ? RETRY_EXHAUSTED : RETRY_ALLOWED }),
						},
					],
				};
			}
			const verdict = parseAuditVerdict(text);
			if (!verdict) return undefined; // unreachable: missingReportParts covers it
			gate.report = text;
			gate.verdict = verdict;
			gate.lastRefusal = undefined;
			// HOME-147: the work backend's append_evidence kind:"audit" binds the exact
			// persisted bytes through this receipt — register what the REAL auditor emitted.
			registerAuditReceipt(text, verdict);
			return undefined;
		}

		// The workflow host refuses writes as NORMAL results (isError false,
		// details.success false): forwarding is confirmed only on an explicit
		// success:true review write.
		if (event.toolName === "work" && gate.report && !event.isError) {
			const input = event.input as { action?: string; kind?: string; body?: string };
			const success = (event.details as { success?: boolean } | undefined)?.success === true;
			const isForward = input.action === "append_evidence" && input.kind === "audit";
			if (success && isForward && reportForwarded(typeof input.body === "string" ? input.body : "", gate.report)) {
				gate.forwarded = true;
				gate.armed = false; // attempt complete — PASS or NEEDS_FIX alike; verdict consequences live in the review
			}
		}
		return undefined;
	});

	// Fail-closed settlement gate: while armed, the session may not settle until the
	// report exists AND has been copied verbatim into the typed review — unless the
	// bounded auditor budget is exhausted, in which case this /summary ends honestly
	// blocked with no audit receipt. A stop with a reserved call but no result means
	// the host interrupted it; release that stale reservation (without burning the
	// bounded replacement) so the stop guidance can be followed.
	pi.on("session_stop", async (_event, ctx) => {
		if (!ownerSession(ctx) || !gate.armed) return undefined;
		if (!gate.report) {
			if (gate.exhausted) {
				ctx.ui.notify(
					"model-bookends: auditor budget exhausted — this /summary ends blocked without an audit receipt; the next owner-entered /summary starts a fresh attempt",
					"warning",
				);
				return undefined;
			}
			if (gate.auditorCallId !== undefined) {
				gate.auditorCallId = undefined;
				gate.auditorAttempts = Math.max(0, gate.auditorAttempts - 1);
			}
			return {
				continue: true,
				additionalContext: gate.lastRefusal
					? prompt.render(stopRefused, { reasons: gate.lastRefusal, retry: RETRY_ALLOWED }).trim()
					: stopNoAudit.trim(),
			};
		}
		if (!gate.forwarded) {
			return { continue: true, additionalContext: stopNotForwarded.trim() };
		}
		return undefined;
	});
}
