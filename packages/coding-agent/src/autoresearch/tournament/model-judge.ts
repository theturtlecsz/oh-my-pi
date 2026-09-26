import {
	type Api,
	type ApiKey,
	type AssistantMessage,
	type Context,
	completeSimple,
	type Model,
	type SimpleStreamOptions,
	type Usage,
} from "@oh-my-pi/pi-ai";
import { prompt } from "@oh-my-pi/pi-utils";
import judgePromptTemplate from "./judge-prompt.md" with { type: "text" };
import type { Summarizer } from "./prepare";
import summarizePromptTemplate from "./summarize-prompt.md" with { type: "text" };
import {
	createSeededRng,
	type JudgeOutcome,
	type JudgeProbabilities,
	type JudgeVerdict,
	type TournamentJudge,
	type TournamentJudgeOptions,
} from "./types";

export const NEUTRAL_LABEL_POOL: readonly string[] = [
	"Amber",
	"Amethyst",
	"Aquamarine",
	"Azurite",
	"Beryl",
	"Calcite",
	"Carnelian",
	"Chalcedony",
	"Citrine",
	"Diamond",
	"Emerald",
	"Fluorite",
	"Garnet",
	"Hematite",
	"Jade",
	"Jasper",
	"Kyanite",
	"Lapis",
	"Malachite",
	"Moonstone",
	"Obsidian",
	"Onyx",
	"Opal",
	"Peridot",
	"Pyrite",
	"Quartz",
	"Rhodonite",
	"Ruby",
	"Sapphire",
	"Selenite",
	"Serpentine",
	"Spinel",
	"Sunstone",
	"Tanzanite",
	"Topaz",
	"Tourmaline",
	"Turquoise",
	"Zircon",
];

export function drawTwoLabels(pool: readonly string[], rng: () => number): [string, string] {
	if (pool.length < 2) {
		throw new Error("Neutral label pool must contain at least 2 labels");
	}
	const idx1 = Math.floor(rng() * pool.length);
	let idx2 = Math.floor(rng() * (pool.length - 1));
	if (idx2 >= idx1) {
		idx2 += 1;
	}
	return [pool[idx1]!, pool[idx2]!];
}

export interface ParsedJudgeAnswer {
	outcome: JudgeOutcome;
	probabilities?: JudgeProbabilities;
}

/**
 * Strict parser for judge model responses. Requires exactly one JSON object with
 * a recognized winner (labelA -> "A", labelB -> "B", "tie" -> "tie").
 * Throws on malformed JSON, missing/unknown winner, or stopReason error.
 */
export function parseJudgeAnswer(text: string, labelA: string, labelB: string): ParsedJudgeAnswer {
	if (!labelA || !labelB || labelA === labelB) {
		throw new Error("parseJudgeAnswer requires two distinct labels");
	}

	let trimmed = text.trim();
	if (trimmed.startsWith("```json")) {
		trimmed = trimmed
			.replace(/^```json\s*/i, "")
			.replace(/\s*```$/, "")
			.trim();
	} else if (trimmed.startsWith("```")) {
		trimmed = trimmed
			.replace(/^```\s*/, "")
			.replace(/\s*```$/, "")
			.trim();
	}

	let parsed: unknown;
	try {
		parsed = JSON.parse(trimmed);
	} catch (err) {
		throw new Error(`Malformed judge response: invalid JSON: ${err instanceof Error ? err.message : String(err)}`);
	}

	if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
		throw new Error("Malformed judge response: expected a single JSON object");
	}

	const record = parsed as Record<string, unknown>;

	if (record.stopReason === "error") {
		throw new Error(`Judge response indicated stopReason error: ${String(record.errorMessage ?? "unknown error")}`);
	}

	if (typeof record.winner !== "string") {
		throw new Error(`Malformed judge response: missing or invalid "winner" field`);
	}

	const winner = record.winner.trim();
	let outcome: JudgeOutcome;

	if (winner.toLowerCase() === labelA.toLowerCase()) {
		outcome = "A";
	} else if (winner.toLowerCase() === labelB.toLowerCase()) {
		outcome = "B";
	} else if (winner.toLowerCase() === "tie") {
		outcome = "tie";
	} else {
		throw new Error(`Unknown judge winner "${winner}"; expected "${labelA}", "${labelB}", or "tie"`);
	}

	let probabilities: JudgeProbabilities | undefined;
	if (record.probabilities && typeof record.probabilities === "object" && !Array.isArray(record.probabilities)) {
		const probRecord = record.probabilities as Record<string, unknown>;
		const rawA = probRecord[labelA] ?? probRecord[labelA.toLowerCase()] ?? probRecord.A ?? probRecord.a;
		const rawB = probRecord[labelB] ?? probRecord[labelB.toLowerCase()] ?? probRecord.B ?? probRecord.b;
		const hasTie = "tie" in probRecord || "Tie" in probRecord || "TIE" in probRecord;
		const rawTie = probRecord.tie ?? probRecord.Tie ?? probRecord.TIE;

		const validA = typeof rawA === "number" && !Number.isNaN(rawA) && rawA >= 0 && rawA <= 1;
		const validB = typeof rawB === "number" && !Number.isNaN(rawB) && rawB >= 0 && rawB <= 1;
		const validTie = !hasTie || (typeof rawTie === "number" && !Number.isNaN(rawTie) && rawTie >= 0 && rawTie <= 1);

		if (validA && validB && validTie) {
			probabilities = { A: rawA as number, B: rawB as number };
			if (hasTie && typeof rawTie === "number") {
				probabilities.tie = rawTie;
			}
		}
	}

	return {
		outcome,
		...(probabilities ? { probabilities } : {}),
	};
}

