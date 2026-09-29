/**
 * Non-interactive hypothesis tournament capability.
 *
 * Usage: bun session-system/capabilities/tournament.ts --input <file.json>
 *
 * Judges are model judges from the bundled catalog. `deps.complete` replaces
 * the provider call. Prints one JSON object. Bad input and tournament failures
 * print {"error":"..."} and exit 1. Never prompts.
 */
import * as path from "node:path";
import { getEnvApiKey, type Api, type Model } from "@oh-my-pi/pi-ai";
import { type GeneratedProvider, getBundledModel, getBundledProviders } from "@oh-my-pi/pi-catalog";
import {
	createModelJudge,
	type SimpleCompleter,
} from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/model-judge";
import { prepareHypotheses, type HypothesisInput } from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/prepare";
import { runTournament } from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/runner";
import type { ComparisonRecord, RankedHypothesis } from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/types";

/** Matches the autoresearch setting default so short write-ups stay verbatim. */
const MAX_JUDGE_CHARS = 6000;

export interface TournamentJudgeSpec {
	id: string;
	family: string;
	/** Bundled catalog id, `provider/model`. */
	model: string;
}

export interface TournamentCapabilityInput {
	id: string;
	question: string;
	hypotheses: HypothesisInput[];
	judges: TournamentJudgeSpec[];
	seed: number;
	swissRoundCap: number;
}

export interface TournamentCapabilityDeps {
	complete?: SimpleCompleter;
}

export interface TournamentCapabilityResult {
	capability: "tournament";
	passed: boolean;
	standings: RankedHypothesis[];
	comparisons: ComparisonRecord[];
}

function requireRecord(value: unknown, label: string): Record<string, unknown> {
	if (!value || typeof value !== "object" || Array.isArray(value)) {
		throw new Error(`${label} must be an object`);
	}
	return value as Record<string, unknown>;
}

function requireNonEmptyString(value: unknown, label: string): string {
	if (typeof value !== "string" || value.trim().length === 0) {
		throw new Error(`${label} must be a non-empty string`);
	}
	return value;
}

function requireFiniteNumber(value: unknown, label: string): number {
	if (typeof value !== "number" || !Number.isFinite(value)) {
		throw new Error(`${label} must be a finite number`);
	}
	return value;
}

export function parseTournamentInput(value: unknown): TournamentCapabilityInput {
	const record = requireRecord(value, "tournament input");
	const hypothesesRaw = record.hypotheses;
	if (!Array.isArray(hypothesesRaw)) throw new Error("hypotheses must be an array");
	const hypotheses: HypothesisInput[] = hypothesesRaw.map((entry, index) => {
		const hypothesis = requireRecord(entry, `hypotheses[${index}]`);
		return {
			id: requireNonEmptyString(hypothesis.id, `hypotheses[${index}].id`),
			title: requireNonEmptyString(hypothesis.title, `hypotheses[${index}].title`),
			writeUp: requireNonEmptyString(hypothesis.writeUp, `hypotheses[${index}].writeUp`),
		};
	});
	const judgesRaw = record.judges;
	if (!Array.isArray(judgesRaw)) throw new Error("judges must be an array");
	const judges: TournamentJudgeSpec[] = judgesRaw.map((entry, index) => {
		const judge = requireRecord(entry, `judges[${index}]`);
		return {
			id: requireNonEmptyString(judge.id, `judges[${index}].id`),
			family: requireNonEmptyString(judge.family, `judges[${index}].family`),
			model: requireNonEmptyString(judge.model, `judges[${index}].model`),
		};
	});
	return {
		id: requireNonEmptyString(record.id, "id"),
		question: requireNonEmptyString(record.question, "question"),
		hypotheses,
		judges,
		seed: requireFiniteNumber(record.seed, "seed"),
		swissRoundCap: requireFiniteNumber(record.swissRoundCap, "swissRoundCap"),
	};
}

function resolveCatalogModel(spec: string): Model<Api> {
	const slash = spec.indexOf("/");
	if (slash <= 0 || slash === spec.length - 1) {
		throw new Error(`Judge model must be "provider/id", received ${JSON.stringify(spec)}`);
	}
	const provider = spec.slice(0, slash);
	const modelId = spec.slice(slash + 1);
	if (!(getBundledProviders() as readonly string[]).includes(provider)) {
		throw new Error(`Unknown catalog provider "${provider}" in judge model "${spec}"`);
	}
	const model = getBundledModel(provider as GeneratedProvider, modelId);
	if (!model) throw new Error(`Unknown catalog model "${spec}"`);
	return model as Model<Api>;
}

export async function runTournamentCapability(
	input: TournamentCapabilityInput,
	deps?: TournamentCapabilityDeps,
): Promise<TournamentCapabilityResult> {
	const parsed = parseTournamentInput(input);
	const { hypotheses } = await prepareHypotheses(parsed.hypotheses, { maxJudgeChars: MAX_JUDGE_CHARS });
	const judges = parsed.judges.map(spec => {
		const slash = spec.model.indexOf("/");
		const provider = spec.model.slice(0, slash);
		return createModelJudge({
			id: spec.id,
			family: spec.family,
			model: resolveCatalogModel(spec.model),
			apiKey: getEnvApiKey(provider),
			seed: parsed.seed,
			...(deps?.complete ? { complete: deps.complete } : {}),
		});
	});
	const tournament = await runTournament({
		id: parsed.id,
		question: parsed.question,
		hypotheses,
		judges,
		swissRoundCap: parsed.swissRoundCap,
		seed: parsed.seed,
	});
	return {
		capability: "tournament",
		passed: true,
		standings: tournament.result.ranking,
		comparisons: tournament.comparisons,
	};
}

function parseCliArgs(argv: string[]): string {
	let inputPath: string | undefined;
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i] ?? "";
		if (arg === "--input" || arg.startsWith("--input=")) {
			const value = arg.startsWith("--input=") ? arg.slice("--input=".length) : argv[++i];
			if (!value) throw new Error("--input requires a file");
			inputPath = value;
			continue;
		}
		throw new Error(`unknown argument ${arg}`);
	}
	if (!inputPath) throw new Error("usage: tournament.ts --input <file.json>");
	return path.resolve(inputPath);
}

async function readInput(inputPath: string): Promise<unknown> {
	let text: string;
	try {
		text = await Bun.file(inputPath).text();
	} catch (err) {
		throw new Error(`cannot read tournament input: ${err instanceof Error ? err.message : String(err)}`);
	}
	try {
		return JSON.parse(text) as unknown;
	} catch (err) {
		throw new Error(`invalid tournament input JSON: ${err instanceof Error ? err.message : String(err)}`);
	}
}

async function main(): Promise<void> {
	try {
		const inputPath = parseCliArgs(process.argv.slice(2));
		const result = await runTournamentCapability(parseTournamentInput(await readInput(inputPath)));
		process.stdout.write(`${JSON.stringify(result)}\n`);
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		process.stdout.write(`${JSON.stringify({ error: message })}\n`);
		process.exitCode = 1;
	}
}

if (import.meta.main) {
	await main();
}
