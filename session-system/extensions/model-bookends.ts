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
 * verbatim report has been copied into the typed Linear review comment
 * (kind:"review"). A NEEDS_FIX report still forwards — it ends the summary
 * attempt; fixes happen afterward and the next owner-entered /summary starts a
 * fresh auditor (state resets on session switch).
 */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
// Static audit instructions (HOME-131) — installed alongside this extension so the
// relative import resolves in both link and copy modes.
import auditContract from "./model-bookends-audit.md" with { type: "text" };
import stopNoAudit from "./model-bookends-stop-no-audit.md" with { type: "text" };
import stopNotForwarded from "./model-bookends-stop-not-forwarded.md" with { type: "text" };

/** Effort pinned by HOME-131: /intake always runs Fable at :high. */
export const INTAKE_THINKING_LEVEL = "high";

/** The five labeled sections every auditor task must inline (HOME-131 input contract). */
export const AUDIT_INPUT_SECTIONS: ReadonlyArray<{ label: string; pattern: RegExp }> = [
	{ label: "Approved plan", pattern: /approved plan/gi },
	{ label: "Acceptance criteria", pattern: /acceptance criteria/gi },
	{ label: "Starting state (commit + pre-existing dirty files)", pattern: /starting (?:state|commit)/gi },
	{ label: "Final diff", pattern: /final diff/gi },
	{ label: "Verification results", pattern: /verification/gi },
];

/** Body of a labeled section: text after the label up to the next section label or end. */
function auditSectionBody(task: string, section: { pattern: RegExp }): string | undefined {
	section.pattern.lastIndex = 0;
	const match = section.pattern.exec(task);
	if (!match) return undefined;
	const start = match.index + match[0].length;
	let end = task.length;
	for (const other of AUDIT_INPUT_SECTIONS) {
		if (other.pattern === section.pattern) continue;
		other.pattern.lastIndex = start;
		const next = other.pattern.exec(task);
		if (next && next.index < end) end = next.index;
	}
	return task.slice(start, end).replace(/^[\s:—-]+/, "");
}

/**
 * Sections absent OR empty in an auditor task body ([] = complete). The label
 * alone is not enough: each section must carry actual content — the plan text,
 * the criteria, the commit + dirty files, the diff, the results — because the
 * auditor audits only what it receives.
 */
export function missingAuditSections(task: string): string[] {
	const missing: string[] = [];
	for (const section of AUDIT_INPUT_SECTIONS) {
		const body = auditSectionBody(task, section);
		if (body === undefined) {
			missing.push(section.label);
		} else if (!/(?:\S\s*){8,}/.test(body)) {
			missing.push(`${section.label} (label present but no content)`);
		}
	}
	return missing;
}

export type AuditVerdict = "PASS" | "NEEDS_FIX" | "BLOCKED";

/** Canonical headed-text sections (auditor.md report template). Case-sensitive and
 *  line-anchored so a one-line token echo cannot satisfy the structure check. */