export type SimpleCompleter = (
	model: Model<Api>,
	context: Context,
	options?: SimpleStreamOptions,
) => Promise<AssistantMessage>;

export interface CreateModelJudgeOptions {
	id: string;
	family: string;
	model: Model<Api>;
	apiKey?: ApiKey;
	seed: number;
	complete?: SimpleCompleter;
	signal?: AbortSignal;
}

export function createModelJudge(options: CreateModelJudgeOptions): TournamentJudge {
	const { id, family, model, apiKey, seed, complete = completeSimple, signal: defaultSignal } = options;
	const rng = createSeededRng(seed);

	return {
		id,
		family,
		async judge(aText: string, bText: string, judgeOptions: TournamentJudgeOptions): Promise<JudgeVerdict> {
			const [labelA, labelB] = drawTwoLabels(NEUTRAL_LABEL_POOL, rng);

			const promptText = prompt.render(judgePromptTemplate, {
				question: judgeOptions.question,
				labelA,
				labelB,
				textA: aText,
				textB: bText,
			});

			const effectiveSignal =
				judgeOptions.signal && defaultSignal
					? AbortSignal.any([defaultSignal, judgeOptions.signal])
					: (judgeOptions.signal ?? defaultSignal);

			const response = await complete(
				model,
				{
					messages: [{ role: "user", content: promptText, timestamp: Date.now() }],
				},
				{
					apiKey,
					disableReasoning: true,
					signal: effectiveSignal,
				},
			);

			if (response.stopReason === "error") {
				throw new Error(`Judge completion failed: ${response.errorMessage ?? "unknown error"}`);
			}

			const text = response.content
				.filter((part): part is { type: "text"; text: string } => part.type === "text")
				.map(part => part.text)
				.join("\n")
				.trim();

			if (!text) {
				throw new Error("Judge response contained no text content");
			}

			const parsed = parseJudgeAnswer(text, labelA, labelB);

			return {
				outcome: parsed.outcome,
				...(parsed.probabilities ? { probabilities: parsed.probabilities } : {}),
				usage: response.usage,
			};
		},
	};
}

export interface CreateModelSummarizerOptions {
	model: Model<Api>;
	apiKey?: ApiKey;
	complete?: SimpleCompleter;
}

export function createModelSummarizer(options: CreateModelSummarizerOptions): Summarizer {
	const { model, apiKey, complete = completeSimple } = options;

	return {
		async summarize(
			writeUp: string,
			maxChars: number,
			signal?: AbortSignal,
		): Promise<{ text: string; usage?: Usage }> {
			const promptText = prompt.render(summarizePromptTemplate, { maxChars, writeUp });

			const response = await complete(
				model,
				{
					messages: [{ role: "user", content: promptText, timestamp: Date.now() }],
				},
				{
					apiKey,
					disableReasoning: true,
					signal,
				},
			);

			if (response.stopReason === "error") {
				throw new Error(`Summarizer completion failed: ${response.errorMessage ?? "unknown error"}`);
			}

			const text = response.content
				.filter((part): part is { type: "text"; text: string } => part.type === "text")
				.map(part => part.text)
				.join("\n")
				.trim();

			return {
				text,
				usage: response.usage,
			};
		},
	};
}
