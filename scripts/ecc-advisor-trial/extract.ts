#!/usr/bin/env bun
// extract.ts — turn one ECC Database Reviewer advisor trial into a reviewable
// artifact (OMP-525).
//
// The trial runs `omp -p --advisor` against a fixed case and leaves the advisor's
// transcript under the sessions tree. This extractor is deliberately strict: a
// run that did not finish, produced no transcript, or exited non-zero is refused
// wholesale (exit 1, nothing written), so a partial or failed trial can never be
// mistaken for a usable sample. On success it copies the transcript byte-exact
// and writes the notes the reviewer reads.
//
//   bun scripts/ecc-advisor-trial/extract.ts \
//     --case <id> --commit <40 hex> --exit-code <n> --sessions <dir> --out <dir>

import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import type { ToolResultMessage } from "@oh-my-pi/pi-ai";
import { ADVISOR_ACK_SUPPRESSED } from "@oh-my-pi/pi-coding-agent/advisor/advise-tool";
import { slugifyAdvisorName } from "@oh-my-pi/pi-coding-agent/advisor/config";
import { advisorTranscriptFilename } from "@oh-my-pi/pi-coding-agent/advisor/transcript-recorder";
import { loadSessionMessagesReadOnly } from "@oh-my-pi/pi-coding-agent/session/session-loader";

/** Canonical advisor the ECC trial measures. */
export const ECC_ADVISOR_NAME = "ECC Database Reviewer";
/** How the trial drives the advisor, recorded verbatim in `notes.json`. */
export const ECC_MECHANISM = "omp -p --advisor (WATCHDOG.yml roster)";
/** Byte-exact transcript is copied to this name under `--out`. */
export const TRANSCRIPT_COPY_NAME = "advisor-transcript.jsonl";
/** Reviewer-facing summary is written to this name under `--out`. */
export const NOTES_NAME = "notes.json";

/** One counted finding: an advise call the primary actually received. */
export interface AdvisorTrialNote {
	/** 1-based position among findings, in transcript order. */
	n: number;
	tool_call_id: string;
	note: string;
	severity: string | null;
	category: string | null;
}

/** The reviewer-facing `notes.json` payload. */
export interface AdvisorTrialNotes {
	case: string;
	commit: string;
	advisor: string;
	mechanism: string;
	omp_exit: number;
	advisor_completed: boolean;
	model: string;
	transcript_sha256: string;
	suppressed: number;
	failed: number;
	unmatched: number;
	notes: AdvisorTrialNote[];
}

export interface ExtractAdvisorRunOptions {
	case: string;
	commit: string;
	exitCode: number;
	sessions: string;
	out: string;
}

export type AdvisorRunOutcome =
	| { ok: true; notes: AdvisorTrialNotes; transcriptPath: string; notesPath: string }
	| { ok: false; reason: string };

/** The transcript filename a named advisor writes: `__advisor.<slug>.jsonl`. */
export function advisorTrialTranscriptFilename(): string {
	return advisorTranscriptFilename(slugifyAdvisorName(ECC_ADVISOR_NAME));
}

/** Recursively collect files whose basename matches, so a flat or nested layout both work. */
async function findFilesNamed(root: string, basename: string): Promise<string[]> {
	const found: string[] = [];
	const stack = [root];
	while (stack.length > 0) {
		const dir = stack.pop()!;
		const entries = await fs.readdir(dir, { withFileTypes: true }).catch(() => []);
		for (const entry of entries) {
			const full = path.join(dir, entry.name);
			if (entry.isDirectory()) stack.push(full);
			else if (entry.isFile() && entry.name === basename) found.push(full);
		}
	}
	found.sort();
	return found;
}

/** Concatenate a message's text blocks, matching how the ack is rendered. */
function resultText(message: ToolResultMessage): string {
	let text = "";
	for (const block of message.content) {
		if (block.type === "text") text += block.text;
	}
	return text;
}

function adviseArgs(block: { arguments: Record<string, unknown> }): {
	note: string;
	severity: string | null;
	category: string | null;
} {
	const args = block.arguments;
	return {
		note: typeof args.note === "string" ? args.note : "",
		severity: typeof args.severity === "string" ? args.severity : null,
		category: typeof args.category === "string" ? args.category : null,
	};
}

/**
 * Decide whether a completed transcript yields a usable advisor run, and on
 * success write the review artifact. Every rejection returns `ok: false`
 * **before** touching `--out`, so a refused run leaves the output directory
 * exactly as it found it.
 */
