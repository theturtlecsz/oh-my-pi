#!/usr/bin/env bun
// verify-upstream-handoff.ts — completeness oracle for upstream incorporations (OMP-156,
// generalized into the standing guardrail by OMP-229).
//
// Freezes one source record per zero-context fork diff hunk (base..fork) plus one per
// binary/rename/delete record, then proves the fork matrix, upstream changelog ledger,
// and human handoff account for every record — and that every merge conflict predicted
// by `git merge-tree` has a matrix row — before an upstream candidate may be accepted.
//
// Record-driven invocation (standing guardrail):
//   bun scripts/verify-upstream-handoff.ts --record docs/upstream/baseline.json [--allow-pending]
//   bun scripts/verify-upstream-handoff.ts --record docs/upstream/reviews/<sha12>/review.json [--allow-pending]
//
// The record JSON pins base/fork/target commits, the changelog version range, and the
// four record files (sources/matrix/changelog/handoff). --record is the only supported
// invocation; explicit per-flag pins were removed with the OMP-229 generalization.
//
// --write-sources: recompute source records from git and (over)write the sources TSV. Run
// exactly once before merging to freeze the manifest; every later run must verify against it.
// --allow-pending: permit proofs of the exact form `pending:<command or live probe>`. The
// pre-cutover run must pass without this flag.
// --report <file>: additionally write the itemized incompatibility report as markdown.

import { createHash } from "node:crypto";

export const SOURCES_HEADER = ["source_id", "path", "kind", "locator", "body_sha"] as const;
export const MATRIX_HEADER = [
	"surface_id",
	"path",
	"scope",
	"source_ids",
	"fork_behavior",
	"upstream_change",
	"classification",
	"resolution",
	"proof",
] as const;
export const CHANGELOG_HEADER = ["entry_id", "package", "version", "section", "text", "disposition", "proof"] as const;

export type SourceKind = "hunk" | "binary" | "rename" | "delete" | "meta";

export interface SourceRecord {
	id: string;
	path: string;
	kind: SourceKind;
	locator: string;
	bodySha: string;
}

export interface MatrixRow {
	surfaceId: string;
	path: string;
	scope: string;
	sourceIds: string[];
	forkBehavior: string;
	upstreamChange: string;
	classification: string;
	resolution: string;
	proof: string;
}

export interface ChangelogRow {
	id: string;
	pkg: string;
	version: string;
	section: string;
	text: string;
	disposition: string;
	proof: string;
}

export interface DerivedEntry {
	id: string;
	pkg: string;
	version: string;
	section: string;
	text: string;
}

const sha12 = (input: string): string => createHash("sha256").update(input).digest("hex").slice(0, 12);

export function sourceId(kind: SourceKind, path: string, locator: string, bodySha: string): string {
	return `s${sha12(`${kind}\0${path}\0${locator}\0${bodySha}`)}`;
}

/** Unquote a git C-quoted path ("a/b\303\251.txt"); pass plain paths through. */
export function unquoteGitPath(raw: string): string {
	if (!raw.startsWith('"') || !raw.endsWith('"')) return raw;
	const inner = raw.slice(1, -1);
	const bytes: number[] = [];
	for (let i = 0; i < inner.length; i++) {
		const ch = inner[i];
		if (ch !== "\\") {
			bytes.push(ch.charCodeAt(0));
			continue;
		}
		const next = inner[++i];
		if (next >= "0" && next <= "7") {
			let oct = next;
			while (oct.length < 3 && inner[i + 1] >= "0" && inner[i + 1] <= "7") oct += inner[++i];
			bytes.push(Number.parseInt(oct, 8));
		} else {
			const map: Record<string, string> = { n: "\n", t: "\t", r: "\r", '"': '"', "\\": "\\" };
			bytes.push((map[next] ?? next).charCodeAt(0));
		}
	}
	return Buffer.from(bytes).toString("utf8");
}

interface RawFileChange {
	status: string;
	oldSha: string;
	newSha: string;
	path: string;
	oldPath?: string;
}

