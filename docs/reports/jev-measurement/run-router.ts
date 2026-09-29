#!/usr/bin/env bun
/**
 * run-router.ts
 *
 * Runs the router-mode measurement over the OMP-298 sets and writes
 * results.json + router-measurement-report.md.
 *
 * Owner slice (run at run time, key never stored):
 *
 *   bun docs/reports/jev-measurement/build-sets.ts --sessions ~/.omp/agent/sessions --no-robomp --out ~/.omp/jev-router-sets
 *   bun docs/reports/jev-measurement/run-router.ts --sets ~/.omp/jev-router-sets \
 *     --out docs/reports/jev-router-measurement-report.md --generation-records
 *
 * The key is read from `~/.config/omp/jev.env` (`OPENROUTER_API_KEY`) when the
 * run starts, and the transport is `makeOpenRouterFetch()` — origin-locked to
 * `https://openrouter.ai`, so nothing else can receive the key.
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { FetchImpl } from "@oh-my-pi/pi-utils";
import { type CurrentSmolHarness } from "./harness";
import {
	type RouterCurrentHandler,
	type RouterIssueItem,
	type RouterMeasurementResults,
	type RouterPromptItem,
	type RouterTurnEndItem,
	runRouterMeasurement,
} from "./router-harness";
import { makeOpenRouterFetch, readJevEnvKey, ROUTER_ENV_DIR, ROUTER_ENV_FILE } from "./router-transport";

function formatPercent(value?: number): string {
	if (value === undefined || Number.isNaN(value)) return "0.0%";
	return `${(value * 100).toFixed(1)}%`;
}

function formatNumber(value?: number, decimals = 1): string {
	if (value === undefined || Number.isNaN(value)) return "0.0";
	return value.toFixed(decimals);
}

function formatUsd(value?: number): string {
	if (value === undefined || Number.isNaN(value)) return "$0.0000";
	return `$${value.toFixed(4)}`;
}

function formatHistogram(histogram: Record<string, number>): string {
	const entries = Object.entries(histogram).sort((a, b) => b[1] - a[1]);
	if (entries.length === 0) return "unavailable";
	return entries.map(([key, count]) => `${key} x${count}`).join(", ");
}

/** Human-readable key source, without the value. */
export function keySourceLabel(configHome: string): string {
	return `${path.join(configHome, ROUTER_ENV_DIR, ROUTER_ENV_FILE)} (${ROUTER_ENV_DIR}/${ROUTER_ENV_FILE})`;
}

export function renderRouterReport(results: RouterMeasurementResults, template: string): string {
	let rendered = template;
	const fill = (key: string, value: string) => {
		rendered = rendered.replaceAll(`{{${key}}}`, value);
	};

	const at = results.features.auto_thinking_route;
	const us = results.features.unexpected_stop_route;
	const ro = results.features.robomp_route;

	fill("router_model", results.routerModel);
	fill("verdict", results.verdict);
	fill("auto_thinking_sample_size", String(results.sampleSizes.auto_thinking));
	fill("unexpected_stop_sample_size", String(results.sampleSizes.unexpected_stop));
	fill("robomp_sample_size", String(results.sampleSizes.robomp));

	for (const [prefix, metrics] of [
		["auto_thinking_route", at],
		["unexpected_stop_route", us],
		["robomp_route", ro],
	] as const) {
		fill(`${prefix}_accuracy`, formatPercent(metrics.accuracy));
		fill(`${prefix}_answer_rate`, formatPercent(metrics.answerRate));
		fill(`${prefix}_p50`, formatNumber(metrics.p50LatencyMs, 1));
		fill(`${prefix}_p95`, formatNumber(metrics.p95LatencyMs, 1));
		fill(`${prefix}_cost`, formatUsd(metrics.costPer1000Usd));
		fill(`${prefix}_cost_source`, metrics.costSource);
		fill(`${prefix}_unparseable`, formatPercent(metrics.unparseableRate));
		fill(`${prefix}_transport_failure`, formatPercent(metrics.transportFailureRate));
		fill(`${prefix}_routed_models`, formatHistogram(metrics.routedModels));
		fill(`${prefix}_routed_effort`, formatHistogram(metrics.routedEffort));
	}

	fill("unexpected_stop_route_precision", formatPercent(us.precision));
	fill("unexpected_stop_route_recall", formatPercent(us.recall));
	fill("robomp_route_skip_share", formatPercent(ro.skipSessionShare));

	const currentAt = results.current.auto_thinking;
	const currentUs = results.current.unexpected_stop;
	fill("auto_thinking_current_accuracy", currentAt ? formatPercent(currentAt.accuracy) : "not measured");
	fill("auto_thinking_current_p50", currentAt ? formatNumber(currentAt.p50LatencyMs, 1) : "not measured");
	fill("auto_thinking_current_p95", currentAt ? formatNumber(currentAt.p95LatencyMs, 1) : "not measured");
	fill("auto_thinking_current_cost", currentAt ? formatUsd(currentAt.costPer1000Usd) : "not measured");
	fill("unexpected_stop_current_accuracy", currentUs ? formatPercent(currentUs.accuracy) : "not measured");
	fill("unexpected_stop_current_precision", currentUs?.precision ? formatPercent(currentUs.precision) : "not measured");
	fill("unexpected_stop_current_recall", currentUs?.recall ? formatPercent(currentUs.recall) : "not measured");
	fill("unexpected_stop_current_p50", currentUs ? formatNumber(currentUs.p50LatencyMs, 1) : "not measured");
	fill("unexpected_stop_current_p95", currentUs ? formatNumber(currentUs.p95LatencyMs, 1) : "not measured");
	fill("unexpected_stop_current_cost", currentUs ? formatUsd(currentUs.costPer1000Usd) : "not measured");

	return rendered;
}