export async function extractAdvisorRun(options: ExtractAdvisorRunOptions): Promise<AdvisorRunOutcome> {
	if (options.exitCode !== 0) {
		return { ok: false, reason: `omp exited ${options.exitCode}; a failed trial is not reviewable` };
	}

	const transcriptName = advisorTrialTranscriptFilename();
	const matches = await findFilesNamed(options.sessions, transcriptName);
	if (matches.length !== 1) {
		return {
			ok: false,
			reason: `expected exactly one ${transcriptName} under ${options.sessions}, found ${matches.length}`,
		};
	}
	const transcriptSource = matches[0];

	const messages: AgentMessage[] = await loadSessionMessagesReadOnly(transcriptSource);
	const last = messages[messages.length - 1];
	if (last === undefined || last.role !== "assistant") {
		return { ok: false, reason: "advisor did not complete: no trailing assistant message" };
	}
	if (last.stopReason !== "stop") {
		return { ok: false, reason: `advisor did not complete: last assistant stopReason is "${last.stopReason}"` };
	}

	const suppressedAcks = new Set(Object.values(ADVISOR_ACK_SUPPRESSED));
	const resultsById = new Map<string, ToolResultMessage>();
	for (const message of messages) {
		if (message.role === "toolResult") resultsById.set(message.toolCallId, message);
	}

	let suppressed = 0;
	let failed = 0;
	const findings: Omit<AdvisorTrialNote, "n">[] = [];
	for (const message of messages) {
		if (message.role !== "assistant") continue;
		for (const block of message.content) {
			if (block.type !== "toolCall" || block.name !== "advise") continue;
			const result = resultsById.get(block.id);
			if (!result) continue; // no result on this transcript path
			if (result.isError) {
				failed++;
				continue;
			}
			if (suppressedAcks.has(resultText(result))) {
				suppressed++;
				continue;
			}
			const { note, severity, category } = adviseArgs(block);
			findings.push({ tool_call_id: block.id, note, severity, category });
		}
	}

	// `loadSessionMessagesReadOnly` strips every tool call that has no result on
	// the transcript path (the dangling-call count survives on the turn). That is
	// "unmatched" here: a call the primary never received a result for. It is not
	// a finding, because there is no acknowledgment to judge.
	let unmatched = 0;
	for (const message of messages) {
		unmatched += (message as AgentMessage & { strippedToolCalls?: number }).strippedToolCalls ?? 0;
	}

	const notes: AdvisorTrialNotes = {
		case: options.case,
		commit: options.commit,
		advisor: ECC_ADVISOR_NAME,
		mechanism: ECC_MECHANISM,
		omp_exit: options.exitCode,
		advisor_completed: true,
		model: `${last.provider}/${last.model}`,
		transcript_sha256: new Bun.CryptoHasher("sha256").update(await Bun.file(transcriptSource).bytes()).digest("hex"),
		suppressed,
		failed,
		unmatched,
		notes: findings.map((finding, index) => ({ n: index + 1, ...finding })),
	};

	const transcriptPath = path.join(options.out, TRANSCRIPT_COPY_NAME);
	const notesPath = path.join(options.out, NOTES_NAME);
	await Bun.write(transcriptPath, Bun.file(transcriptSource));
	await Bun.write(notesPath, `${JSON.stringify(notes, null, "\t")}\n`);

	return { ok: true, notes, transcriptPath, notesPath };
}

function parseArgs(argv: readonly string[]): Record<string, string> {
	const parsed: Record<string, string> = {};
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (!arg.startsWith("--")) continue;
		const eq = arg.indexOf("=");
		if (eq !== -1) {
			parsed[arg.slice(2, eq)] = arg.slice(eq + 1);
		} else if (i + 1 < argv.length) {
			parsed[arg.slice(2)] = argv[++i];
		}
	}
	return parsed;
}

if (import.meta.main) {
	const args = parseArgs(process.argv.slice(2));
	const required = ["case", "commit", "exit-code", "sessions", "out"] as const;
	const missing = required.filter(key => !args[key]);
	if (missing.length > 0) {
		console.error(`extract: missing required flags: ${missing.map(key => `--${key}`).join(", ")}`);
		process.exit(1);
	}
	const outcome = await extractAdvisorRun({
		case: args.case,
		commit: args.commit,
		exitCode: Number.parseInt(args["exit-code"], 10),
		sessions: args.sessions,
		out: args.out,
	});
	if (!outcome.ok) {
		console.error(`extract: ${outcome.reason}`);
		process.exit(1);
	}
}
