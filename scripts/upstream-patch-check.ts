#!/usr/bin/env bun
// upstream-patch-check.ts — pre-intake fork-patch conflict check (OMP-401).
//
// Before an upstream candidate is incorporated, this predicts which fork patches
// the candidate would break: it runs `git merge-tree --write-tree --merge-base
// <baseline target> <head> <target>` and reports every conflicted path together
// with its fork-behavior inventory row. A conflicted `shared` path is exactly a
// fork change to upstream-owned code that upstream also touched, so the intake
// session must re-fit it before merging.
//
//   bun scripts/upstream-patch-check.ts --target <40-hex> [--head <rev>]
//     [--baseline docs/upstream-baseline.json] [--inventory docs/upstream-fork-inventory.tsv]
//
// Exit codes: 0 clean, 1 conflicts, 2 usage/unresolvable target (with a fetch hint).

import { type InventoryRow, type InventoryScope, parseInventoryTsv } from "./upstream-inventory.ts";
import { parseMergeTreeConflicts } from "./verify-upstream-handoff.ts";

export type ConflictScope = InventoryScope | "uninventoried";

export interface ClassifiedConflict {
	path: string;
	scope: ConflictScope;
	behavior: string;
}

const UNINVENTORIED_BEHAVIOR = "no inventory row";

/** Map each conflicted path to its inventory row's scope/behavior, or `uninventoried`. */
export function classifyConflicts(conflicts: Iterable<string>, rows: InventoryRow[]): ClassifiedConflict[] {
	const rowsByPath = new Map(rows.map(r => [r.path, r]));
	return [...new Set(conflicts)].sort().map(path => {
		const row = rowsByPath.get(path);
		return row
			? { path, scope: row.scope, behavior: row.behavior }
			: { path, scope: "uninventoried", behavior: UNINVENTORIED_BEHAVIOR };
	});
}

interface Args {
	target: string;
	head: string;
	baseline: string;
	inventory: string;
}

export function parseArgs(argv: string[]): Args {
	const flags = new Map<string, string>();
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--target" || arg === "--head" || arg === "--baseline" || arg === "--inventory") {
			const value = argv[++i];
			if (value === undefined) throw new Error(`missing value for ${arg}`);
			flags.set(arg.slice(2), value);
		} else {
			throw new Error(`unexpected argument ${arg}`);
		}
	}
	const target = flags.get("target");
	if (!target) throw new Error("missing --target");
	return {
		target,
		head: flags.get("head") ?? "HEAD",
		baseline: flags.get("baseline") ?? "docs/upstream-baseline.json",
		inventory: flags.get("inventory") ?? "docs/upstream-fork-inventory.tsv",
	};
}

async function git(args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
	const proc = Bun.spawn(["git", ...args], { stdout: "pipe", stderr: "pipe" });
	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	return { exitCode, stdout, stderr };
}

async function isLocalCommit(rev: string): Promise<boolean> {
	const { exitCode } = await git(["rev-parse", "--verify", "--quiet", `${rev}^{commit}`]);
	return exitCode === 0;
}

function failUsage(message: string, upstreamRepo: string, target: string): never {
	console.error(`ERROR: ${message}`);
	console.error(`hint: git fetch ${upstreamRepo} ${target}`);
	process.exit(2);
}

async function main(): Promise<void> {
	let args: Args;
	try {
		args = parseArgs(process.argv.slice(2));
	} catch (err) {
		console.error(`usage error: ${err instanceof Error ? err.message : err}`);
		process.exit(2);
	}

	const baselineFile = Bun.file(args.baseline);
	if (!(await baselineFile.exists())) {
		console.error(`ERROR: baseline record missing: ${args.baseline}`);
		process.exit(2);
	}
	const baseline = JSON.parse(await baselineFile.text()) as { target?: unknown; upstream_repo?: unknown };
	const upstreamRepo = typeof baseline.upstream_repo === "string" ? baseline.upstream_repo : "origin";
	if (typeof baseline.target !== "string" || !/^[0-9a-f]{40}$/.test(baseline.target)) {
		console.error(`ERROR: ${args.baseline} has no full 40-hex target commit`);
		process.exit(2);
	}

	if (!/^[0-9a-f]{40}$/.test(args.target)) {
		failUsage(`--target must be a full 40-hex commit: '${args.target}'`, upstreamRepo, args.target);
	}
	if (!(await isLocalCommit(args.target))) {
		failUsage(`--target ${args.target} is not a local commit`, upstreamRepo, args.target);
	}
	if (!(await isLocalCommit(baseline.target))) {
		failUsage(`baseline target ${baseline.target} is not a local commit`, upstreamRepo, baseline.target);
	}

	const inventoryFile = Bun.file(args.inventory);
	if (!(await inventoryFile.exists())) {
		console.error(`ERROR: inventory missing: ${args.inventory}`);
		process.exit(2);
	}
	const rows = parseInventoryTsv(await inventoryFile.text());
	const sharedRows = rows.filter(r => r.scope === "shared").length;

	const mergeTree = await git([
		"merge-tree",
		"--write-tree",
		"--no-messages",
		// Conflict paths must match inventory paths.
		"-X",
		"no-renames",
		"--merge-base",
		baseline.target,
		args.head,
		args.target,
	]);
	if (mergeTree.exitCode !== 0 && mergeTree.exitCode !== 1) {
		failUsage(`git merge-tree failed: ${mergeTree.stderr.trim()}`, upstreamRepo, args.target);
	}

	const conflicts = classifyConflicts(parseMergeTreeConflicts(mergeTree.stdout), rows);
	const target12 = args.target.slice(0, 12);
	if (conflicts.length) {
		for (const c of conflicts) console.log(`BROKEN ${c.path} [${c.scope}] ${c.behavior}`);
		console.log(`FAIL: ${conflicts.length} fork patch(es) broken by ${target12} (${sharedRows} shared rows)`);
		process.exit(1);
	}
	console.log(`PASS: ${sharedRows} fork patches merge cleanly with ${target12}`);
	process.exit(0);
}

if (import.meta.main) {
	await main();
}
