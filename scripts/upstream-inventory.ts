#!/usr/bin/env bun
// upstream-inventory.ts — standing fork-behavior inventory check (OMP-229).
//
// The inventory (docs/upstream-fork-inventory.tsv) records every path whose
// content diverges from the accepted upstream baseline commit, with a
// human-owned behavior description and classification. The check recomputes
// the divergence set from git and fails, itemized, when the inventory is
// missing a diverging path, records a stale blob/state/scope, or keeps a row
// for a path that no longer diverges — so ordinary pull requests cannot
// silently add, change, or remove fork behavior.
//
//   bun scripts/upstream-inventory.ts [--baseline docs/upstream-baseline.json]
//     [--inventory docs/upstream-fork-inventory.tsv] [--head <rev>] [--write] [--patches]
//
// --write regenerates the machine columns (scope/state/head_blob), preserves
// existing behavior/classification text, seeds new rows from commit subjects,
// and drops rows whose paths no longer diverge.
//
// --patches prints one tab-separated line per shared row (path, state, +added,
// -removed, classification, behavior) and a total summary line.

import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseRawDiff, unquoteGitPath } from "./verify-upstream-handoff.ts";

export const INVENTORY_HEADER = ["path", "scope", "state", "head_blob", "behavior", "classification"] as const;

export type InventoryScope = "shared" | "fork-only";
export type InventoryState = "modified" | "added" | "deleted";

export interface InventoryRow {
	path: string;
	scope: InventoryScope;
	state: InventoryState;
	headBlob: string;
	behavior: string;
	classification: string;
}

export interface DivergingPath {
	path: string;
	scope: InventoryScope;
	state: InventoryState;
	headBlob: string;
}

const ZERO_BLOB = "-";

/** Compute the diverging path set from `git diff-tree -r --no-renames <baseline> <head>` output. */
export function computeDivergence(diffTreeText: string): DivergingPath[] {
	const out: DivergingPath[] = [];
	for (const change of parseRawDiff(diffTreeText)) {
		if (change.status === "A") {
			out.push({ path: change.path, scope: "fork-only", state: "added", headBlob: change.newSha.slice(0, 12) });
		} else if (change.status === "D") {
			out.push({ path: change.path, scope: "shared", state: "deleted", headBlob: ZERO_BLOB });
		} else {
			// M and T (typechange) both mean the baseline path exists with different content.
			out.push({ path: change.path, scope: "shared", state: "modified", headBlob: change.newSha.slice(0, 12) });
		}
	}
	out.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
	return out;
}

export function parseInventoryTsv(text: string): InventoryRow[] {
	const lines = text.replace(/^\uFEFF/, "").split("\n");
	while (lines.length && lines[lines.length - 1] === "") lines.pop();
	if (!lines.length) throw new Error("inventory: empty file");
	const got = lines[0].split("\t");
	if (got.length !== INVENTORY_HEADER.length || INVENTORY_HEADER.some((h, i) => got[i] !== h)) {
		throw new Error(`inventory: bad header — expected ${INVENTORY_HEADER.join("\\t")}`);
	}
	return lines.slice(1).map((line, i) => {
		const cells = line.split("\t");
		if (cells.length !== INVENTORY_HEADER.length) {
			throw new Error(`inventory: row ${i + 2} has ${cells.length} fields, expected ${INVENTORY_HEADER.length}`);
		}
		return {
			path: cells[0],
			scope: cells[1] as InventoryScope,
			state: cells[2] as InventoryState,
			headBlob: cells[3],
			behavior: cells[4],
			classification: cells[5],
		};
	});
}

export function formatInventoryTsv(rows: InventoryRow[]): string {
	const lines = [INVENTORY_HEADER.join("\t")];
	for (const r of rows) lines.push([r.path, r.scope, r.state, r.headBlob, r.behavior, r.classification].join("\t"));
	return `${lines.join("\n")}\n`;
}

