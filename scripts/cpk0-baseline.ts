#!/usr/bin/env bun
/**
 * scripts/cpk0-baseline.ts — CPK-0 baseline startup measurements (OMP-204-s03).
 *
 * Freezes the three CPK-0 baseline numbers for the coding-agent CLI:
 *   - startup_latency_ms: median cold-boot wall time (spawn -> exit) of
 *     `bun --preload <rss-preload> packages/coding-agent/src/cli.ts` under
 *     `PI_TIMING=x`, which runs the full pre-paint chain and then exits.
 *   - memory_overhead_mb: median CLI RSS minus median bare-runtime RSS, so the
 *     figure isolates the agent's own footprint from the Bun runtime floor.
 *   - system_prompt_tokens: strict native token count of the default system
 *     prompt built against an empty workspace.
 *
 * Every run gets a fresh HOME and cwd so no user config, session, or project
 * context leaks into the measurement. The CLI prints the Baseline as JSON.
 *
 * Usage:
 *   bun scripts/cpk0-baseline.ts
 */

import * as os from "node:os";
import * as path from "node:path";
import { Tokenizer } from "@oh-my-pi/pi-agent-core";
import { buildSystemPrompt } from "@oh-my-pi/pi-coding-agent/system-prompt";
import { isEnoent, TempDir } from "@oh-my-pi/pi-utils";

const REPO_ROOT = path.resolve(import.meta.dir, "..");
const CLI_PATH = path.join(REPO_ROOT, "packages/coding-agent/src/cli.ts");
const PRELOAD_PATH = path.join(import.meta.dir, "cpk0-rss-preload.ts");
const STARTUP_TIMINGS_PATTERN = /--- Startup timings/i;
const BYTES_PER_MIB = 1024 * 1024;
const STDERR_TAIL_CHARS = 2000;

/** Absolute mkdtemp prefix; `TempDir` treats a leading `@` as a fixed path, not a template. */
function tempPrefix(name: string): string {
	return path.join(os.tmpdir(), `cpk0-${name}-`);
}

/** Frozen CPK-0 baseline numbers. All values are positive on a healthy Linux host. */
export interface Baseline {
	/** Median cold-boot wall time of the CLI, in milliseconds. */
	startup_latency_ms: number;
	/** Median CLI RSS minus median bare-runtime RSS, in MiB. */
	memory_overhead_mb: number;
	/** Strict native token count of the default system prompt. */
	system_prompt_tokens: number;
	/** Number of CLI (and bare-runtime) samples behind the medians. */
	runs: number;
}

/** One CLI cold-boot sample. */
export interface StartupProbe {
	/** Wall time from spawn to process exit, in milliseconds. */
	latencyMs: number;
	/** Peak resident set size reported by the preload, in bytes. */
	rssBytes: number;
}

export interface ProbeStartupOptions {
	/** Override the RSS-capture preload path (defaults to scripts/cpk0-rss-preload.ts). */
	preloadPath?: string;
}

function median(values: number[]): number {
	const sorted = [...values].sort((left, right) => left - right);
	const midpoint = Math.floor(sorted.length / 2);
	return sorted.length % 2 === 0 ? (sorted[midpoint - 1] + sorted[midpoint]) / 2 : sorted[midpoint];
}

function tail(text: string): string {
	const trimmed = text.trim();
	return trimmed.length <= STDERR_TAIL_CHARS ? trimmed : `…${trimmed.slice(-STDERR_TAIL_CHARS)}`;
}

function buildEnv(home: string, rssOut: string, timing: boolean): NodeJS.ProcessEnv {
	const env: NodeJS.ProcessEnv = { ...process.env, HOME: home, CPK0_RSS_OUT: rssOut };
	// A stale agent dir would point the child at the developer's real config.
	delete env.PI_CODING_AGENT_DIR;
	if (timing) env.PI_TIMING = "x";
	return env;
}

async function readRssBytes(rssPath: string): Promise<number> {
	let text: string;
	try {
		text = await Bun.file(rssPath).text();
	} catch (err) {
		if (isEnoent(err)) {
			throw new Error(`RSS preload did not write ${rssPath}; CPK0_RSS_OUT was not honored`);
		}
		throw err;
	}
	const bytes = Number(text.trim());
	if (!Number.isFinite(bytes) || bytes <= 0) {
		throw new Error(`RSS preload wrote an invalid value to ${rssPath}: ${JSON.stringify(text)}`);
	}
	return bytes;
}

