/**
 * workflow/session-ledger.ts — the Session Ledger note (OMP-69).
 *
 * After each settled owner turn that performed real work, reconstruct one
 * short plain-human note — what happened, where things stand, what Chris does
 * next — from the turn's own transcript span plus bounded Work Ledger reads.
 * Delivered once as a displayed `session-ledger-summary` custom message with
 * `deliverAs: "nextTurn"`, so Chris sees it AND the next assistant turn
 * receives the identical persisted content without a second model call.
 *
 * Never writes to the Work Ledger. Fails open: missing @advisor model,
 * missing credential, provider failure, or malformed output degrades to the
 * literal unavailable line — the session is never blocked and no extra turn
 * is ever triggered. Output-quarantine framing is OMP-70, not here.
 */
import { completeSimple } from "@oh-my-pi/pi-ai";
import {
	type AgentEndEvent,
	buildSecretObfuscator,
	Container,
	type ExtensionAPI,
	getAgentDir,
	getMarkdownTheme,
	Markdown,
	type SecretObfuscator,
	Spacer,
	Text,
} from "@oh-my-pi/pi-coding-agent";
import { prompt } from "@oh-my-pi/pi-utils";
import type { WorkflowBackend } from "./backend";
import ledgerPromptTemplate from "./session-ledger-prompt.md" with { type: "text" };

export const SESSION_LEDGER_TYPE = "session-ledger-summary";
export const SESSION_LEDGER_UNAVAILABLE = "SESSION LEDGER UNAVAILABLE: summary could not be generated.";

const LABELS = ["WHAT HAPPENED:", "WHERE THINGS STAND:", "WHAT YOU NEED TO DO:"] as const;

export interface SessionLedgerDeps {
	backend: WorkflowBackend;
	getApiKey(provider: string): Promise<string | undefined>;
	/** Test seam — production default is pi-ai's completeSimple. */
	complete?: typeof completeSimple;
	/** Test seam — production default builds the shared session obfuscator. */
	buildObfuscator?: (cwd: string) => Promise<SecretObfuscator | undefined>;
}

/**
 * Strict response contract: after trimming outer whitespace only, exactly one
 * non-empty single line per label, byte-for-byte labels, in order, no interior
 * blank lines, no CRLF, nothing else. Returns the validated note or undefined.
 */
export function validateLedgerNote(text: string): string | undefined {
	const trimmed = text.trim();
	if (trimmed.includes("\r")) return undefined;
	const lines = trimmed.split("\n");
	if (lines.length !== LABELS.length) return undefined;
	for (let i = 0; i < LABELS.length; i++) {
		const label = LABELS[i] as string;
		const line = lines[i] as string;
		if (!line.startsWith(label)) return undefined;
		if (line.slice(label.length).trim().length === 0) return undefined;
	}
	return trimmed;
}

type SpanMessage = AgentEndEvent["messages"][number];

function blockText(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	const parts: string[] = [];
	for (const block of content) {
		if (block === null || typeof block !== "object" || !("type" in block)) continue;
		if (block.type === "text" && "text" in block && typeof block.text === "string") parts.push(block.text);
		else if (block.type === "image") parts.push("[image]");
	}
	return parts.join("\n");
}

/**
 * Slice from the final user message through settle. No user message in the
 * transcript (internal/custom continuation) → empty span: never serialize
 * old or full history for a turn the owner did not prompt.
 */
export function selectSpan(messages: readonly SpanMessage[]): SpanMessage[] {
	for (let i = messages.length - 1; i >= 0; i--) {
		if (messages[i]?.role === "user") return messages.slice(i);
	}
	return [];
}

/** Real work = the slice carries assistant content or tool results. */
export function spanHasActivity(span: readonly SpanMessage[]): boolean {
	return span.some(message => {
		if (message.role === "toolResult") return true;
		return message.role === "assistant" && message.content.length > 0;
	});
}

/**
 * Full-fidelity serialization of the settled span: tool arguments and tool
 * results verbatim, never truncated — the whole serialized text passes
 * through the shared secret obfuscator before prompt assembly.
 */
export function serializeSpan(span: readonly SpanMessage[]): string {
	const out: string[] = [];
	for (const message of span) {
		switch (message.role) {
			case "user":
				out.push(`USER:\n${blockText(message.content)}`);
				break;
			case "assistant":
				for (const block of message.content) {
					if (block.type === "text" && block.text.trim()) {
						out.push(`ASSISTANT:\n${block.text}`);
					} else if (block.type === "toolCall") {
						out.push(`TOOL CALL ${block.name}: ${JSON.stringify(block.arguments ?? {})}`);
					}
				}
				break;
			case "toolResult":
				out.push(`TOOL RESULT ${message.toolName}${message.isError ? " (error)" : ""}:\n${blockText(message.content)}`);
				break;
			default: {
				const text = "content" in message ? blockText(message.content) : "";
				if (!text.trim()) break;
				const kind = "customType" in message && typeof message.customType === "string" ? ` ${message.customType}` : "";
				out.push(`CONTEXT${kind}:\n${text}`);
			}
		}
	}
	return out.join("\n\n");
}

/**
 * Bounded read-only ledger context: NOW identity, state, blockers/relations,
 * and the digest packet. Degrades honestly — a ledger outage never blocks the
 * note, and this module never writes to the backend.
 */