/** Check the inventory against the recomputed divergence set; returns itemized errors. */
export function checkInventory(computed: DivergingPath[], rows: InventoryRow[]): string[] {
	const errors: string[] = [];
	const rowsByPath = new Map<string, InventoryRow>();
	for (const row of rows) {
		if (rowsByPath.has(row.path)) errors.push(`inventory: duplicate row for ${row.path}`);
		rowsByPath.set(row.path, row);
		if (!row.behavior.trim()) errors.push(`inventory: ${row.path} has an empty behavior description`);
		if (row.scope === "shared" && row.behavior.startsWith("fork change (describe)")) {
			errors.push(
				`inventory: ${row.path} changes upstream-owned code but its behavior is the seed placeholder — name the fork patch`,
			);
		}
		if (!["retained", "re-fitted", "dropped"].includes(row.classification)) {
			errors.push(`inventory: ${row.path} has invalid classification '${row.classification}'`);
		}
		if (row.classification === "dropped" && !row.behavior.includes("owner-ruling:")) {
			errors.push(`inventory: ${row.path} 'dropped' requires an explicit owner-ruling: reference in behavior`);
		}
	}
	const computedByPath = new Map(computed.map(c => [c.path, c]));
	for (const c of computed) {
		const row = rowsByPath.get(c.path);
		if (!row) {
			errors.push(
				`inventory: ${c.path} diverges from the upstream baseline (${c.state}) but has no inventory row — run \`bun scripts/upstream-inventory.ts --write\` and describe the fork behavior`,
			);
			continue;
		}
		if (row.scope !== c.scope || row.state !== c.state || row.headBlob !== c.headBlob) {
			errors.push(
				`inventory: ${c.path} row is stale (recorded ${row.scope}/${row.state}/${row.headBlob}, computed ${c.scope}/${c.state}/${c.headBlob}) — run \`bun scripts/upstream-inventory.ts --write\` and update the behavior description`,
			);
		}
	}
	for (const row of rows) {
		if (!computedByPath.has(row.path)) {
			errors.push(
				`inventory: ${row.path} no longer diverges from the upstream baseline — remove its row (\`bun scripts/upstream-inventory.ts --write\`)`,
			);
		}
	}
	return errors;
}

const sanitize = (text: string): string => text.replaceAll("\t", " ").replaceAll("\n", " ").trim();

/** Merge the recomputed divergence set with existing rows, preserving human columns. */
export function mergeInventory(
	computed: DivergingPath[],
	existing: InventoryRow[],
	seedBehavior: (path: string) => string,
): InventoryRow[] {
	const existingByPath = new Map(existing.map(r => [r.path, r]));
	return computed.map(c => {
		const prior = existingByPath.get(c.path);
		return {
			path: c.path,
			scope: c.scope,
			state: c.state,
			headBlob: c.headBlob,
			behavior: prior?.behavior.trim() ? prior.behavior : sanitize(seedBehavior(c.path)) || "fork change (describe)",
			classification: prior?.classification.trim() ? prior.classification : "retained",
		};
	});
}

/** Build path -> recent distinct commit subjects from one `git log --name-only` pass. */
export function buildSubjectIndex(logText: string, maxSubjects = 5): Map<string, string[]> {
	const index = new Map<string, string[]>();
	let subject = "";
	for (const line of logText.split("\n")) {
		if (line.startsWith("\u0000")) {
			subject = line.slice(1);
			continue;
		}
		if (!line.trim() || !subject) continue;
		const path = unquoteGitPath(line);
		const subjects = index.get(path) ?? [];
		if (subjects.length < maxSubjects && !subjects.includes(subject)) subjects.push(subject);
		index.set(path, subjects);
	}
	return index;
}

export interface NumstatEntry {
	added: number;
	removed: number;
}

