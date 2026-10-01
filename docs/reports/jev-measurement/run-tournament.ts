#!/usr/bin/env bun
/**
 * run-tournament.ts
 *
 * Owner-run CLI for the R1-J Jev tournament-judge measurement. It builds the
 * two real judges (Jev typed client and a chat model of another family), runs
 * the harness in `tournament-harness.ts`, and writes the report and results.
 *
 * Owner slice (key read at run time, never stored, never logged):
 *
 *   bun docs/reports/jev-measurement/run-tournament.ts \
 *     --pairs docs/reports/jev-measurement/tournament-pairs.json \
 *     --hypotheses docs/reports/jev-measurement/tournament-hypotheses.json \
 *     --question "Which mechanism best explains the effect?" \
 *     --out docs/reports/jev-tournament-measurement-report.md \
 *     --json docs/reports/jev-tournament-results.json
 *
 * The typed Jev key is read from `~/.config/omp/jev.env` (`TYPESAFE_API_KEY`)
 * when the run starts. The chat judge runs the owner's configured smol model
 * through the normal registry, so the second family is a real model.
 *
 * Nothing in the implementer slice runs this file; tests drive the injected-judge
 * harness in `tournament-harness.ts` instead, so no test reads the env file or
 * reaches a real endpoint.
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import { classifyModel } from "@oh-my-pi/pi-catalog/identity";
import { createJevJudge } from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/jev-judge";
import { createModelJudge, createModelSummarizer } from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/model-judge";
import {
	type HypothesisInput,
	prepareHypotheses,
	type Summarizer,
} from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/prepare";
import type { TournamentJudge } from "@oh-my-pi/pi-coding-agent/autoresearch/tournament/types";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { resolveRoleSelection } from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { discoverAuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-broker-config";
import { JEV_ENV_KEY } from "@oh-my-pi/pi-coding-agent/tiny/jev-client";
import { lookup } from "@oh-my-pi/pi-coding-agent/config/registry";
import { parseEnvFile } from "@oh-my-pi/pi-utils";
import { defaultConfigHome, jevEnvPath } from "./router-transport";
import { type LabeledPair, type TournamentMeasurementResults, runTournamentMeasurement } from "./tournament-harness";

/** Path label for the run, without the key value. */
export function keySourceLabel(configHome: string): string {
	return `${jevEnvPath(configHome)} (${JEV_ENV_KEY})`;
}

/**
 * Read the owner's typed Jev key from `~/.config/omp/jev.env` at run time.
 * A missing file or key returns `undefined`; the value is never logged.
 */
export function readJevKey(configHome: string = defaultConfigHome()): string | undefined {
	const value = parseEnvFile(jevEnvPath(configHome))[JEV_ENV_KEY];
	return value && value.trim() ? value.trim() : undefined;
}

function formatPercent(value: number): string {
	return `${(value * 100).toFixed(1)}%`;
}

function formatNumber(value: number, decimals = 1): string {
	return value.toFixed(decimals);
}

function formatUsd(value: number): string {
	return `$${value.toFixed(4)}`;
}

/** Fill every `{{placeholder}}` in the report template from the results. */
export function renderTournamentReport(results: TournamentMeasurementResults, template: string): string {
	let rendered = template;
	const fill = (key: string, value: string) => {
		rendered = rendered.replaceAll(`{{${key}}}`, value);
	};

	const pair = results.labeledPairSet;
	fill("pairs_count", String(pair.pairs));
	fill("jev_pair_agreement", formatPercent(pair.jev.agreementWithReference));
	fill("chat_pair_agreement", formatPercent(pair.chat.agreementWithReference));
	fill("inter_judge_agreement", formatPercent(pair.interJudgeAgreement));
	fill("jev_tie_rate", formatPercent(pair.jev.tieRate));
	fill("chat_tie_rate", formatPercent(pair.chat.tieRate));
	fill("jev_order_invariance", formatPercent(pair.orderInvariance.jev));
	fill("chat_order_invariance", formatPercent(pair.orderInvariance.chat));
	fill("jev_off_list_rate", formatPercent(pair.offListRate.jev));
	fill("chat_off_list_rate", formatPercent(pair.offListRate.chat));
	fill("jev_pair_p50", formatNumber(pair.jev.p50LatencyMs));
	fill("jev_pair_p95", formatNumber(pair.jev.p95LatencyMs));
	fill("chat_pair_p50", formatNumber(pair.chat.p50LatencyMs));
	fill("chat_pair_p95", formatNumber(pair.chat.p95LatencyMs));

	const tournament = results.tournament20;
	fill("tournament_pool_size", String(tournament.poolSize));
	fill(
		"tournament_schedule",
		tournament.schedule.kind === "round-robin"
			? "round-robin"
			: `swiss (round cap: ${tournament.schedule.roundCap})`,
	);
	fill("tournament_question", tournament.question);

	for (const [prefix, stats] of [
		["chat", tournament.chat],
		["jev", tournament.jev],
	] as const) {
		fill(`${prefix}_comparisons`, String(stats.comparisons));
		fill(`${prefix}_calls`, String(stats.calls));
		fill(`${prefix}_failures`, String(stats.failures));
		fill(`${prefix}_cost`, formatUsd(stats.costUsd));
		fill(`${prefix}_tokens`, String(stats.totalTokens));
		fill(`${prefix}_wall_time`, formatNumber(stats.wallTimeMs, 0));
		fill(`${prefix}_p50`, formatNumber(stats.p50LatencyMs));
		fill(`${prefix}_p95`, formatNumber(stats.p95LatencyMs));
	}

	return rendered;
}

