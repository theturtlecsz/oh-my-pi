#!/usr/bin/env bun
// upstream-review-seed.ts — create or refresh a review record and its companions, or settle proofs (OMP-401).
//
// CLI:
//   bun scripts/upstream-review-seed.ts --target <sha> --version <X.Y.Z> [--fork <rev>]
//     [--baseline <json>] [--inventory <tsv>] [--dir docs]
//   bun scripts/upstream-review-seed.ts --record <rec> --settle --gates-passed-at <commit>
//
// In seed mode, generates or updates <dir>/upstream-review-<target12>.json and its companion files:
//   <stem>-sources.tsv, <stem>-matrix.tsv, <stem>-changelog.tsv, <stem>-handoff.md
//
// In settle mode (--settle), validates commit containment against target, settles fork-only proofs
// and gate proofs (session-system/update.sh gates 3-12), writes updated matrix and changelog TSVs,
// lists any remaining pending proofs, and exits 1 if pending rows remain, else 0 (exit 2 if target
// not contained). A fork-only proof is settled only when the row's scope is fork-only and its
// pending command names this record's fork (12-hex prefix). Any other proof is left as written.
//
// Changelog rows are the entries deriveChangelogEntries produces for every
// packages/*/CHANGELOG.md at the target, over [version_min, version_max].
// An existing row matched by entry_id is kept verbatim; a row that is no longer
// derived is dropped. A new Added row is `adopted` with proof
// `pending:session-system/update.sh gates 3-12`. Any other new row is `re-fitted`
// with proof `pending:decide re-fitted or not-applicable`.
//
// Handoff text outside <!-- seed:index:begin --> and <!-- seed:index:end --> is
// kept (the markers are appended when missing). The block between them is
// regenerated as a sorted list of every surface id, source id, and changelog entry id.