/** Parse `git diff --numstat --no-renames` output into a map of path -> { added, removed }. */
export function parseNumstat(text: string): Map<string, NumstatEntry> {
	const map = new Map<string, NumstatEntry>();
	for (const line of text.split(/\r?\n/)) {
		if (!line.trim()) continue;
		const tab1 = line.indexOf("\t");
		if (tab1 < 0) continue;
		const tab2 = line.indexOf("\t", tab1 + 1);
		if (tab2 < 0) continue;
		const addedStr = line.slice(0, tab1);
		const removedStr = line.slice(tab1 + 1, tab2);
		const rawPath = line.slice(tab2 + 1);
		const added = addedStr === "-" ? 0 : parseInt(addedStr, 10);
		const removed = removedStr === "-" ? 0 : parseInt(removedStr, 10);
		const path = unquoteGitPath(rawPath);
		map.set(path, {
			added: Number.isNaN(added) ? 0 : added,
			removed: Number.isNaN(removed) ? 0 : removed,
		});
	}
	return map;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

async function git(args: string[], env?: Record<string, string>): Promise<string> {
	const proc = Bun.spawn(["git", ...args], {
		stdout: "pipe",
		stderr: "pipe",
		env: env ? { ...process.env, ...env } : process.env,
	});
	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	if (exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${stderr.trim()}`);
	return stdout;
}

/**
 * Snapshot the working tree (tracked changes plus untracked non-ignored files)
 * as a tree object via a throwaway index, so uncommitted fork changes are
 * checked exactly as a commit of them would be. On a clean checkout (CI) the
 * snapshot equals HEAD's tree.
 */
async function worktreeTree(): Promise<string> {
	const dir = mkdtempSync(join(tmpdir(), "omp-upstream-inventory-"));
	const env = { GIT_INDEX_FILE: join(dir, "index") };
	try {
		await git(["read-tree", "HEAD"], env);
		await git(["add", "-A"], env);
		return (await git(["write-tree"], env)).trim();
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
}

async function main(): Promise<void> {
	let baselinePath = "docs/upstream-baseline.json";
	let inventoryPath = "docs/upstream-fork-inventory.tsv";
	let head = "HEAD";
	let write = false;
	let patches = false;
	const argv = process.argv.slice(2);
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--write") write = true;
		else if (arg === "--patches") patches = true;
		else if (arg === "--baseline") baselinePath = argv[++i] ?? "";
		else if (arg === "--inventory") inventoryPath = argv[++i] ?? "";
		else if (arg === "--head") head = argv[++i] ?? "";
		else {
			console.error(`usage error: unexpected argument ${arg}`);
			process.exit(2);
		}
	}

	if (patches && write) {
		console.error("usage error: --patches and --write cannot be combined");
		process.exit(2);
	}

	const baselineFile = Bun.file(baselinePath);
	if (!(await baselineFile.exists())) {
		console.error(`ERROR: baseline record missing: ${baselinePath}`);
		process.exit(1);
	}
	const baseline = JSON.parse(await baselineFile.text()) as { target?: unknown };
	if (typeof baseline.target !== "string" || !/^[0-9a-f]{40}$/.test(baseline.target)) {
		console.error(`ERROR: ${baselinePath} has no full 40-hex target commit`);
		process.exit(1);
	}

	const headTree = head === "HEAD" ? await worktreeTree() : head;

	if (patches) {
		const inventoryFile = Bun.file(inventoryPath);
		if (!(await inventoryFile.exists())) {
			console.error(
				`ERROR: inventory missing: ${inventoryPath} — run \`bun scripts/upstream-inventory.ts --write\``,
			);
			process.exit(1);
		}
		let rows: InventoryRow[];
		try {
			rows = parseInventoryTsv(await inventoryFile.text());
		} catch (err) {
			console.error(`ERROR: ${err instanceof Error ? err.message : err}`);
			process.exit(1);
		}
		const numstatText = await git(["diff", "--numstat", "--no-renames", baseline.target, headTree]);
		const numstat = parseNumstat(numstatText);
		const sharedRows = rows.filter(r => r.scope === "shared");
		let totalAdded = 0;
		let totalRemoved = 0;
		for (const r of sharedRows) {
			const stat = numstat.get(r.path) ?? { added: 0, removed: 0 };
			totalAdded += stat.added;
			totalRemoved += stat.removed;
			console.log([r.path, r.state, `+${stat.added}`, `-${stat.removed}`, r.classification, r.behavior].join("\t"));
		}
		console.log(`TOTAL ${sharedRows.length} patches +${totalAdded} -${totalRemoved}`);
		return;
	}

	const diffTreeText = await git(["diff-tree", "-r", "--no-renames", "--abbrev=40", baseline.target, headTree]);
	// The inventory file itself is guardrail bookkeeping, not fork behavior: a
	// self-row recording its own blob hash has no fixpoint (the hash changes the
	// content it hashes), so the inventory is excluded from its own divergence set.
	const computed = computeDivergence(diffTreeText).filter(c => c.path !== inventoryPath);

	if (write) {
		const inventoryFile = Bun.file(inventoryPath);
		const existing = (await inventoryFile.exists()) ? parseInventoryTsv(await inventoryFile.text()) : [];
		const logText = await git([
			"log",
			"--no-merges",
			"--format=%x00%s",
			"--name-only",
			`${baseline.target}..${head}`,
		]);
		const subjects = buildSubjectIndex(logText);
		const rows = mergeInventory(computed, existing, path => (subjects.get(path) ?? []).join("; "));
		await Bun.write(inventoryPath, formatInventoryTsv(rows));
		console.log(`wrote ${rows.length} inventory rows to ${inventoryPath}`);
		return;
	}

	const inventoryFile = Bun.file(inventoryPath);
	if (!(await inventoryFile.exists())) {
		console.error(`ERROR: inventory missing: ${inventoryPath} — run \`bun scripts/upstream-inventory.ts --write\``);
		process.exit(1);
	}
	let errors: string[];
	try {
		errors = checkInventory(computed, parseInventoryTsv(await inventoryFile.text()));
	} catch (err) {
		console.error(`ERROR: ${err instanceof Error ? err.message : err}`);
		process.exit(1);
	}
	if (errors.length) {
		for (const e of errors) console.error(`ERROR: ${e}`);
		console.error(`FAIL: ${errors.length} inventory error(s) — divergingPaths=${computed.length}`);
		process.exit(1);
	}
	console.log(`PASS: fork-behavior inventory current — divergingPaths=${computed.length}`);
}

if (import.meta.main) {
	await main();
}