export interface TournamentJudges {
	jev: TournamentJudge;
	chat: TournamentJudge;
}

export interface BuildJudgesOptions {
	configHome?: string;
	seed: number;
	/** Key override for tests; the CLI reads the env file when omitted. */
	apiKey?: string;
	/** Chat model role chain; defaults to smol then tiny. */
	chatRoles?: readonly string[];
}

export interface BuiltJudges {
	judges: TournamentJudges;
	summarizer: Summarizer;
}

/**
 * Build the two real judges. The Jev judge is the typed client; the chat judge
 * is the owner's configured smol model, forced into a distinct family for the
 * bias check.
 */
export async function buildJudges(options: BuildJudgesOptions): Promise<BuiltJudges> {
	const apiKey = options.apiKey ?? readJevKey(options.configHome);
	if (!apiKey) {
		throw new Error(`no typed Jev key found in ${keySourceLabel(options.configHome ?? defaultConfigHome())}`);
	}

	const settings = await Settings.loadReadOnly();
	const registry = new ModelRegistry(await discoverAuthStorage(), undefined, { settings });
	const selection = resolveRoleSelection(options.chatRoles ?? ["smol", "tiny"], settings, registry.getAvailable());
	if (!selection) {
		throw new Error("no chat model resolved for the tournament second family");
	}
	const chatModel = selection.model;
	const chatClass = classifyModel(chatModel.provider, chatModel.id, { lenient: true }).class;
	const chatFamily = chatClass !== "unknown" ? chatClass : chatModel.provider.toLowerCase();
	const chatApiKey = await registry.getApiKey(chatModel);

	const jev = createJevJudge({
		deps: {
			getSetting: (path: string) => lookup(path)?.get(settings),
			getApiKey: async () => apiKey,
			recordUsage: () => {},
		},
	});
	const chat = createModelJudge({
		id: chatModel.id,
		family: chatFamily,
		model: chatModel,
		apiKey: chatApiKey,
		seed: options.seed,
	});
	const summarizer = createModelSummarizer({ model: chatModel, apiKey: chatApiKey });
	return { judges: { jev, chat }, summarizer };
}

async function loadJson<T>(file: string): Promise<T> {
	const text = await Bun.file(file).text();
	return JSON.parse(text) as T;
}

export interface RunTournamentCliOptions {
	pairsPath: string;
	hypothesesPath: string;
	outPath: string;
	jsonPath?: string;
	configHome?: string;
	seed: number;
	question: string;
	swissRoundCap: number;
	maxJudgeChars: number;
	apiKey?: string;
	chatRoles?: readonly string[];
	templatePath?: string;
	/** Test seam: build judges without touching the real registry or env file. */
	build?: (options: BuildJudgesOptions) => Promise<BuiltJudges>;
}

