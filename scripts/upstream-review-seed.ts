#!/usr/bin/env bun
// upstream-review-seed.ts — create or refresh a review record and its fork matrix (OMP-401).
//
// CLI:
//   bun scripts/upstream-review-seed.ts --target <sha> --version <X.Y.Z> [--fork <rev>]
//     [--baseline <json>] [--inventory <tsv>] [--dir docs]
//
// Generates or updates <dir>/upstream-review-<target12>.json and its companion files:
//   <stem>-sources.tsv, <stem>-matrix.tsv, <stem>-changelog.tsv, <stem>-handoff.md

import * as path from "node:path";
import { type InventoryRow, parseInventoryTsv } from "./upstream-inventory.ts";
import {
	compareVersions,
	computeSourceRecords,
	computeUpstreamChanges,
	formatChangelogTsv,
	formatMatrixTsv,
	formatSourcesTsv,
	formatUpstreamEntry,
	type MatrixRow,
	parseMatrixTsv,
	parseMergeTreeConflicts,
	type SourceRecord,
	type UpstreamChange,
	unquoteGitPath,
} from "./verify-upstream-handoff.ts";

export type GitRunner = (args: string[], okExitCodes?: number[]) => Promise<string>;

async function defaultGit(args: string[], okExitCodes: number[] = [0], cwd?: string): Promise<string> {
	const proc = Bun.spawn(["git", ...args], { stdout: "pipe", stderr: "pipe", cwd });
	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	if (!okExitCodes.includes(exitCode)) throw new Error(`git ${args.join(" ")} failed: ${stderr.trim()}`);
	return stdout;
}

export interface ReviewSeedArgs {
	target: string;
	version: string;
	fork: string;
	baseline?: string;
	inventory?: string;
	dir: string;
}

export function parseArgs(argv: string[]): ReviewSeedArgs {
	let target: string | undefined;
	let version: string | undefined;
	let fork = "HEAD";
	let baseline: string | undefined;
	let inventory: string | undefined;
	let dir = "docs";

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--target") {
			target = argv[++i];
			if (!target) throw new Error("missing value for --target");
		} else if (arg === "--version") {
			version = argv[++i];
			if (!version) throw new Error("missing value for --version");
		} else if (arg === "--fork") {
			fork = argv[++i] ?? "";
			if (!fork) throw new Error("missing value for --fork");
		} else if (arg === "--baseline") {
			baseline = argv[++i];
			if (!baseline) throw new Error("missing value for --baseline");
		} else if (arg === "--inventory") {
			inventory = argv[++i];
			if (!inventory) throw new Error("missing value for --inventory");
		} else if (arg === "--dir") {
			dir = argv[++i] ?? "";
			if (!dir) throw new Error("missing value for --dir");
		} else {
			throw new Error(`unexpected argument ${arg}`);
		}
	}

	if (!target) throw new Error("missing required --target");
	if (!version) throw new Error("missing required --version");
	if (!/^\d+\.\d+\.\d+$/.test(version)) {
		throw new Error(`--version must be in X.Y.Z format (got '${version}')`);
	}

	return { target, version, fork, baseline, inventory, dir };
}

/**
 * Find the lowest `## [x.y.z]` above baselineVersion across target packages/*\/CHANGELOG.md.
 * Falls back to versionMax if none are found.
 */