export const REPORT_SECTIONS: ReadonlyArray<{ label: string; pattern: RegExp }> = [
	{ label: "FINDINGS", pattern: /^\s*(?:#+\s*)?FINDINGS\b/m },
	{ label: "ACCEPTANCE COVERAGE", pattern: /^\s*(?:#+\s*)?ACCEPTANCE COVERAGE\b/m },
	{ label: "OUT OF SCOPE", pattern: /^\s*(?:#+\s*)?OUT OF SCOPE\b/m },
	{ label: "CHECKS RUN", pattern: /^\s*(?:#+\s*)?CHECKS RUN\b/m },
	{ label: "REMAINING QUESTIONS", pattern: /^\s*(?:#+\s*)?REMAINING QUESTIONS\b/m },
];

/** First verdict token in a report, or undefined when the report is not verdict-structured. */
export function parseAuditVerdict(text: string): AuditVerdict | undefined {
	const match = /VERDICT["']?\s*:\s*["']?(PASS|NEEDS_FIX|BLOCKED)\b/i.exec(text);
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
	else if (verdict === "NEEDS_FIX" && obj.findings.length === 0) missing.push("at least one finding under NEEDS_FIX");
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
export function missingReportParts(text: string): string[] {
	const trimmed = text.trim();
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
	const missing = REPORT_SECTIONS.filter(section => !section.pattern.test(text)).map(section => section.label);
	if (!/^\s*(?:#+\s*)?VERDICT\s*:\s*(?:PASS|NEEDS_FIX|BLOCKED)\b/m.test(text)) {
		missing.unshift("VERDICT: PASS | NEEDS_FIX | BLOCKED");
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
 * Bare auditor report from a task tool result. The task tool wraps subagent
 * output in `<task-result ...><output>...</output></task-result>` transport
 * metadata (observed in the HOME-131 dry-run transcript); the exact report
 * lives in `details.results[0].output`. Prefer details; fall back to the
 * `<output>` block, then the whole text.
 */
export function extractAuditReport(details: unknown, contentText: string): string {
	const detailOutput = (details as { results?: Array<{ output?: unknown }> } | undefined)?.results?.[0]?.output;
	if (typeof detailOutput === "string" && detailOutput.trim()) return detailOutput.trim();
	const wrapped = /<output>\n?([\s\S]*?)\n?<\/output>/.exec(contentText);
	if (wrapped?.[1]?.trim()) return wrapped[1].trim();
	return contentText.trim();
}

export const AUDIT_CONTRACT = auditContract.trim();

interface AuditGate {
	armed: boolean;
	contractInjected: boolean;
	/** Tool-call id of the accepted auditor spawn (undefined = none yet). */
	auditorCallId?: string;
	/** Verdict-structured report text, once received. */
	report?: string;
	verdict?: AuditVerdict;
	forwarded: boolean;
}

const freshGate = (): AuditGate => ({ armed: false, contractInjected: false, forwarded: false });

export default function modelBookends(pi: ExtensionAPI) {
	// Owner session only; fail closed on hosts predating ctx.taskDepth (same rule as linear-now).
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
			const input = event.input as { tasks?: Array<{ agent?: string; task?: unknown }> };
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
			gate.auditorCallId = event.toolCallId;
			return undefined;
		}

		if (event.toolName === "linear") {
			const input = event.input as { action?: string; kind?: string; body?: string };
			if (input.action !== "comment" || input.kind !== "review") return undefined;
			if (!gate.report) {
				return { block: true, reason: "Audit gate: run the auditor before posting the review — the review must carry its verbatim report." };
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
			const text = extractAuditReport(event.details, contentText);
			const missingParts = event.isError ? ["(auditor task failed)"] : missingReportParts(text);
			if (missingParts.length > 0) {
				// Failed or structurally incomplete run: release the slot so a FRESH auditor can be spawned.
				gate.auditorCallId = undefined;
				try {
					ctx.ui.notify(
						`Audit gate: auditor report unusable — missing ${missingParts.join("; ")}. Spawn a fresh auditor.`,
						"warning",
					);
				} catch {
					/* headless */
				}
				return undefined;
			}
			const verdict = parseAuditVerdict(text);
			if (!verdict) return undefined; // unreachable: missingReportParts covers it
			gate.report = text;
			gate.verdict = verdict;
			return undefined;
		}

		// linear-now refuses writes as NORMAL results (isError false, details.success false):
		// forwarding is confirmed only on an explicit success:true review write.
		if (event.toolName === "linear" && gate.report && !event.isError) {
			const input = event.input as { action?: string; kind?: string; body?: string };
			const success = (event.details as { success?: boolean } | undefined)?.success === true;
			if (
				success &&
				input.action === "comment" &&
				input.kind === "review" &&
				reportForwarded(typeof input.body === "string" ? input.body : "", gate.report)
			) {
				gate.forwarded = true;
				gate.armed = false; // attempt complete — PASS or NEEDS_FIX alike; verdict consequences live in the review
			}
		}
		return undefined;
	});

	// Fail-closed settlement gate: while armed, the session may not settle until the
	// report exists AND has been copied verbatim into the typed review. Reset only via
	// session switch (fresh /summary → fresh auditor).
	pi.on("session_stop", async (_event, ctx) => {
		if (!ownerSession(ctx) || !gate.armed) return undefined;
		if (!gate.report) {
			return { continue: true, additionalContext: stopNoAudit.trim() };
		}
		if (!gate.forwarded) {
			return { continue: true, additionalContext: stopNotForwarded.trim() };
		}
		return undefined;
	});
}