export async function runTournamentCli(options: RunTournamentCliOptions): Promise<TournamentMeasurementResults> {
	const pairs = await loadJson<LabeledPair[]>(options.pairsPath);
	const inputs = await loadJson<HypothesisInput[]>(options.hypothesesPath);

	const build = options.build ?? buildJudges;
	const { judges, summarizer } = await build({
		configHome: options.configHome,
		seed: options.seed,
		apiKey: options.apiKey,
		chatRoles: options.chatRoles,
	});

	const { hypotheses } = await prepareHypotheses(inputs, {
		maxJudgeChars: options.maxJudgeChars,
		summarizer,
	});

	const results = await runTournamentMeasurement({
		pairs,
		hypotheses,
		question: options.question,
		jev: judges.jev,
		chat: judges.chat,
		swissRoundCap: options.swissRoundCap,
		seed: options.seed,
	});

	const templatePath = options.templatePath ?? path.join(import.meta.dir, "tournament-report-template.md");
	let template: string;
	try {
		template = await Bun.file(templatePath).text();
	} catch {
		template = "# Jev Tournament Judge Measurement Report\n\n{{pairs_count}} pairs measured.\n";
	}

	await fs.mkdir(path.dirname(options.outPath), { recursive: true });
	await Bun.write(options.outPath, renderTournamentReport(results, template));
	if (options.jsonPath) {
		await Bun.write(options.jsonPath, `${JSON.stringify(results, null, 2)}\n`);
	}
	return results;
}

export interface RunTournamentMainDeps {
	build?: (options: BuildJudgesOptions) => Promise<BuiltJudges>;
}

export async function main(
	argv: string[] = process.argv.slice(2),
	deps: RunTournamentMainDeps = {},
): Promise<TournamentMeasurementResults> {
	let pairsPath = "";
	let hypothesesPath = "";
	let outPath = "docs/reports/jev-tournament-measurement-report.md";
	let jsonPath: string | undefined;
	let configHome: string | undefined;
	let seed = 2026;
	let question = "";
	let swissRoundCap = 5;
	let maxJudgeChars = 6000;

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i] ?? "";
		const take = (): string => argv[++i] ?? "";
		if (arg === "--pairs") pairsPath = take();
		else if (arg.startsWith("--pairs=")) pairsPath = arg.slice("--pairs=".length);
		else if (arg === "--hypotheses") hypothesesPath = take();
		else if (arg.startsWith("--hypotheses=")) hypothesesPath = arg.slice("--hypotheses=".length);
		else if (arg === "--out") outPath = take();
		else if (arg.startsWith("--out=")) outPath = arg.slice("--out=".length);
		else if (arg === "--json") jsonPath = take();
		else if (arg.startsWith("--json=")) jsonPath = arg.slice("--json=".length);
		else if (arg === "--config-home") configHome = take();
		else if (arg.startsWith("--config-home=")) configHome = arg.slice("--config-home=".length);
		else if (arg === "--seed") seed = Number.parseInt(take(), 10);
		else if (arg.startsWith("--seed=")) seed = Number.parseInt(arg.slice("--seed=".length), 10);
		else if (arg === "--question") question = take();
		else if (arg.startsWith("--question=")) question = arg.slice("--question=".length);
		else if (arg === "--swiss-round-cap") swissRoundCap = Number.parseInt(take(), 10);
		else if (arg.startsWith("--swiss-round-cap="))
			swissRoundCap = Number.parseInt(arg.slice("--swiss-round-cap=".length), 10);
		else if (arg === "--max-judge-chars") maxJudgeChars = Number.parseInt(take(), 10);
		else if (arg.startsWith("--max-judge-chars="))
			maxJudgeChars = Number.parseInt(arg.slice("--max-judge-chars=".length), 10);
		else {
			console.error(`usage error: unexpected argument ${arg}`);
			process.exit(2);
		}
	}

	if (!pairsPath || !hypothesesPath || !question) {
		console.error(
			"Usage: run-tournament.ts --pairs <file.json> --hypotheses <file.json> --question <text> [options]",
		);
		process.exit(2);
	}

	try {
		const results = await runTournamentCli({
			pairsPath,
			hypothesesPath,
			outPath,
			jsonPath,
			configHome,
			seed,
			question,
			swissRoundCap,
			maxJudgeChars,
			build: deps.build,
		});
		console.log(
			`tournament judge: ${results.labeledPairSet.pairs} pairs, 20-hypothesis schedule ${results.tournament20.schedule.kind}`,
		);
		console.log(`key read from ${keySourceLabel(configHome ?? defaultConfigHome())}`);
		return results;
	} catch (error) {
		console.error(error instanceof Error ? error.message : String(error));
		process.exit(1);
	}
}

if (import.meta.main) {
	await main();
}