/** Parse `git diff --raw --no-renames --abbrev=40` output. */
export function parseRawDiff(rawText: string): RawFileChange[] {
	const out: RawFileChange[] = [];
	for (const line of rawText.split("\n")) {
		if (!line.startsWith(":")) continue;
		// :100644 100644 <old> <new> <status>\t<path>[\t<newpath>]
		const tab = line.indexOf("\t");
		if (tab < 0) continue;
		const meta = line.slice(0, tab).split(/\s+/);
		const paths = line
			.slice(tab + 1)
			.split("\t")
			.map(unquoteGitPath);
		const status = meta[4] ?? "";
		if (status.startsWith("R") || status.startsWith("C")) {
			out.push({ status: status[0], oldSha: meta[2], newSha: meta[3], path: paths[1], oldPath: paths[0] });
		} else {
			out.push({ status, oldSha: meta[2], newSha: meta[3], path: paths[0] });
		}
	}
	return out;
}

/** Paths whose numstat marks them binary (`-` added/deleted counts). */
export function parseBinaryPaths(numstatText: string): Set<string> {
	const out = new Set<string>();
	for (const line of numstatText.split("\n")) {
		const m = line.match(/^-\t-\t(.+)$/);
		if (m) out.add(unquoteGitPath(m[1]));
	}
	return out;
}

/** Parse `git diff --unified=0 --no-renames` text into per-path hunks. */
export function parseHunks(diffText: string): Array<{ path: string; locator: string; body: string }> {
	const out: Array<{ path: string; locator: string; body: string }> = [];
	let path = "";
	let locator = "";
	let body: string[] = [];
	let inHunk = false;
	const flush = () => {
		if (inHunk) out.push({ path, locator, body: body.join("\n") });
		inHunk = false;
		body = [];
	};
	for (const line of diffText.split("\n")) {
		if (line.startsWith("diff --git ")) {
			flush();
			// `diff --git a/X b/Y` — with --no-renames X and Y only differ for create/delete prefixes.
			const m = line.match(/^diff --git "?a\/.*? "?b\/(.*?)"?$/);
			const rawRest = line.slice("diff --git ".length);
			// Robust split: quoted paths contain no unescaped ` b/`; fall back to regex capture.
			const bIdx = rawRest.lastIndexOf(rawRest.startsWith('"') ? ' "b/' : " b/");
			if (bIdx >= 0) {
				const bPart = rawRest.slice(bIdx + 1);
				path = unquoteGitPath(bPart.startsWith('"') ? `"${bPart.slice(2, -1)}"` : bPart.slice(2));
			} else if (m) {
				path = unquoteGitPath(m[1]);
			}
			continue;
		}
		const hm = line.match(/^@@ (-\d+(?:,\d+)? \+\d+(?:,\d+)?) @@/);
		if (hm) {
			flush();
			inHunk = true;
			locator = hm[1];
			continue;
		}
		if (inHunk) {
			if (line.startsWith("+") || line.startsWith("-") || line.startsWith("\\")) {
				body.push(line);
			} else {
				flush();
			}
		}
	}
	flush();
	return out;
}

/** Compute the frozen source-record set from raw git outputs. */
export function computeSourceRecords(rawText: string, numstatText: string, diffText: string): SourceRecord[] {
	const records: SourceRecord[] = [];
	const binaries = parseBinaryPaths(numstatText);
	for (const change of parseRawDiff(rawText)) {
		if (change.status === "D") {
			records.push(make("delete", change.path, "delete", change.oldSha.slice(0, 12)));
			continue;
		}
		if (change.oldPath !== undefined) {
			records.push(
				make(
					"rename",
					change.path,
					`from:${change.oldPath}`,
					`${change.oldSha.slice(0, 12)}>${change.newSha.slice(0, 12)}`,
				),
			);
		}
		if (binaries.has(change.path)) {
			records.push(
				make("binary", change.path, "binary", `${change.oldSha.slice(0, 12)}>${change.newSha.slice(0, 12)}`),
			);
		}
	}
	for (const hunk of parseHunks(diffText)) {
		records.push(make("hunk", hunk.path, hunk.locator, sha12(hunk.body)));
	}
	// Content-less changes (empty-file adds, mode-only edits) produce neither hunks nor
	// binary/delete records; emit a file-level meta record so every changed path is covered.
	const coveredPaths = new Set(records.map(r => r.path));
	for (const change of parseRawDiff(rawText)) {
		if (!coveredPaths.has(change.path)) {
			records.push(
				make(
					"meta",
					change.path,
					`status:${change.status}`,
					`${change.oldSha.slice(0, 12)}>${change.newSha.slice(0, 12)}`,
				),
			);
		}
	}
	records.sort((a, b) =>
		a.path < b.path ? -1 : a.path > b.path ? 1 : a.locator < b.locator ? -1 : a.locator > b.locator ? 1 : 0,
	);
	return records;

	function make(kind: SourceKind, path: string, locator: string, bodySha: string): SourceRecord {
		return { id: sourceId(kind, path, locator, bodySha), path, kind, locator, bodySha };
	}
}

export function compareVersions(a: string, b: string): number {
	const pa = a.split(".").map(Number);
	const pb = b.split(".").map(Number);
	for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
		const d = (pa[i] ?? 0) - (pb[i] ?? 0);
		if (d !== 0) return d;
	}
	return 0;
}

