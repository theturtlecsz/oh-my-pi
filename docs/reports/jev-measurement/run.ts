#!/usr/bin/env bun
/**
 * run.ts
 *
 * Runs the Jev measurement harness over the dataset, writing results.json and
 * jev-measurement-report.md.
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import { type FakeSmolHandler, type MeasurementResults, runMeasurementHarness } from "./harness";

function formatPercent(value?: number): string {
	if (value === undefined || Number.isNaN(value)) return "0.0%";
	return `${(value * 100).toFixed(1)}%`;
}

function formatNumber(value?: number, decimals = 2): string {
	if (value === undefined || Number.isNaN(value)) return "0.00";
	return value.toFixed(decimals);
}

function formatCost(value?: number): string {
	if (value === undefined || Number.isNaN(value)) return "$0.0000";
	return `$${value.toFixed(4)}`;
}

export function renderReport(results: MeasurementResults, template: string): string {
	const at = results.features.auto_thinking;
	const us = results.features.unexpected_stop;
	const ro = results.features.robomp;

	let rendered = template;

	// Auto-thinking replacements
	rendered = rendered.replace("{{auto_thinking_current_accuracy}}", formatPercent(at.current.accuracy));
	rendered = rendered.replace("{{auto_thinking_jev_accuracy}}", formatPercent(at.jev.accuracy));
	rendered = rendered.replace("{{auto_thinking_current_p50}}", formatNumber(at.current.p50LatencyMs, 1));
	rendered = rendered.replace("{{auto_thinking_jev_p50}}", formatNumber(at.jev.p50LatencyMs, 1));
	rendered = rendered.replace("{{auto_thinking_current_p95}}", formatNumber(at.current.p95LatencyMs, 1));
	rendered = rendered.replace("{{auto_thinking_jev_p95}}", formatNumber(at.jev.p95LatencyMs, 1));
	rendered = rendered.replace("{{auto_thinking_current_cost}}", formatCost(at.current.costPer1000));
	rendered = rendered.replace("{{auto_thinking_jev_cost}}", formatCost(at.jev.costPer1000));
	rendered = rendered.replace("{{auto_thinking_current_unparseable}}", formatPercent(at.current.unparseableRate));
	rendered = rendered.replace("{{auto_thinking_jev_unparseable}}", formatPercent(at.jev.unparseableRate));
	rendered = rendered.replace("{{auto_thinking_current_off_list}}", formatPercent(at.current.offListRate));
	rendered = rendered.replace("{{auto_thinking_jev_off_list}}", formatPercent(at.jev.offListRate));

	// Unexpected-stop replacements
	rendered = rendered.replace("{{unexpected_stop_current_accuracy}}", formatPercent(us.current.accuracy));
	rendered = rendered.replace("{{unexpected_stop_jev_accuracy}}", formatPercent(us.jev.accuracy));
	rendered = rendered.replace("{{unexpected_stop_current_precision}}", formatPercent(us.current.precision));
	rendered = rendered.replace("{{unexpected_stop_jev_precision}}", formatPercent(us.jev.precision));
	rendered = rendered.replace("{{unexpected_stop_current_recall}}", formatPercent(us.current.recall));
	rendered = rendered.replace("{{unexpected_stop_jev_recall}}", formatPercent(us.jev.recall));
	rendered = rendered.replace("{{unexpected_stop_current_p50}}", formatNumber(us.current.p50LatencyMs, 1));
	rendered = rendered.replace("{{unexpected_stop_jev_p50}}", formatNumber(us.jev.p50LatencyMs, 1));
	rendered = rendered.replace("{{unexpected_stop_current_p95}}", formatNumber(us.current.p95LatencyMs, 1));
	rendered = rendered.replace("{{unexpected_stop_jev_p95}}", formatNumber(us.jev.p95LatencyMs, 1));
	rendered = rendered.replace("{{unexpected_stop_current_cost}}", formatCost(us.current.costPer1000));
	rendered = rendered.replace("{{unexpected_stop_jev_cost}}", formatCost(us.jev.costPer1000));
	rendered = rendered.replace("{{unexpected_stop_current_unparseable}}", formatPercent(us.current.unparseableRate));
	rendered = rendered.replace("{{unexpected_stop_jev_unparseable}}", formatPercent(us.jev.unparseableRate));
	rendered = rendered.replace("{{unexpected_stop_current_off_list}}", formatPercent(us.current.offListRate));
	rendered = rendered.replace("{{unexpected_stop_jev_off_list}}", formatPercent(us.jev.offListRate));

	// Robomp replacements
	rendered = rendered.replace(
		"{{robomp_jev_confident_accuracy}}",
		formatPercent(ro.jev.confidentBucketAccuracy),
	);
	rendered = rendered.replace("{{robomp_jev_skip_share}}", formatPercent(ro.jev.skipSessionShare));
	rendered = rendered.replace("{{robomp_current_accuracy}}", formatPercent(ro.current.accuracy));
	rendered = rendered.replace("{{robomp_jev_accuracy}}", formatPercent(ro.jev.accuracy));
	rendered = rendered.replace("{{robomp_current_p50}}", formatNumber(ro.current.p50LatencyMs, 1));
	rendered = rendered.replace("{{robomp_jev_p50}}", formatNumber(ro.jev.p50LatencyMs, 1));
	rendered = rendered.replace("{{robomp_current_p95}}", formatNumber(ro.current.p95LatencyMs, 1));
	rendered = rendered.replace("{{robomp_jev_p95}}", formatNumber(ro.jev.p95LatencyMs, 1));
	rendered = rendered.replace("{{robomp_current_cost}}", formatCost(ro.current.costPer1000));
	rendered = rendered.replace("{{robomp_jev_cost}}", formatCost(ro.jev.costPer1000));
	rendered = rendered.replace("{{robomp_current_unparseable}}", formatPercent(ro.current.unparseableRate));
	rendered = rendered.replace("{{robomp_jev_unparseable}}", formatPercent(ro.jev.unparseableRate));
	rendered = rendered.replace("{{robomp_current_off_list}}", formatPercent(ro.current.offListRate));
	rendered = rendered.replace("{{robomp_jev_off_list}}", formatPercent(ro.jev.offListRate));

	// Verdict replacement
	rendered = rendered.replace("{{verdict}}", results.verdict || "");

	return rendered;
}

export async function run(options: {
	setsDir: string;
	outDir: string;
	robompSessionCostUsd?: number;
	robompSessionP50Ms?: number;
	robompSessionP95Ms?: number;
	jevBaseUrl?: string;
	fakeSmol?: FakeSmolHandler;
	robompRunner?: (issuesPath: string, jevBaseUrl?: string) => Promise<any>;
}): Promise<MeasurementResults> {
	const results = await runMeasurementHarness(options);

	await fs.mkdir(options.outDir, { recursive: true });

	const resultsPath = path.join(options.outDir, "results.json");
	await Bun.write(resultsPath, JSON.stringify(results, null, 2) + "\n");

	const templatePath = path.join(import.meta.dir, "report-template.md");
	let template = "";
	try {
		template = await Bun.file(templatePath).text();
	} catch {
		template = "# Jev Decision-Classifier Measurement Report\n\n## Verdict\n\n{{verdict}}\n";
	}

	const renderedReport = renderReport(results, template);
	const reportPath = path.join(options.outDir, "jev-measurement-report.md");
	await Bun.write(reportPath, renderedReport);

	return results;
}

async function main() {
	const args = process.argv.slice(2);
	let setsDir = "";
	let outDir = "docs/reports";
	let robompSessionCostUsd: number | undefined;
	let robompSessionP50Ms: number | undefined;
	let robompSessionP95Ms: number | undefined;
	let jevBaseUrl: string | undefined;
	let fakeSmolPath: string | undefined;

	for (let i = 0; i < args.length; i++) {
		const arg = args[i];
		if (arg === "--sets" && i + 1 < args.length) {
			setsDir = args[++i];
		} else if (arg.startsWith("--sets=")) {
			setsDir = arg.slice("--sets=".length);
		} else if (arg === "--out" && i + 1 < args.length) {
			outDir = args[++i];
		} else if (arg.startsWith("--out=")) {
			outDir = arg.slice("--out=".length);
		} else if (arg === "--robomp-session-cost-usd" && i + 1 < args.length) {
			robompSessionCostUsd = parseFloat(args[++i]);
		} else if (arg.startsWith("--robomp-session-cost-usd=")) {
			robompSessionCostUsd = parseFloat(arg.slice("--robomp-session-cost-usd=".length));
		} else if (arg === "--robomp-session-p50-ms" && i + 1 < args.length) {
			robompSessionP50Ms = parseFloat(args[++i]);
		} else if (arg.startsWith("--robomp-session-p50-ms=")) {
			robompSessionP50Ms = parseFloat(arg.slice("--robomp-session-p50-ms=".length));
		} else if (arg === "--robomp-session-p95-ms" && i + 1 < args.length) {
			robompSessionP95Ms = parseFloat(args[++i]);
		} else if (arg.startsWith("--robomp-session-p95-ms=")) {
			robompSessionP95Ms = parseFloat(arg.slice("--robomp-session-p95-ms=".length));
		} else if (arg === "--jev-base-url" && i + 1 < args.length) {
			jevBaseUrl = args[++i];
		} else if (arg.startsWith("--jev-base-url=")) {
			jevBaseUrl = arg.slice("--jev-base-url=".length);
		} else if (arg === "--fake-smol" && i + 1 < args.length) {
			fakeSmolPath = args[++i];
		} else if (arg.startsWith("--fake-smol=")) {
			fakeSmolPath = arg.slice("--fake-smol=".length);
		}
	}

	if (!setsDir) {
		console.error("Usage: run.ts --sets <dir> --out <dir> [options]");
		process.exit(1);
	}

	let fakeSmol: FakeSmolHandler | undefined;
	if (fakeSmolPath) {
		const mod = await import(path.resolve(fakeSmolPath));
		fakeSmol = mod.fakeSmol ?? mod.default ?? mod;
	}

	const results = await run({
		setsDir,
		outDir,
		robompSessionCostUsd,
		robompSessionP50Ms,
		robompSessionP95Ms,
		jevBaseUrl,
		fakeSmol,
	});

	console.log(results.verdict);
}

if (import.meta.main) {
	await main();
}