async function ledgerContext(backend: WorkflowBackend): Promise<string> {
	try {
		const now = await backend.currentNow();
		if (!now) return "NOW: none selected";
		const lines = [`NOW: ${now.key} — ${now.title}${now.project ? ` (${now.project})` : ""}`];
		try {
			const detail = await backend.issueDetail(now.key);
			lines.push(`STATE: ${detail.state}`);
			if (detail.blockedBy.length > 0) lines.push(`BLOCKED BY: ${detail.blockedBy.join(", ")}`);
			if (detail.blocks.length > 0) lines.push(`BLOCKS: ${detail.blocks.join(", ")}`);
			if (detail.related.length > 0) lines.push(`RELATED: ${detail.related.join(", ")}`);
			lines.push("── digest packet ──", detail.digestPacket);
		} catch {
			lines.push("(detail unavailable)");
		}
		return lines.join("\n");
	} catch {
		return "(ledger unavailable)";
	}
}

export function registerSessionLedger(pi: ExtensionAPI, deps: SessionLedgerDeps): void {
	const complete = deps.complete ?? completeSimple;
	const buildObfuscator = deps.buildObfuscator ?? ((cwd: string) => buildSecretObfuscator(cwd, getAgentDir()));

	// One arm per owner agent run: set on agent_start, consumed by the first
	// terminal agent_end, reset on session boundaries. Duplicate terminal
	// events can never emit twice.
	let armed = false;
	// Session generation: bumped on every session boundary. An in-flight note
	// captures the generation at settle entry and re-checks it before sending,
	// so a note generated for one transcript can never land in the next.
	let generation = 0;
	// Reservation barrier: the in-flight generate-and-deliver operation for the
	// last settled owner turn. The next owner prompt's before_agent_start hook
	// awaits it, so a prompt submitted immediately after settle still receives
	// the identical note (sent, dropped as stale, or failed open) in-context.
	let pendingNote: Promise<void> | undefined;

	pi.registerMessageRenderer(SESSION_LEDGER_TYPE, (message, _options, theme) => {
		const container = new Container();
		container.addChild(new Text(theme.fg("accent", "Session Ledger"), 1, 0));
		container.addChild(
			new Markdown(blockText(message.content), 1, 0, getMarkdownTheme(), {
				color: (text: string) => theme.fg("customMessageText", text),
			}),
		);
		container.addChild(new Spacer(1));
		return container;
	});

	pi.on("session_start", async () => {
		armed = false;
		generation++;
	});
	pi.on("session_switch", async () => {
		armed = false;
		generation++;
	});
	pi.on("agent_start", async (_event, ctx) => {
		if (ctx.taskDepth === 0) armed = true;
	});

	pi.on("agent_end", async (event, ctx) => {
		if (ctx.taskDepth !== 0) return;
		if (event.willContinue === true) return;
		if (!armed) return;
		armed = false; // cleared before any async work — duplicates cannot re-enter
		const settledGeneration = generation;
		const span = selectSpan(event.messages);
		if (!spanHasActivity(span)) return;

		const op = (async () => {
			let content = SESSION_LEDGER_UNAVAILABLE;
			try {
				const model = ctx.models.resolve("@advisor");
				const key = model ? await deps.getApiKey(model.provider) : undefined;
				if (model && key) {
					const [obfuscator, ledger] = await Promise.all([buildObfuscator(ctx.cwd), ledgerContext(deps.backend)]);
					const rendered = prompt.render(ledgerPromptTemplate, {
						ledgerContext: obfuscator ? obfuscator.obfuscate(ledger) : ledger,
						span: obfuscator ? obfuscator.obfuscate(serializeSpan(span)) : serializeSpan(span),
					});
					const res = await complete(
						model,
						{ messages: [{ role: "user", content: rendered, timestamp: Date.now() }] },
						{ apiKey: key, disableReasoning: true },
					);
					// pi-ai surfaces provider failures IN-BAND — completeSimple does not throw.
					if (res.stopReason !== "error" && res.stopReason !== "aborted") {
						const text = res.content
							.filter(block => block.type === "text")
							.map(block => block.text)
							.join("\n");
						const validated = validateLedgerNote(text);
						if (validated) content = validated;
					}
				}
			} catch (error) {
				// Fail open: the note degrades to the unavailable line, never throws.
				try {
					pi.logger.warn("session-ledger: note generation failed", { error: String(error) });
				} catch {
					/* headless */
				}
			}
			if (generation !== settledGeneration) return; // session boundary crossed mid-flight — stale note, drop it
			try {
				pi.sendMessage(
					{ customType: SESSION_LEDGER_TYPE, content, display: true },
					{ deliverAs: "nextTurn", triggerTurn: false },
				);
			} catch {
				// The op must never reject: a rejecting pendingNote would propagate
				// through the next prompt's awaited before_agent_start barrier.
			}
		})();
		pendingNote = op;
		try {
			await op;
		} finally {
			if (pendingNote === op) pendingNote = undefined;
		}
	});

	// Owner-depth barrier: the immediately following prompt cannot pass its
	// pre-agent hook until the prior settled turn's note has been resolved.
	// Never triggers a synthetic turn and never polls.
	pi.on("before_agent_start", async (_event, ctx) => {
		if (ctx.taskDepth !== 0) return;
		const pending = pendingNote;
		if (pending) await pending;
	});
}