/**
 * Spawn the CLI once under a fresh HOME/cwd and return its cold-boot latency
 * and RSS. Rejects when the process exits non-zero or does not emit the
 * `PI_TIMING` startup-timings report — there is no fallback measurement.
 */
export async function probeStartup(cliPath: string, options: ProbeStartupOptions = {}): Promise<StartupProbe> {
	const preloadPath = options.preloadPath ?? PRELOAD_PATH;
	const home = await TempDir.create(tempPrefix("home"));
	const cwd = await TempDir.create(tempPrefix("cwd"));
	const rssOut = path.join(home.path(), "rss.txt");
	try {
		const start = performance.now();
		const proc = Bun.spawn(["bun", "--preload", preloadPath, cliPath], {
			cwd: cwd.path(),
			env: buildEnv(home.path(), rssOut, true),
			stdin: "ignore",
			stdout: "pipe",
			stderr: "pipe",
		});
		const [exitCode, stdout, stderr] = await Promise.all([
			proc.exited,
			new Response(proc.stdout).text(),
			new Response(proc.stderr).text(),
		]);
		const latencyMs = performance.now() - start;
		if (exitCode !== 0 || !STARTUP_TIMINGS_PATTERN.test(stderr)) {
			const timings = STARTUP_TIMINGS_PATTERN.test(stderr) ? "present" : "missing";
			throw new Error(`CLI startup probe failed (exit ${exitCode}, timings ${timings}):\n${tail(stderr || stdout)}`);
		}
		return { latencyMs, rssBytes: await readRssBytes(rssOut) };
	} finally {
		await Promise.all([home.remove(), cwd.remove()]);
	}
}

/** Sample the bare Bun runtime floor with the same RSS preload. */
async function probeBareRuntime(preloadPath: string): Promise<number> {
	const home = await TempDir.create(tempPrefix("bare"));
	const rssOut = path.join(home.path(), "rss.txt");
	try {
		const proc = Bun.spawn(["bun", "--preload", preloadPath, "-e", "0"], {
			cwd: home.path(),
			env: buildEnv(home.path(), rssOut, false),
			stdin: "ignore",
			stdout: "pipe",
			stderr: "pipe",
		});
		const [exitCode, stdout, stderr] = await Promise.all([
			proc.exited,
			new Response(proc.stdout).text(),
			new Response(proc.stderr).text(),
		]);
		if (exitCode !== 0) {
			throw new Error(`bare runtime probe failed (exit ${exitCode}):\n${tail(stderr || stdout)}`);
		}
		return await readRssBytes(rssOut);
	} finally {
		await home.remove();
	}
}

async function measureSystemPromptTokens(): Promise<number> {
	const cwd = await TempDir.create(tempPrefix("prompt"));
	try {
		const { systemPrompt } = await buildSystemPrompt({ cwd: cwd.path(), contextFiles: [], skills: [], rules: [] });
		return new Tokenizer(null).countTokens(systemPrompt, "strict");
	} finally {
		await cwd.remove();
	}
}

/**
 * Measure the CPK-0 baseline over `runs` cold-boot samples. Each sample uses a
 * fresh HOME and cwd; the bare-runtime floor is sampled the same number of
 * times so the memory figure is a median-of-medians difference.
 */
export async function measureBaseline(runs = 5): Promise<Baseline> {
	if (!Number.isInteger(runs) || runs < 1) {
		throw new Error(`runs must be a positive integer, got ${runs}`);
	}
	const cliLatencies: number[] = [];
	const cliRss: number[] = [];
	for (let i = 0; i < runs; i++) {
		const probe = await probeStartup(CLI_PATH);
		cliLatencies.push(probe.latencyMs);
		cliRss.push(probe.rssBytes);
	}
	const bareRss: number[] = [];
	for (let i = 0; i < runs; i++) {
		bareRss.push(await probeBareRuntime(PRELOAD_PATH));
	}
	const overheadBytes = median(cliRss) - median(bareRss);
	return {
		startup_latency_ms: Math.round(median(cliLatencies)),
		memory_overhead_mb: Math.round((overheadBytes / BYTES_PER_MIB) * 10) / 10,
		system_prompt_tokens: await measureSystemPromptTokens(),
		runs,
	};
}

if (import.meta.main) {
	const baseline = await measureBaseline();
	console.log(JSON.stringify(baseline, null, 2));
}