export interface VersionRange {
	min: string;
	max: string;
}

export function versionInRange(v: string, range: VersionRange): boolean {
	return /^\d+\.\d+\.\d+$/.test(v) && compareVersions(v, range.min) >= 0 && compareVersions(v, range.max) <= 0;
}

const SECTION_SLUGS: Record<string, string> = {
	Added: "added",
	"Breaking Changes": "breaking",
	Removed: "removed",
};

/** Derive ledger entries from one package changelog at the pinned target. */
export function deriveChangelogEntries(pkg: string, changelogText: string, range: VersionRange): DerivedEntry[] {
	const out: DerivedEntry[] = [];
	let version: string | null = null;
	let section: string | null = null;
	let indexInSection = 0;
	let current: DerivedEntry | null = null;
	const flush = () => {
		if (current) out.push(current);
		current = null;
	};
	for (const line of changelogText.split("\n")) {
		const mv = line.match(/^## \[([^\]]+)\]/);
		if (mv) {
			flush();
			version = mv[1] === "Unreleased" ? null : mv[1];
			section = null;
			continue;
		}
		const ms = line.match(/^### (.+?)\s*$/);
		if (ms) {
			flush();
			section = ms[1];
			indexInSection = 0;
			continue;
		}
		const slug = section ? SECTION_SLUGS[section] : undefined;
		if (!version || !slug || !versionInRange(version, range)) continue;
		if (line.startsWith("- ")) {
			flush();
			indexInSection++;
			current = {
				id: `${pkg}@${version}:${slug}:${indexInSection}`,
				pkg,
				version,
				section: section as string,
				text: line.slice(2).replaceAll("\t", " ").trim(),
			};
		} else if (current && /^\s+\S/.test(line)) {
			current.text += ` ${line.replaceAll("\t", " ").trim()}`;
		} else if (current && line.trim() === "") {
			flush();
		}
	}
	flush();
	return out;
}

function splitTsv(text: string, header: readonly string[], label: string): string[][] {
	const lines = text.replace(/^\uFEFF/, "").split("\n");
	while (lines.length && lines[lines.length - 1] === "") lines.pop();
	if (!lines.length) throw new Error(`${label}: empty file`);
	const got = lines[0].split("\t");
	if (got.length !== header.length || header.some((h, i) => got[i] !== h)) {
		throw new Error(`${label}: bad header — expected ${header.join("\\t")}`);
	}
	return lines.slice(1).map((line, i) => {
		const cells = line.split("\t");
		if (cells.length !== header.length) {
			throw new Error(`${label}: row ${i + 2} has ${cells.length} fields, expected ${header.length}`);
		}
		return cells;
	});
}

export function parseSourcesTsv(text: string): SourceRecord[] {
	return splitTsv(text, SOURCES_HEADER, "sources").map(([id, path, kind, locator, bodySha]) => ({
		id,
		path,
		kind: kind as SourceKind,
		locator,
		bodySha,
	}));
}

export function parseMatrixTsv(text: string): MatrixRow[] {
	return splitTsv(text, MATRIX_HEADER, "matrix").map(cells => ({
		surfaceId: cells[0],
		path: cells[1],
		scope: cells[2],
		sourceIds: cells[3]
			.split(",")
			.map(s => s.trim())
			.filter(Boolean),
		forkBehavior: cells[4],
		upstreamChange: cells[5],
		classification: cells[6],
		resolution: cells[7],
		proof: cells[8],
	}));
}

export function parseChangelogTsv(text: string): ChangelogRow[] {
	return splitTsv(text, CHANGELOG_HEADER, "changelog").map(cells => ({
		id: cells[0],
		pkg: cells[1],
		version: cells[2],
		section: cells[3],
		text: cells[4],
		disposition: cells[5],
		proof: cells[6],
	}));
}

export function formatSourcesTsv(records: SourceRecord[]): string {
	const lines = [SOURCES_HEADER.join("\t")];
	for (const r of records) lines.push([r.id, r.path, r.kind, r.locator, r.bodySha].join("\t"));
	return `${lines.join("\n")}\n`;
}

export interface ValidateInput {
	frozenSources: SourceRecord[];
	computedSources: SourceRecord[];
	matrix: MatrixRow[];
	changelogRows: ChangelogRow[];
	derivedEntries: DerivedEntry[];
	forkPaths: Set<string>;
	sharedPaths: Set<string>;
	/** Paths `git merge-tree fork target` predicts as conflicted; empty when unknown. */
	conflictPaths?: Set<string>;
	/** Frozen per-path upstream-change manifest from the record. */
	frozenUpstream: UpstreamChange[];
	/** Recomputed per-path upstream changes from base..target. */
	computedUpstream: UpstreamChange[];
	handoffText: string;
	allowPending: boolean;
}

function checkProof(proof: string, where: string, allowPending: boolean, errors: string[]): void {
	if (proof.startsWith("pending:")) {
		if (!allowPending) {
			errors.push(`${where}: unresolved pending proof (${proof.slice(0, 80)})`);
		} else if (!/^pending:\S/.test(proof)) {
			errors.push(`${where}: pending proof must name an exact command or live probe`);
		}
	}
}

export function validate(input: ValidateInput): string[] {
	const errors: string[] = [];
	const {
		frozenSources,
		computedSources,
		matrix,
		changelogRows,
		derivedEntries,
		forkPaths,
		sharedPaths,
		conflictPaths,
		frozenUpstream,
		computedUpstream,
		handoffText,
		allowPending,
	} = input;

	// 1. Frozen source manifest must equal the recomputed record set exactly.
	const frozenById = new Map(frozenSources.map(r => [r.id, r]));
	const computedById = new Map(computedSources.map(r => [r.id, r]));
	if (frozenById.size !== frozenSources.length) {
		const seen = new Set<string>();
		for (const r of frozenSources) {
			if (seen.has(r.id)) errors.push(`sources: duplicate source_id ${r.id}`);
			seen.add(r.id);
		}
	}
	for (const [id, rec] of computedById) {
		const frozen = frozenById.get(id);
		if (!frozen) {
			errors.push(`sources: missing record ${id} (${rec.kind} ${rec.path} ${rec.locator})`);
		} else if (
			frozen.path !== rec.path ||
			frozen.kind !== rec.kind ||
			frozen.locator !== rec.locator ||
			frozen.bodySha !== rec.bodySha
		) {
			errors.push(`sources: record ${id} differs from recomputed diff`);
		}
	}
	for (const id of frozenById.keys()) {
		if (!computedById.has(id)) errors.push(`sources: stale record ${id} not present in recomputed diff`);
	}

	// 2. Matrix field validity.
	const surfaceIds = new Set<string>();
	const referencedSources = new Set<string>();
	const matrixPaths = new Set<string>();
	for (const row of matrix) {
		const where = `matrix ${row.surfaceId || "<missing surface_id>"}`;
		if (surfaceIds.has(row.surfaceId)) errors.push(`${where}: duplicate surface_id`);
		surfaceIds.add(row.surfaceId);
		matrixPaths.add(row.path);
		const fields: Array<[string, string]> = [
			["surface_id", row.surfaceId],
			["path", row.path],
			["scope", row.scope],
			["fork_behavior", row.forkBehavior],
			["upstream_change", row.upstreamChange],
			["classification", row.classification],
			["resolution", row.resolution],
			["proof", row.proof],
		];
		for (const [name, value] of fields) {
			if (!value.trim()) errors.push(`${where}: empty ${name}`);
		}
		if (!row.sourceIds.length) errors.push(`${where}: empty source_ids`);
		if (row.scope !== "fork-only" && row.scope !== "shared") errors.push(`${where}: invalid scope '${row.scope}'`);
		if (!["retained", "re-fitted", "dropped"].includes(row.classification)) {
			errors.push(`${where}: invalid classification '${row.classification}'`);
		}
		if (row.classification === "dropped" && !row.resolution.includes("owner-ruling:")) {
			errors.push(`${where}: 'dropped' requires an explicit owner-ruling: reference in resolution`);
		}
		for (const sid of row.sourceIds) {
			referencedSources.add(sid);
			if (!frozenById.has(sid)) errors.push(`${where}: unknown source_id ${sid}`);
		}
		if (!forkPaths.has(row.path)) errors.push(`${where}: path not changed in base..fork`);
		const expectedScope = sharedPaths.has(row.path) ? "shared" : "fork-only";
		if ((row.scope === "shared") !== (expectedScope === "shared")) {
			errors.push(`${where}: scope '${row.scope}' contradicts computed '${expectedScope}'`);
		}
		checkProof(row.proof, where, allowPending, errors);
	}

	// 3. Coverage: every source mapped, every changed path present.
	for (const id of frozenById.keys()) {
		if (!referencedSources.has(id)) {
			const rec = frozenById.get(id) as SourceRecord;
			errors.push(`matrix: unmapped source_id ${id} (${rec.kind} ${rec.path})`);
		}
	}
	for (const path of forkPaths) {
		if (!matrixPaths.has(path)) errors.push(`matrix: changed path missing — ${path}`);
	}
	const matrixShared = new Set(matrix.filter(r => r.scope === "shared").map(r => r.path));
	for (const path of sharedPaths) {
		if (!matrixShared.has(path)) errors.push(`matrix: shared path missing/misscoped — ${path}`);
	}
	for (const path of matrixShared) {
		if (!sharedPaths.has(path)) errors.push(`matrix: path marked shared outside computed intersection — ${path}`);
	}

	// 3b. Conflicts: every merge-tree-predicted conflict must be accounted by a matrix
	// row whose resolution/proof rules above already apply.
	for (const path of conflictPaths ?? []) {
		if (!matrixPaths.has(path)) {
			errors.push(`conflict: predicted merge conflict in ${path} has no matrix row`);
		}
	}

	// 4. Changelog ledger equals derived entry set.
	const derivedById = new Map(derivedEntries.map(e => [e.id, e]));
	const rowsById = new Map<string, ChangelogRow>();
	for (const row of changelogRows) {
		const where = `changelog ${row.id || "<missing entry_id>"}`;
		if (rowsById.has(row.id)) errors.push(`${where}: duplicate entry_id`);
		rowsById.set(row.id, row);
		const derived = derivedById.get(row.id);
		if (!derived) {
			errors.push(`${where}: not derived from pinned-target changelogs`);
			continue;
		}
		if (derived.pkg !== row.pkg || derived.version !== row.version || derived.section !== row.section) {
			errors.push(`${where}: package/version/section differ from derived entry`);
		}
		if (derived.text !== row.text) errors.push(`${where}: text differs from derived entry`);
		if (!row.disposition.trim()) errors.push(`${where}: empty disposition`);
		if (!row.proof.trim()) errors.push(`${where}: empty proof`);
		const allowed =
			row.section === "Added" ? ["adopted", "re-fitted", "not-applicable"] : ["re-fitted", "not-applicable"];
		if (!allowed.includes(row.disposition)) {
			errors.push(`${where}: disposition '${row.disposition}' invalid for section '${row.section}'`);
		}
		checkProof(row.proof, where, allowPending, errors);
	}
	for (const id of derivedById.keys()) {
		if (!rowsById.has(id)) errors.push(`changelog: missing entry ${id}`);
	}

	// 4b. Frozen upstream-change manifest must equal the recomputed base..target set:
	// every upstream change — including target-only paths untouched by the fork —
	// must be enumerated by the record.
	const frozenUpstreamByPath = new Map(frozenUpstream.map(c => [c.path, c]));
	if (frozenUpstreamByPath.size !== frozenUpstream.length) {
		const seen = new Set<string>();
		for (const c of frozenUpstream) {
			if (seen.has(c.path)) errors.push(`upstream: duplicate record for ${c.path}`);
			seen.add(c.path);
		}
	}
	const computedUpstreamByPath = new Map(computedUpstream.map(c => [c.path, c]));
	for (const c of computedUpstream) {
		const frozen = frozenUpstreamByPath.get(c.path);
		if (!frozen) {
			errors.push(`upstream: unaccounted upstream change ${c.path} (${c.status})`);
		} else if (frozen.status !== c.status || frozen.blobs !== c.blobs) {
			errors.push(`upstream: record ${c.path} differs from recomputed upstream diff`);
		}
	}
	for (const path of frozenUpstreamByPath.keys()) {
		if (!computedUpstreamByPath.has(path)) {
			errors.push(`upstream: stale record ${path} not present in base..target`);
		}
	}

	// 5. Handoff must link every surface, source, and changelog ID.
	for (const id of surfaceIds) {
		if (!handoffText.includes(id)) errors.push(`handoff: missing surface link ${id}`);
	}
	for (const id of frozenById.keys()) {
		if (!handoffText.includes(id)) errors.push(`handoff: missing source link ${id}`);
	}
	for (const id of derivedById.keys()) {
		if (!handoffText.includes(id)) errors.push(`handoff: missing changelog link ${id}`);
	}

	return errors;
}

// ---------------------------------------------------------------------------
// Incompatibility report
// ---------------------------------------------------------------------------

export type ReportCategory = "fork" | "upstream" | "conflict" | "proof" | "record";

const REPORT_SECTIONS: Array<[ReportCategory, string]> = [
	["fork", "Unaccounted fork behavior"],
	["upstream", "Unaccounted upstream changes"],
	["conflict", "Unresolved merge conflicts"],
	["proof", "Failed or pending proofs"],
	["record", "Record integrity"],
];

export function categorizeError(error: string): ReportCategory {
	if (error.includes("pending proof")) return "proof";
	if (error.startsWith("conflict:")) return "conflict";
	if (error.startsWith("sources:") || error.startsWith("matrix")) return "fork";
	if (error.startsWith("changelog") || error.startsWith("upstream:")) return "upstream";
	return "record";
}

/** Group validation errors into the itemized incompatibility report (markdown). */
export function buildIncompatibilityReport(errors: string[]): string {
	const byCategory = new Map<ReportCategory, string[]>();
	for (const error of errors) {
		const category = categorizeError(error);
		const bucket = byCategory.get(category);
		if (bucket) bucket.push(error);
		else byCategory.set(category, [error]);
	}
	const lines = ["# Incompatibility report", "", `${errors.length} finding(s) block acceptance.`];
	for (const [category, title] of REPORT_SECTIONS) {
		const bucket = byCategory.get(category);
		if (!bucket) continue;
		lines.push("", `## ${title} (${bucket.length})`, "");
		for (const error of bucket) lines.push(`- ${error}`);
	}
	lines.push("");
	return lines.join("\n");
}

// ---------------------------------------------------------------------------
// Record files (docs/upstream/baseline.json, docs/upstream/reviews/*/review.json)
// ---------------------------------------------------------------------------

export interface UpstreamChange {
	status: string;
	blobs: string;
	path: string;
}

export interface GuardrailRecord {
	upstreamRepo: string;
	upstreamVersion: string;
	base: string;
	fork: string;
	target: string;
	versionMin: string;
	versionMax: string;
	sources: string;
	matrix: string;
	changelog: string;
	handoff: string;
	upstreamChanges: UpstreamChange[];
}

const UPSTREAM_ENTRY = /^([A-Z]\d*) ([0-9a-f]{12}>[0-9a-f]{12}) (.+)$/;

export function formatUpstreamEntry(change: UpstreamChange): string {
	return `${change.status} ${change.blobs} ${change.path}`;
}

export function parseUpstreamEntry(entry: string, label: string): UpstreamChange {
	const m = entry.match(UPSTREAM_ENTRY);
	if (!m) throw new Error(`${label}: malformed upstream_changes entry '${entry.slice(0, 80)}'`);
	return { status: m[1], blobs: m[2], path: m[3] };
}

/** Compute the per-path upstream-change manifest from `git diff --raw base..target` output. */
export function computeUpstreamChanges(rawText: string): UpstreamChange[] {
	const out = parseRawDiffChanges(rawText);
	out.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
	return out;

	function parseRawDiffChanges(text: string): UpstreamChange[] {
		return parseRawDiff(text).map(change => ({
			status: change.status,
			blobs: `${change.oldSha.slice(0, 12)}>${change.newSha.slice(0, 12)}`,
			path: change.path,
		}));
	}
}

export function parseRecord(text: string, label: string): GuardrailRecord {
	let raw: unknown;
	try {
		raw = JSON.parse(text);
	} catch (err) {
		throw new Error(`${label}: invalid JSON — ${err instanceof Error ? err.message : err}`);
	}
	if (typeof raw !== "object" || raw === null) throw new Error(`${label}: not a JSON object`);
	const record = raw as Record<string, unknown>;
	const str = (key: string): string => {
		const value = record[key];
		if (typeof value !== "string" || !value.trim()) throw new Error(`${label}: missing or empty ${key}`);
		return value;
	};
	for (const key of ["base", "fork", "target"]) {
		if (!/^[0-9a-f]{40}$/.test(str(key))) throw new Error(`${label}: ${key} must be a full 40-hex commit`);
	}
	for (const key of ["version_min", "version_max", "upstream_version"]) {
		if (!/^\d+\.\d+\.\d+$/.test(str(key))) throw new Error(`${label}: ${key} must be a final x.y.z version`);
	}
	const rawChanges = record.upstream_changes;
	if (!Array.isArray(rawChanges)) {
		throw new Error(`${label}: missing upstream_changes manifest — freeze it with --write-sources`);
	}
	const upstreamChanges = rawChanges.map((entry, i) => {
		if (typeof entry !== "string") throw new Error(`${label}: upstream_changes[${i}] is not a string`);
		return parseUpstreamEntry(entry, label);
	});
	return {
		upstreamRepo: str("upstream_repo"),
		upstreamVersion: str("upstream_version"),
		base: str("base"),
		fork: str("fork"),
		target: str("target"),
		versionMin: str("version_min"),
		versionMax: str("version_max"),
		sources: str("sources"),
		matrix: str("matrix"),
		changelog: str("changelog"),
		handoff: str("handoff"),
		upstreamChanges,
	};
}

/** Parse `git merge-tree --write-tree --no-messages` output into conflicted paths. */
export function parseMergeTreeConflicts(output: string): Set<string> {
	const paths = new Set<string>();
	// First line is the written tree OID; the rest is the conflicted file info,
	// one `<mode> <object> <stage>\t<filename>` line per conflicted stage.
	for (const line of output.split("\n").filter(Boolean).slice(1)) {
		const m = line.match(/^\d{6} [0-9a-f]{40} [1-3]\t(.+)$/);
		paths.add(unquoteGitPath(m ? m[1] : line));
	}
	return paths;
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

interface Args {
	record: string;
	report?: string;
	writeSources: boolean;
	allowPending: boolean;
}

export function parseArgs(argv: string[]): Args {
	const flags = new Map<string, string>();
	let writeSources = false;
	let allowPending = false;
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--write-sources") {
			writeSources = true;
		} else if (arg === "--allow-pending") {
			allowPending = true;
		} else if (arg === "--record" || arg === "--report") {
			const value = argv[++i];
			if (value === undefined) throw new Error(`missing value for ${arg}`);
			flags.set(arg.slice(2), value);
		} else {
			throw new Error(`unexpected argument ${arg}`);
		}
	}
	const record = flags.get("record");
	if (!record) throw new Error("missing --record");
	return { record, report: flags.get("report"), writeSources, allowPending };
}

async function git(args: string[], okExitCodes: number[] = [0]): Promise<string> {
	const proc = Bun.spawn(["git", ...args], { stdout: "pipe", stderr: "pipe" });
	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	if (!okExitCodes.includes(exitCode)) throw new Error(`git ${args.join(" ")} failed: ${stderr.trim()}`);
	return stdout;
}

async function main(): Promise<void> {
	let args: Args;
	try {
		args = parseArgs(process.argv.slice(2));
	} catch (err) {
		console.error(`usage error: ${err instanceof Error ? err.message : err}`);
		process.exit(2);
	}

	const readFile = async (path: string, label: string): Promise<string> => {
		const file = Bun.file(path);
		if (!(await file.exists())) {
			console.error(`ERROR: ${label} file missing: ${path}`);
			process.exit(1);
		}
		return file.text();
	};

	const recordText = await readFile(args.record, "record");
	let pins: GuardrailRecord;
	try {
		if (args.writeSources) {
			// The freeze may create the manifest for the first time; tolerate its
			// absence here — parseRecord enforces it on every verification run.
			const raw = JSON.parse(recordText) as Record<string, unknown>;
			if (!Array.isArray(raw.upstream_changes)) raw.upstream_changes = [];
			pins = parseRecord(JSON.stringify(raw), args.record);
		} else {
			pins = parseRecord(recordText, args.record);
		}
	} catch (err) {
		console.error(`ERROR: ${err instanceof Error ? err.message : err}`);
		process.exit(1);
	}
	const range = { min: pins.versionMin, max: pins.versionMax };

	const diffRange = `${pins.base}..${pins.fork}`;
	const targetRange = `${pins.base}..${pins.target}`;
	const [rawText, numstatText, diffText, forkNames, targetRawText, mergeTreeText] = await Promise.all([
		git(["diff", "--raw", "--no-renames", "--abbrev=40", "--no-color", diffRange]),
		git(["diff", "--numstat", "--no-renames", "--no-color", diffRange]),
		git(["diff", "--unified=0", "--no-renames", "--no-color", diffRange]),
		git(["diff", "--name-only", "--no-renames", "--no-color", diffRange]),
		git(["diff", "--raw", "--no-renames", "--abbrev=40", "--no-color", targetRange]),
		// Explicit --merge-base: the pinned base commit removes any dependency on
		// history connectivity, so depth-1 fetches of the three pins suffice (CI).
		git(["merge-tree", "--write-tree", "--no-messages", "--merge-base", pins.base, pins.fork, pins.target], [0, 1]),
	]);
	const computedSources = computeSourceRecords(rawText, numstatText, diffText);
	const computedUpstream = computeUpstreamChanges(targetRawText);

	if (args.writeSources) {
		await Bun.write(pins.sources, formatSourcesTsv(computedSources));
		const raw = JSON.parse(recordText) as Record<string, unknown>;
		raw.upstream_changes = computedUpstream.map(formatUpstreamEntry);
		await Bun.write(args.record, `${JSON.stringify(raw, null, "\t")}\n`);
		console.log(
			`wrote ${computedSources.length} source records to ${pins.sources} and ${computedUpstream.length} upstream-change entries to ${args.record}`,
		);
		return;
	}

	const forkPaths = new Set(forkNames.split("\n").filter(Boolean).map(unquoteGitPath));
	const targetPaths = new Set(computedUpstream.map(c => c.path));
	const sharedPaths = new Set([...forkPaths].filter(p => targetPaths.has(p)));
	const conflictPaths = parseMergeTreeConflicts(mergeTreeText);

	const changelogPaths = (await git(["ls-tree", "-r", "--name-only", pins.target]))
		.split("\n")
		.filter(p => /^packages\/[^/]+\/CHANGELOG\.md$/.test(p));
	const derivedEntries: DerivedEntry[] = [];
	for (const clPath of changelogPaths) {
		const pkg = clPath.split("/")[1];
		derivedEntries.push(...deriveChangelogEntries(pkg, await git(["show", `${pins.target}:${clPath}`]), range));
	}

	let errors: string[];
	try {
		errors = validate({
			frozenSources: parseSourcesTsv(await readFile(pins.sources, "sources")),
			computedSources,
			matrix: parseMatrixTsv(await readFile(pins.matrix, "matrix")),
			changelogRows: parseChangelogTsv(await readFile(pins.changelog, "changelog")),
			derivedEntries,
			forkPaths,
			sharedPaths,
			conflictPaths,
			frozenUpstream: pins.upstreamChanges,
			computedUpstream,
			handoffText: await readFile(pins.handoff, "handoff"),
			allowPending: args.allowPending,
		});
	} catch (err) {
		console.error(`ERROR: ${err instanceof Error ? err.message : err}`);
		process.exit(1);
	}

	if (errors.length) {
		const reportText = buildIncompatibilityReport(errors);
		console.error(reportText);
		if (args.report) await Bun.write(args.report, reportText);
		console.error(
			`FAIL: ${errors.length} error(s) — sources=${computedSources.length} shared=${sharedPaths.size} conflicts=${conflictPaths.size} entries=${derivedEntries.length} upstreamPaths=${computedUpstream.length}`,
		);
		process.exit(1);
	}
	if (args.report)
		await Bun.write(args.report, "# Incompatibility report\n\nNo findings; acceptance is not blocked.\n");
	console.log(
		`PASS: sources=${computedSources.length} forkPaths=${forkPaths.size} shared=${sharedPaths.size} changelogEntries=${derivedEntries.length} upstreamPaths=${computedUpstream.length}${args.allowPending ? " (pending allowed)" : ""}`,
	);
}

if (import.meta.main) {
	await main();
}