import * as path from "node:path";
import { type InventoryRow, parseInventoryTsv } from "./upstream-inventory.ts";
import {
	type ChangelogRow,
	compareVersions,
	computeSourceRecords,
	computeUpstreamChanges,
	type DerivedEntry,
	deriveChangelogEntries,
	formatChangelogTsv,
	formatMatrixTsv,
	formatSourcesTsv,
	formatUpstreamEntry,
	type MatrixRow,
	parseChangelogTsv,
	parseMatrixTsv,
	parseMergeTreeConflicts,
	parseRecord,
	type SourceRecord,
	type UpstreamChange,
	unquoteGitPath,
	type VersionRange,
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

export class TargetNotContainedError extends Error {
	constructor(target: string, commit: string) {
		super(`commit ${commit} does not contain target ${target}`);
		this.name = "TargetNotContainedError";
	}
}

export interface PendingRow {
	kind: "matrix" | "changelog";
	id: string;
	proof: string;
}

export interface ReviewSeedArgs {
	settle?: boolean;
	record?: string;
	gatesPassedAt?: string;
	target?: string;
	version?: string;
	fork: string;
	baseline?: string;
	inventory?: string;
	dir: string;
}

export function parseArgs(argv: string[]): ReviewSeedArgs {
	let settle = false;
	let record: string | undefined;
	let gatesPassedAt: string | undefined;
	let target: string | undefined;
	let version: string | undefined;
	let fork = "HEAD";
	let baseline: string | undefined;
	let inventory: string | undefined;
	let dir = "docs";

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--settle") {
			settle = true;
		} else if (arg === "--record") {
			record = argv[++i];
			if (!record) throw new Error("missing value for --record");
		} else if (arg === "--gates-passed-at") {
			gatesPassedAt = argv[++i];
			if (!gatesPassedAt) throw new Error("missing value for --gates-passed-at");
		} else if (arg === "--target") {
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

	if (settle) {
		if (!record) throw new Error("missing required --record");
		if (!gatesPassedAt) throw new Error("missing required --gates-passed-at");
		if (target) throw new Error("--target cannot be combined with --settle");
		if (version) throw new Error("--version cannot be combined with --settle");
		if (baseline) throw new Error("--baseline cannot be combined with --settle");
		if (inventory) throw new Error("--inventory cannot be combined with --settle");
		return { settle: true, record, gatesPassedAt, fork, dir };
	}

	if (record) throw new Error("--record requires --settle");
	if (gatesPassedAt) throw new Error("--gates-passed-at requires --settle");
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

const ADDED_CHANGELOG_PROOF = "pending:session-system/update.sh gates 3-12";
const OTHER_CHANGELOG_PROOF = "pending:decide re-fitted or not-applicable";

/** One ledger row per derived entry. Existing entry_id rows stay verbatim; the rest are dropped. */
export function buildChangelogRows(derived: DerivedEntry[], existing: ChangelogRow[]): ChangelogRow[] {
	const existingById = new Map(existing.map(row => [row.id, row]));
	return derived.map(entry => {
		const kept = existingById.get(entry.id);
		if (kept) return kept;
		const added = entry.section === "Added";
		return {
			id: entry.id,
			pkg: entry.pkg,
			version: entry.version,
			section: entry.section,
			text: entry.text,
			disposition: added ? "adopted" : "re-fitted",
			proof: added ? ADDED_CHANGELOG_PROOF : OTHER_CHANGELOG_PROOF,
		};
	});
}

export const SEED_INDEX_BEGIN = "<!-- seed:index:begin -->";
export const SEED_INDEX_END = "<!-- seed:index:end -->";

/** Sorted markdown list of every surface id, source id, and changelog entry id. */
export function buildHandoffIndex(
	surfaceIds: readonly string[],
	sourceIds: readonly string[],
	entryIds: readonly string[],
): string {
	return [...surfaceIds, ...sourceIds, ...entryIds]
		.sort()
		.map(id => `- ${id}`)
		.join("\n");
}

/**
 * Replace the seed-index block, or append the markers when the pair is missing.
 * Text outside the markers is preserved byte for byte.
 */
export function applyHandoffIndex(existing: string, indexBody: string): string {
	const block = `${SEED_INDEX_BEGIN}\n${indexBody}${indexBody ? "\n" : ""}${SEED_INDEX_END}`;
	const begin = existing.indexOf(SEED_INDEX_BEGIN);
	const end = begin >= 0 ? existing.indexOf(SEED_INDEX_END, begin + SEED_INDEX_BEGIN.length) : -1;
	if (begin >= 0 && end >= 0) {
		return existing.slice(0, begin) + block + existing.slice(end + SEED_INDEX_END.length);
	}
	if (existing.length === 0) return `${block}\n`;
	return `${existing}${existing.endsWith("\n") ? "" : "\n"}${block}\n`;
}

async function deriveTargetChangelogEntries(
	targetSha: string,
	range: VersionRange,
	git: GitRunner,
): Promise<DerivedEntry[]> {
	const lsText = await git(["ls-tree", "-r", "--name-only", targetSha]);
	const changelogPaths = lsText.split("\n").filter(p => /^packages\/[^/]+\/CHANGELOG\.md$/.test(p));
	const entries: DerivedEntry[] = [];
	for (const clPath of changelogPaths) {
		const pkg = clPath.split("/")[1];
		entries.push(...deriveChangelogEntries(pkg, await git(["show", `${targetSha}:${clPath}`]), range));
	}
	return entries;
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
	changelogRows: ChangelogRow[];
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
	// Diff the pinned trees under the fork pin's own attributes: the working
	// tree's .gitattributes (e.g. a later `binary` marking) must not change the record.
	const attrSource = `--attr-source=${forkSha}`;
	const [rawText, numstatText, diffText, forkNames, targetRawText, mergeTreeText] = await Promise.all([
		git([attrSource, "diff", "--raw", "--no-renames", "--abbrev=40", "--no-color", diffRange]),
		git([attrSource, "diff", "--numstat", "--no-renames", "--no-color", diffRange]),
		git([attrSource, "diff", "--unified=0", "--no-renames", "--no-color", diffRange]),
		git([attrSource, "diff", "--name-only", "--no-renames", "--no-color", diffRange]),
		git([attrSource, "diff", "--raw", "--no-renames", "--abbrev=40", "--no-color", targetRange]),
		git([attrSource, "merge-tree", "--write-tree", "--no-messages", "--merge-base", baseSha, forkSha, targetSha], [0, 1]),
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

	const derivedEntries = await deriveTargetChangelogEntries(targetSha, { min: versionMin, max: versionMax }, git);
	let existingChangelog: ChangelogRow[] = [];
	const existingChangelogFile = Bun.file(changelogDiskPath);
	if (await existingChangelogFile.exists()) {
		try {
			existingChangelog = parseChangelogTsv(await existingChangelogFile.text());
		} catch {}
	}
	const changelogRows = buildChangelogRows(derivedEntries, existingChangelog);

	const existingHandoffFile = Bun.file(handoffDiskPath);
	const existingHandoff = (await existingHandoffFile.exists()) ? await existingHandoffFile.text() : "";
	const handoffText = applyHandoffIndex(
		existingHandoff,
		buildHandoffIndex(
			matrixRows.map(row => row.surfaceId),
			computedSources.map(source => source.id),
			changelogRows.map(row => row.id),
		),
	);

	// Write companion files
	await Bun.write(sourcesDiskPath, formatSourcesTsv(computedSources));
	await Bun.write(matrixDiskPath, formatMatrixTsv(matrixRows));
	await Bun.write(changelogDiskPath, formatChangelogTsv(changelogRows));
	await Bun.write(handoffDiskPath, handoffText);

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
		changelogRows,
		computedSources,
		computedUpstream,
	};
}

export interface SettleReviewOptions {
	record: string;
	gatesPassedAt: string;
	cwd?: string;
	gitRunner?: GitRunner;
}

export interface SettleResult {
	recordPath: string;
	matrixPath: string;
	changelogPath: string;
	settledForkOnlyCount: number;
	settledGateCount: number;
	pendingRows: PendingRow[];
}

export async function settleReview(options: SettleReviewOptions): Promise<SettleResult> {
	const cwd = options.cwd;
	const git: GitRunner = options.gitRunner ?? ((args, okExitCodes) => defaultGit(args, okExitCodes, cwd));
	const resolveFile = (p: string): string => (cwd && !path.isAbsolute(p) ? path.join(cwd, p) : p);

	const recordDiskPath = resolveFile(options.record);
	const recordFile = Bun.file(recordDiskPath);
	if (!(await recordFile.exists())) {
		throw new Error(`record file missing: ${options.record}`);
	}
	const recordText = await recordFile.text();
	const record = parseRecord(recordText, options.record);

	let commitSha: string;
	try {
		commitSha = (await git(["rev-parse", `${options.gatesPassedAt}^{commit}`])).trim();
	} catch {
		throw new TargetNotContainedError(record.target, options.gatesPassedAt);
	}

	const targetSha = record.target;
	try {
		await git(["merge-base", "--is-ancestor", targetSha, commitSha]);
	} catch {
		throw new TargetNotContainedError(targetSha, commitSha);
	}

	const commit12 = commitSha.slice(0, 12);
	const forkSha = record.fork;
	const fork12 = forkSha.slice(0, 12);

	const matrixDiskPath = resolveFile(record.matrix);
	const matrixFile = Bun.file(matrixDiskPath);
	if (!(await matrixFile.exists())) {
		throw new Error(`matrix file missing: ${record.matrix}`);
	}
	const matrixRows = parseMatrixTsv(await matrixFile.text());

	// Differing paths between fork and commit
	const changedForkDiff = await git(["diff", "--name-only", "--no-renames", `${forkSha}..${commitSha}`]);
	const changedForkSet = new Set(changedForkDiff.split("\n").filter(Boolean).map(unquoteGitPath));

	let settledForkOnlyCount = 0;
	let settledGateCount = 0;

	for (const row of matrixRows) {
		const forkOnlyPending = `pending:git diff --exit-code ${fork12} HEAD -- ${row.path}`;
		if (row.scope === "fork-only" && row.proof === forkOnlyPending) {
			if (!changedForkSet.has(row.path)) {
				row.proof = `fork-only sweep: git diff ${fork12}..${commit12} -- ${row.path} is empty`;
				settledForkOnlyCount++;
			}
		} else if (row.proof === "pending:session-system/update.sh gates 3-12") {
			row.proof = `session-system/update.sh gates 3-12 passed at ${commit12} (operator-recorded)`;
			settledGateCount++;
		}
	}

	const changelogDiskPath = resolveFile(record.changelog);
	const changelogFile = Bun.file(changelogDiskPath);
	if (!(await changelogFile.exists())) {
		throw new Error(`changelog file missing: ${record.changelog}`);
	}
	const changelogRows = parseChangelogTsv(await changelogFile.text());

	for (const row of changelogRows) {
		if (row.proof === "pending:session-system/update.sh gates 3-12") {
			row.proof = `session-system/update.sh gates 3-12 passed at ${commit12} (operator-recorded)`;
			settledGateCount++;
		}
	}

	await Bun.write(matrixDiskPath, formatMatrixTsv(matrixRows));
	await Bun.write(changelogDiskPath, formatChangelogTsv(changelogRows));

	const pendingRows: PendingRow[] = [];
	for (const r of matrixRows) {
		if (r.proof.startsWith("pending:")) {
			pendingRows.push({ kind: "matrix", id: r.surfaceId, proof: r.proof });
		}
	}
	for (const r of changelogRows) {
		if (r.proof.startsWith("pending:")) {
			pendingRows.push({ kind: "changelog", id: r.id, proof: r.proof });
		}
	}

	return {
		recordPath: options.record,
		matrixPath: record.matrix,
		changelogPath: record.changelog,
		settledForkOnlyCount,
		settledGateCount,
		pendingRows,
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

	if (args.settle) {
		try {
			const result = await settleReview({
				record: args.record!,
				gatesPassedAt: args.gatesPassedAt!,
			});
			if (result.pendingRows.length > 0) {
				for (const r of result.pendingRows) {
					console.error(`${r.kind} ${r.id}: ${r.proof}`);
				}
				process.exit(1);
			}
			console.log(`PASS: review settled at ${args.gatesPassedAt}`);
			process.exit(0);
		} catch (err) {
			if (err instanceof TargetNotContainedError) {
				console.error(`ERROR: ${err.message}`);
				process.exit(2);
			}
			console.error(`ERROR: ${err instanceof Error ? err.message : err}`);
			process.exit(1);
		}
	}

	try {
		const result = await seedReview(args as SeedReviewOptions);
		console.log(
			`seeded review ${result.recordPath} (${result.matrixRows.length} matrix rows, ${result.computedSources.length} sources, ${result.changelogRows.length} changelog entries, ${result.computedUpstream.length} upstream changes)`,
		);
	} catch (err) {
		console.error(`ERROR: ${err instanceof Error ? err.message : err}`);
		process.exit(1);
	}
}

if (import.meta.main) {
	await main();
}