async function loadJsonl<T>(file: string): Promise<T[]> {
	try {
		const text = await Bun.file(file).text();
		return text
			.split("\n")
			.map(line => line.trim())
			.filter(Boolean)
			.map(line => JSON.parse(line) as T);
	} catch {
		return [];
	}
}

export interface RunRouterOptions {
	setsDir?: string;
	apiKey: string;
	fetch: FetchImpl;
	outPath: string;
	generationRecords?: boolean;
	templatePath?: string;
	jsonPath?: string;
	prompts?: RouterPromptItem[];
	turnEnds?: RouterTurnEndItem[];
	issues?: RouterIssueItem[];
	current?: CurrentSmolHarness;
	fakeCurrent?: RouterCurrentHandler;
}

export async function runRouter(options: RunRouterOptions): Promise<RouterMeasurementResults> {
	let prompts = options.prompts ?? [];
	let turnEnds = options.turnEnds ?? [];
	let issues = options.issues ?? [];
	if (options.setsDir) {
		prompts = prompts.length ? prompts : await loadJsonl<RouterPromptItem>(path.join(options.setsDir, "prompts.jsonl"));
		const turnEndsPath = (await Bun.file(path.join(options.setsDir, "turn-ends.jsonl")).exists())
			? path.join(options.setsDir, "turn-ends.jsonl")
			: path.join(options.setsDir, "turn_ends.jsonl");
		turnEnds = turnEnds.length ? turnEnds : await loadJsonl<RouterTurnEndItem>(turnEndsPath);
		issues = issues.length ? issues : await loadJsonl<RouterIssueItem>(path.join(options.setsDir, "issues.jsonl"));
	}

	const results = await runRouterMeasurement({
		prompts,
		turnEnds,
		issues,
		apiKey: options.apiKey,
		fetch: options.fetch,
		generationRecords: options.generationRecords,
		current: options.current,
		fakeCurrent: options.fakeCurrent,
	});

	const templatePath = options.templatePath ?? path.join(import.meta.dir, "router-report-template.md");
	let template = "";
	try {
		template = await Bun.file(templatePath).text();
	} catch {
		template = "# Jev Router Measurement Report\n\n{{verdict}}\n";
	}

	await fs.mkdir(path.dirname(options.outPath), { recursive: true });
	await Bun.write(options.outPath, renderRouterReport(results, template));
	if (options.jsonPath) {
		await Bun.write(options.jsonPath, `${JSON.stringify(results, null, 2)}\n`);
	}
	return results;
}

async function main() {
	const args = process.argv.slice(2);
	let setsDir = "";
	let outPath = "docs/reports/jev-router-measurement-report.md";
	let jsonPath: string | undefined;
	let configHome: string | undefined;
	let generationRecords = false;
	let fakeTransportPath: string | undefined;
	let fakeCurrentPath: string | undefined;

	for (let i = 0; i < args.length; i++) {
		const arg = args[i];
		if (arg === "--sets") setsDir = args[++i] ?? "";
		else if (arg.startsWith("--sets=")) setsDir = arg.slice("--sets=".length);
		else if (arg === "--out") outPath = args[++i] ?? outPath;
		else if (arg.startsWith("--out=")) outPath = arg.slice("--out=".length);
		else if (arg === "--json") jsonPath = args[++i];
		else if (arg.startsWith("--json=")) jsonPath = arg.slice("--json=".length);
		else if (arg === "--config-home") configHome = args[++i];
		else if (arg.startsWith("--config-home=")) configHome = arg.slice("--config-home=".length);
		else if (arg === "--generation-records") generationRecords = true;
		else if (arg === "--fake-transport") fakeTransportPath = args[++i];
		else if (arg.startsWith("--fake-transport=")) fakeTransportPath = arg.slice("--fake-transport=".length);
		else if (arg === "--fake-current") fakeCurrentPath = args[++i];
		else if (arg.startsWith("--fake-current=")) fakeCurrentPath = arg.slice("--fake-current=".length);
		else {
			console.error(`usage error: unexpected argument ${arg}`);
			process.exit(2);
		}
	}

	// The key is read at run time from the owner's env file. Tests never reach
	// here: they call runRouter() with an explicit fake transport and key.
	const apiKey = readJevEnvKey(configHome);
	if (!apiKey) {
		const home = configHome ?? "the owner's config home";
		console.error(`ERROR: no OpenRouter key found in ${keySourceLabel(home)}`);
		process.exit(1);
	}

	let fetchImpl: FetchImpl = makeOpenRouterFetch();
	if (fakeTransportPath) {
		const mod = await import(path.resolve(fakeTransportPath));
		fetchImpl = mod.fakeOpenRouter ?? mod.default ?? mod;
	}

	let fakeCurrent: RouterCurrentHandler | undefined;
	if (fakeCurrentPath) {
		const mod = await import(path.resolve(fakeCurrentPath));
		fakeCurrent = mod.fakeCurrent ?? mod.default ?? mod;
	}

	const results = await runRouter({
		setsDir: setsDir || undefined,
		apiKey,
		fetch: fetchImpl,
		outPath,
		jsonPath,
		generationRecords,
		fakeCurrent,
	});

	console.log(`router: ${results.routerModel} (key read from ${keySourceLabel(configHome ?? "~/.config")})`);
	console.log(results.verdict);
}

if (import.meta.main) {
	await main();
}
