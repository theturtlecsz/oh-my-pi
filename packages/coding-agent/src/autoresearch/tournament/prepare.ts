import type { Usage } from "@oh-my-pi/pi-ai";
import { addUsageTotals, createUsageTotals } from "../../utils/usage-totals";
import type { Hypothesis } from "./types";

export interface HypothesisInput {
	id: string;
	title: string;
	writeUp: string;
}

export interface Summarizer {
	summarize(writeUp: string, maxChars: number, signal?: AbortSignal): Promise<{ text: string; usage?: Usage }>;
}

export interface PrepareHypothesesOptions {
	maxJudgeChars: number;
	summarizer?: Summarizer;
	signal?: AbortSignal;
}

export interface PreparedHypotheses {
	hypotheses: Hypothesis[];
	usage: Usage;
}

const REDACTED = "[redacted]";

function escapeRegExp(value: string): string {
	return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Replace every case-insensitive occurrence of the hypothesis id and title with
 * `[redacted]` so a blinded judge cannot recover which hypothesis it is reading.
 * Longer patterns are tried first so an id nested inside a title is fully masked.
 */
export function redactIdentity(text: string, identity: { id: string; title: string }): string {
	const patterns = [identity.id, identity.title]
		.filter(part => part.length > 0)
		.sort((a, b) => b.length - a.length)
		.map(escapeRegExp);
	if (patterns.length === 0) return text;
	return text.replace(new RegExp(patterns.join("|"), "gi"), REDACTED);
}

/**
 * Build the blinded judge text for each hypothesis. Write-ups that already fit
 * within `maxJudgeChars` are redacted verbatim; longer ones are summarized (never
 * truncated) and the summary is redacted. The full write-up is always preserved.
 */
export async function prepareHypotheses(
	inputs: HypothesisInput[],
	options: PrepareHypothesesOptions,
): Promise<PreparedHypotheses> {
	if (inputs.length < 2) {
		throw new Error(`prepareHypotheses requires at least 2 hypotheses, received ${inputs.length}`);
	}

	const seen = new Set<string>();
	for (const input of inputs) {
		if (input.id.trim().length === 0) {
			throw new Error("Hypothesis id must not be empty");
		}
		if (input.title.trim().length === 0) {
			throw new Error(`Hypothesis ${input.id} has an empty title`);
		}
		if (input.writeUp.trim().length === 0) {
			throw new Error(`Hypothesis ${input.id} has an empty write-up`);
		}
		if (seen.has(input.id)) {
			throw new Error(`Duplicate hypothesis id: ${input.id}`);
		}
		seen.add(input.id);
	}

	const usage = createUsageTotals();
	const hypotheses: Hypothesis[] = [];

	for (const input of inputs) {
		const identity = { id: input.id, title: input.title };

		if (input.writeUp.length <= options.maxJudgeChars) {
			hypotheses.push({
				id: input.id,
				title: input.title,
				writeUp: input.writeUp,
				judgeText: redactIdentity(input.writeUp, identity),
				summarized: false,
			});
			continue;
		}

		if (!options.summarizer) {
			throw new Error(
				`Hypothesis ${input.id} write-up is ${input.writeUp.length} chars, exceeding maxJudgeChars ${options.maxJudgeChars}; a summarizer is required`,
			);
		}

		const result = await options.summarizer.summarize(input.writeUp, options.maxJudgeChars, options.signal);
		if (result.usage) {
			addUsageTotals(usage, result.usage);
		}

		const judgeText = redactIdentity(result.text, identity);
		if (judgeText.length > options.maxJudgeChars) {
			throw new Error(
				`Hypothesis ${input.id} summary is ${judgeText.length} chars, exceeding maxJudgeChars ${options.maxJudgeChars}`,
			);
		}

		hypotheses.push({
			id: input.id,
			title: input.title,
			writeUp: input.writeUp,
			judgeText,
			summarized: true,
		});
	}

	return { hypotheses, usage };
}
