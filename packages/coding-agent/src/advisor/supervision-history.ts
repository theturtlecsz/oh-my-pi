import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import type { CpkSentinelLabel } from "../extensibility/cpk/supervision-proposal";
import { assertCpkSentinelLabels } from "../extensibility/cpk/supervision-proposal";
import { visitEntriesFromFileStream } from "../session/session-loader";
import type { AdvisorCategory, AdvisorSeverity } from "./advise-tool";
import { ADVISOR_WIP_MARKER } from "./delta-split";

/**
 * One replayable unit of the advisor's supervision conversation, recovered from
 * an {@link AdvisorTranscriptRecorder} file (OMP-208).
 *
 * - `update` collapses a run of consecutive persisted user messages — the
 *   rendered "Session update" chunks the advisor runtime delivers — into one
 *   event. `inProgress` is true when the run's last user text ends with
 *   {@link ADVISOR_WIP_MARKER}, i.e. was rendered with `willContinue`.
 * - `advise` is one assistant `advise` tool call, in transcript order, carrying
 *   the arguments the model supplied.
 *
 * The shape is deliberately delivery-agnostic: it records what the advisor
 * *said*, not what the emission guard chose to forward. The CPK-6 corpus drives
 * both through the legacy pair to measure catching power.
 */
export type AdvisorReplayEvent =
	| { type: "update"; inProgress: boolean }
	| { type: "advise"; note: string; severity?: AdvisorSeverity; category?: AdvisorCategory };

/** A sentinel session: a replayed transcript plus the defects the legacy pair must still catch. */
export interface AdvisorSentinelSession {
	/** Corpus id — the `<name>` of `<name>.jsonl` / `<name>.labels.json`. */
	id: string;
	events: AdvisorReplayEvent[];
	/** Validated labels; `transcriptIndex` is an index into `events` that points at an `advise` event. */
	expected: CpkSentinelLabel[];
}

function contentBlocks(message: AgentMessage): unknown[] {
	const content = (message as { content?: unknown }).content;
	return Array.isArray(content) ? content : [];
}

/** Flatten a message's text content (string or text blocks). Empty for other kinds. */
function messageText(message: AgentMessage): string {
	const content = (message as { content?: unknown }).content;
	if (typeof content === "string") return content;
	let text = "";
	for (const block of contentBlocks(message)) {
		if (!block || typeof block !== "object") continue;
		const b = block as { type?: unknown; text?: unknown };
		if (b.type === "text" && typeof b.text === "string") text += b.text;
	}
	return text;
}

function toAdviseEvent(block: unknown): AdvisorReplayEvent | undefined {
	if (!block || typeof block !== "object") return undefined;
	const b = block as { type?: unknown; name?: unknown; arguments?: unknown };
	if (b.type !== "toolCall" || b.name !== "advise") return undefined;
	const raw = b.arguments;
	const args: Record<string, unknown> = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
	const note = typeof args.note === "string" ? args.note : "";
	const severity =
		args.severity === "nit" || args.severity === "concern" || args.severity === "blocker" ? args.severity : undefined;
	const category = typeof args.category === "string" ? (args.category as AdvisorCategory) : undefined;
	return {
		type: "advise",
		note,
		...(severity ? { severity } : {}),
		...(category ? { category } : {}),
	};
}

/**
 * Recover the advisor's supervision conversation from a persisted
 * {@link AdvisorTranscriptRecorder} file.
 *
 * A run of consecutive `user` messages becomes one `update` (the runtime renders
 * a wip/final batch as one or more consecutive user chunks); each assistant
 * `advise` tool call becomes one `advise`, in file order. Non-message entries
 * (the session header, tool results, a stray title slot) and non-`advise` tool
 * calls are skipped.
 */
export async function loadAdvisorReplayEvents(file: string): Promise<AdvisorReplayEvent[]> {
	const events: AdvisorReplayEvent[] = [];
	let pendingUserTexts: string[] = [];
	const flushUpdate = (): void => {
		if (pendingUserTexts.length === 0) return;
		const last = pendingUserTexts[pendingUserTexts.length - 1];
		events.push({ type: "update", inProgress: last.endsWith(ADVISOR_WIP_MARKER) });
		pendingUserTexts = [];
	};

	await visitEntriesFromFileStream(file, entry => {
		if (entry.type !== "message") {
			flushUpdate();
			return;
		}
		const message = entry.message;
		if (message.role === "user") {
			pendingUserTexts.push(messageText(message));
			return;
		}
		flushUpdate();
		if (message.role !== "assistant") return;
		for (const block of contentBlocks(message)) {
			const advice = toAdviseEvent(block);
			if (advice) events.push(advice);
		}
	});
	flushUpdate();
	return events;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Load a directory of sentinel sessions: each `<name>.jsonl` (recorder format)
 * with its sibling `<name>.labels.json` describing the defects the legacy pair
 * must still catch (`{"expected":[{ruleClass, transcriptIndex}, ...]}`).
 *
 * Labels are validated against the recovered event count, so an unknown rule
 * class or an out-of-range `transcriptIndex` fails closed with `invalid_sentinel`
 * before any comparison runs. Sessions come back sorted by id.
 */
export async function loadAdvisorSentinelCorpus(dir: string): Promise<AdvisorSentinelSession[]> {
	const dirents = await fs.readdir(dir, { withFileTypes: true });
	const ids = dirents
		.filter(dirent => dirent.isFile() && dirent.name.endsWith(".jsonl"))
		.map(dirent => dirent.name.slice(0, -".jsonl".length))
		.sort();

	const sessions: AdvisorSentinelSession[] = [];
	for (const id of ids) {
		const events = await loadAdvisorReplayEvents(path.join(dir, `${id}.jsonl`));
		const raw: unknown = await Bun.file(path.join(dir, `${id}.labels.json`)).json();
		const expected = assertCpkSentinelLabels(isRecord(raw) ? raw.expected : undefined, events.length);
		sessions.push({ id, events, expected });
	}
	return sessions;
}