export async function findVersionMin(
	targetSha: string,
	baselineVersion: string,
	versionMax: string,
	git: GitRunner,
): Promise<string> {
	const lsText = await git(["ls-tree", "-r", "--name-only", targetSha]);
	const changelogPaths = lsText
		.split("\n")
		.map(p => p.trim())
		.filter(p => /^packages\/[^/]+\/CHANGELOG\.md$/.test(p));

	const versions = new Set<string>();
	for (const clPath of changelogPaths) {
		const content = await git(["show", `${targetSha}:${clPath}`]);
		for (const line of content.split("\n")) {
			const m = line.match(/^## \[(\d+\.\d+\.\d+)\]/);
			if (m) {
				const v = m[1];
				if (compareVersions(v, baselineVersion) > 0) {
					versions.add(v);
				}
			}
		}
	}

	if (versions.size === 0) return versionMax;
	const sorted = [...versions].sort(compareVersions);
	return sorted[0];
}

export function buildMatrixRows(
	forkPaths: Set<string>,
	targetPaths: Set<string>,
	conflictPaths: Set<string>,
	computedSources: SourceRecord[],
	inventoryByPath: Map<string, InventoryRow>,
	existingMatrixByPath: Map<string, MatrixRow>,
	fork12: string,
): MatrixRow[] {
	const sourcesByPath = new Map<string, string[]>();
	for (const s of computedSources) {
		const list = sourcesByPath.get(s.path);
		if (list) list.push(s.id);
		else sourcesByPath.set(s.path, [s.id]);
	}

	const sortedPaths = [...forkPaths].sort();
	const rows: MatrixRow[] = [];

	for (const p of sortedPaths) {
		const isShared = targetPaths.has(p);
		const scope = isShared ? "shared" : "fork-only";
		const sourceIds = sourcesByPath.get(p) ?? [];
		const existing = existingMatrixByPath.get(p);

		if (existing) {
			rows.push({
				surfaceId: p,
				path: p,
				scope,
				sourceIds,
				forkBehavior: existing.forkBehavior,
				upstreamChange: existing.upstreamChange,
				classification: existing.classification,
				resolution: existing.resolution,
				proof: existing.proof,
			});
			continue;
		}

		const inv = inventoryByPath.get(p);
		const forkBehavior = inv?.behavior ?? "fork change (describe)";
		const classification = inv?.classification ?? "retained";

		if (!isShared) {
			rows.push({
				surfaceId: p,
				path: p,
				scope: "fork-only",
				sourceIds,
				forkBehavior,
				upstreamChange: "none (fork-only path)",
				classification,
				resolution: "carried unchanged",
				proof: `pending:git diff --exit-code ${fork12} HEAD -- ${p}`,
			});
		} else if (conflictPaths.has(p)) {
			rows.push({
				surfaceId: p,
				path: p,
				scope: "shared",
				sourceIds,
				forkBehavior,
				upstreamChange: "upstream changed; conflict predicted",
				classification,
				resolution: "resolve during integration",
				proof: "pending:resolve and name the focused test",
			});
		} else {
			rows.push({
				surfaceId: p,
				path: p,
				scope: "shared",
				sourceIds,
				forkBehavior,
				upstreamChange: "upstream changed; merges cleanly",
				classification,
				resolution: "auto-merged",
				proof: "pending:session-system/update.sh gates 3-12",
			});
		}
	}

	return rows;
}

export interface SeedReviewOptions {
	target: string;
	version: string;
	fork?: string;
	baseline?: string;
	inventory?: string;
	dir?: string;
	cwd?: string;
	gitRunner?: GitRunner;
}

export interface SeedResult {
	recordPath: string;
	sourcesPath: string;
	matrixPath: string;
	changelogPath: string;
	handoffPath: string;
	targetSha: string;
	forkSha: string;
	baseSha: string;
	versionMin: string;
	versionMax: string;
	matrixRows: MatrixRow[];
	computedSources: SourceRecord[];
	computedUpstream: UpstreamChange[];
}

export async function seedReview(options: SeedReviewOptions): Promise<SeedResult> {
	const cwd = options.cwd;
	const git: GitRunner = options.gitRunner ?? ((args, okExitCodes) => defaultGit(args, okExitCodes, cwd));

	const targetSha = (await git(["rev-parse", `${options.target}^{commit}`])).trim();
	const forkRev = options.fork ?? "HEAD";
	const forkSha = (await git(["rev-parse", `${forkRev}^{commit}`])).trim();
	const target12 = targetSha.slice(0, 12);
	const fork12 = forkSha.slice(0, 12);

	const dir = options.dir ?? "docs";
	const resolveFile = (p: string): string => (cwd && !path.isAbsolute(p) ? path.join(cwd, p) : p);

	// Baseline path resolution
	let baselinePath = options.baseline;
	if (!baselinePath) {
		const defaultBaseline = path.join(dir, "upstream-baseline.json");
		const resolvedDefault = resolveFile(defaultBaseline);
		if (await Bun.file(resolvedDefault).exists()) {
			baselinePath = defaultBaseline;
		} else {
			baselinePath = "docs/upstream-baseline.json";
		}
	}
	const baselineFile = Bun.file(resolveFile(baselinePath));
	if (!(await baselineFile.exists())) {
		throw new Error(`baseline record missing: ${baselinePath}`);
	}
	const baselineJson = JSON.parse(await baselineFile.text()) as Record<string, unknown>;
	const upstreamRepo = typeof baselineJson.upstream_repo === "string" ? baselineJson.upstream_repo : "";
	if (!upstreamRepo) {
		throw new Error(`baseline ${baselinePath} missing upstream_repo`);
	}
	const baselineTarget = typeof baselineJson.target === "string" ? baselineJson.target : "";
	if (!/^[0-9a-f]{40}$/.test(baselineTarget)) {
		throw new Error(`baseline ${baselinePath} target must be 40-hex commit`);
	}
	const baseSha = (await git(["rev-parse", `${baselineTarget}^{commit}`])).trim();
	const baselineVersion =
		typeof baselineJson.upstream_version === "string"
			? baselineJson.upstream_version
			: typeof baselineJson.version_max === "string"
				? baselineJson.version_max
				: "";

	// Inventory path resolution
	let inventoryPath = options.inventory;
	if (!inventoryPath) {
		const defaultInventory = path.join(dir, "upstream-fork-inventory.tsv");
		const resolvedDefault = resolveFile(defaultInventory);
		if (await Bun.file(resolvedDefault).exists()) {
			inventoryPath = defaultInventory;
		} else {
			inventoryPath = "docs/upstream-fork-inventory.tsv";
		}
	}
	const inventoryFile = Bun.file(resolveFile(inventoryPath));
	let inventoryByPath = new Map<string, InventoryRow>();
	if (await inventoryFile.exists()) {
		const inventoryRows = parseInventoryTsv(await inventoryFile.text());
		inventoryByPath = new Map(inventoryRows.map(r => [r.path, r]));
	}

	const versionMax = options.version;
	const versionMin = await findVersionMin(targetSha, baselineVersion, versionMax, git);

	const stem = path.join(dir, `upstream-review-${target12}`);
	const recordRelPath = path.join(dir, `upstream-review-${target12}.json`);
	const sourcesRelPath = `${stem}-sources.tsv`;
	const matrixRelPath = `${stem}-matrix.tsv`;
	const changelogRelPath = `${stem}-changelog.tsv`;
	const handoffRelPath = `${stem}-handoff.md`;

	const recordDiskPath = resolveFile(recordRelPath);
	const sourcesDiskPath = resolveFile(sourcesRelPath);
	const matrixDiskPath = resolveFile(matrixRelPath);
	const changelogDiskPath = resolveFile(changelogRelPath);
	const handoffDiskPath = resolveFile(handoffRelPath);

	// Diff commands
	const diffRange = `${baseSha}..${forkSha}`;
	const targetRange = `${baseSha}..${targetSha}`;
	const [rawText, numstatText, diffText, forkNames, targetRawText, mergeTreeText] = await Promise.all([
		git(["diff", "--raw", "--no-renames", "--abbrev=40", "--no-color", diffRange]),
		git(["diff", "--numstat", "--no-renames", "--no-color", diffRange]),
		git(["diff", "--unified=0", "--no-renames", "--no-color", diffRange]),
		git(["diff", "--name-only", "--no-renames", "--no-color", diffRange]),
		git(["diff", "--raw", "--no-renames", "--abbrev=40", "--no-color", targetRange]),
		git(["merge-tree", "--write-tree", "--no-messages", "--merge-base", baseSha, forkSha, targetSha], [0, 1]),
	]);

	const computedSources = computeSourceRecords(rawText, numstatText, diffText);
	const computedUpstream = computeUpstreamChanges(targetRawText);
	const conflictPaths = parseMergeTreeConflicts(mergeTreeText);
	const forkPaths = new Set(forkNames.split("\n").filter(Boolean).map(unquoteGitPath));
	const targetPaths = new Set(computedUpstream.map(c => c.path));

	// Existing matrix
	const existingMatrixFile = Bun.file(matrixDiskPath);
	let existingMatrixByPath = new Map<string, MatrixRow>();
	if (await existingMatrixFile.exists()) {
		try {
			const existingRows = parseMatrixTsv(await existingMatrixFile.text());
			existingMatrixByPath = new Map(existingRows.map(r => [r.path, r]));
		} catch {}
	}

	const matrixRows = buildMatrixRows(
		forkPaths,
		targetPaths,
		conflictPaths,
		computedSources,
		inventoryByPath,
		existingMatrixByPath,
		fork12,
	);

	// Write companion files
	await Bun.write(sourcesDiskPath, formatSourcesTsv(computedSources));
	await Bun.write(matrixDiskPath, formatMatrixTsv(matrixRows));

	const changelogFile = Bun.file(changelogDiskPath);
	if (!(await changelogFile.exists())) {
		await Bun.write(changelogDiskPath, formatChangelogTsv([]));
	}

	const handoffFile = Bun.file(handoffDiskPath);
	if (!(await handoffFile.exists())) {
		await Bun.write(handoffDiskPath, "");
	}

	// Read existing record if present to keep any extra properties
	const recordFile = Bun.file(recordDiskPath);
	let existingRecord: Record<string, unknown> = {};
	if (await recordFile.exists()) {
		try {
			existingRecord = (await recordFile.json()) as Record<string, unknown>;
		} catch {}
	}

	const recordData: Record<string, unknown> = {
		...existingRecord,
		upstream_repo: upstreamRepo,
		upstream_version: versionMax,
		base: baseSha,
		fork: forkSha,
		target: targetSha,
		version_min: versionMin,
		version_max: versionMax,
		sources: sourcesRelPath,
		matrix: matrixRelPath,
		changelog: changelogRelPath,
		handoff: handoffRelPath,
		upstream_changes: computedUpstream.map(formatUpstreamEntry),
	};

	await Bun.write(recordDiskPath, `${JSON.stringify(recordData, null, "\t")}\n`);

	return {
		recordPath: recordRelPath,
		sourcesPath: sourcesRelPath,
		matrixPath: matrixRelPath,
		changelogPath: changelogRelPath,
		handoffPath: handoffRelPath,
		targetSha,
		forkSha,
		baseSha,
		versionMin,
		versionMax,
		matrixRows,
		computedSources,
		computedUpstream,
	};
}

async function main(): Promise<void> {
	let args: ReviewSeedArgs;
	try {
		args = parseArgs(process.argv.slice(2));
	} catch (err) {
		console.error(`usage error: ${err instanceof Error ? err.message : err}`);
		process.exit(2);
	}

	try {
		const result = await seedReview(args);
		console.log(
			`seeded review ${result.recordPath} (${result.matrixRows.length} matrix rows, ${result.computedSources.length} sources, ${result.computedUpstream.length} upstream changes)`,
		);
	} catch (err) {
		console.error(`ERROR: ${err instanceof Error ? err.message : err}`);
		process.exit(1);
	}
}

if (import.meta.main) {
	await main();
}
