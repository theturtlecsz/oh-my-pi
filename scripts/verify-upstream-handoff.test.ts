import { afterAll, describe, expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
	buildIncompatibilityReport,
	type ChangelogRow,
	categorizeError,
	compareVersions,
	computeSourceRecords,
	deriveChangelogEntries,
	formatSourcesTsv,
	type MatrixRow,
	parseArgs,
	parseChangelogTsv,
	parseHunks,
	parseMatrixTsv,
	parseMergeTreeConflicts,
	parseRawDiff,
	parseRecord,
	parseSourcesTsv,
	type SourceRecord,
	sourceId,
	unquoteGitPath,
	type ValidateInput,
	validate,
	versionInRange,
} from "./verify-upstream-handoff.ts";

const RANGE = { min: "17.3.3", max: "18.0.6" };

describe("version range", () => {
	test("includes the inclusive lower boundary", () => {
		expect(versionInRange("17.3.3", RANGE)).toBe(true);
	});
	test("excludes versions below the range", () => {
		expect(versionInRange("17.3.2", RANGE)).toBe(false);
	});
	test("includes the target and interior versions", () => {
		expect(versionInRange("18.0.6", RANGE)).toBe(true);
		expect(versionInRange("17.4.0", RANGE)).toBe(true);
		expect(versionInRange("18.0.0", RANGE)).toBe(true);
	});
	test("excludes versions above the target", () => {
		expect(versionInRange("18.0.7", RANGE)).toBe(false);
		expect(versionInRange("18.1.0", RANGE)).toBe(false);
	});
	test("range is data, not a constant", () => {
		expect(versionInRange("18.1.2", { min: "18.0.7", max: "18.1.2" })).toBe(true);
		expect(versionInRange("18.0.6", { min: "18.0.7", max: "18.1.2" })).toBe(false);
	});
	test("orders multi-digit components numerically", () => {
		expect(compareVersions("17.10.0", "17.9.9")).toBeGreaterThan(0);
		expect(compareVersions("18.0.6", "18.0.6")).toBe(0);
	});
});

describe("deriveChangelogEntries", () => {
	const changelog = `# Changelog

## [Unreleased]

### Added

- Unreleased bullet must not appear.

## [18.0.6] - 2026-08-26

### Added

- First added bullet.
- Second added bullet
  with a continuation line.

### Changed

- Changed bullets are not tracked.

### Breaking Changes

- One breaking change.

## [17.3.3] - 2026-08-14

### Removed

- Removed at the inclusive lower boundary.

## [17.3.2] - 2026-08-13

### Added

- Below-range bullet must not appear.
`;

	test("derives only in-range Added/Breaking/Removed bullets with 1-based indexes", () => {
		const entries = deriveChangelogEntries("coding-agent", changelog, RANGE);
		expect(entries.map(e => e.id)).toEqual([
			"coding-agent@18.0.6:added:1",
			"coding-agent@18.0.6:added:2",
			"coding-agent@18.0.6:breaking:1",
			"coding-agent@17.3.3:removed:1",
		]);
	});
	test("joins continuation lines into the bullet text", () => {
		const entries = deriveChangelogEntries("coding-agent", changelog, RANGE);
		expect(entries[1].text).toBe("Second added bullet with a continuation line.");
	});
	test("keeps section names verbatim", () => {
		const entries = deriveChangelogEntries("coding-agent", changelog, RANGE);
		expect(entries[2].section).toBe("Breaking Changes");
		expect(entries[3].section).toBe("Removed");
	});
	test("a different range selects different versions", () => {
		const entries = deriveChangelogEntries("coding-agent", changelog, { min: "18.0.0", max: "18.0.6" });
		expect(entries.map(e => e.version)).toEqual(["18.0.6", "18.0.6", "18.0.6"]);
	});
});

describe("diff parsing", () => {
	const rawText = [
		":000000 100644 0000000000000000000000000000000000000000 1111111111111111111111111111111111111111 A\tsrc/new.ts",
		":100644 100644 2222222222222222222222222222222222222222 3333333333333333333333333333333333333333 M\tassets/logo.png",
		":100644 000000 4444444444444444444444444444444444444444 0000000000000000000000000000000000000000 D\tsrc/gone.ts",
		"",
	].join("\n");
	const numstatText = ["12\t0\tsrc/new.ts", "-\t-\tassets/logo.png", "0\t9\tsrc/gone.ts", ""].join("\n");
	const diffText = [
		"diff --git a/src/new.ts b/src/new.ts",
		"new file mode 100644",
		"index 0000000..1111111",
		"--- /dev/null",
		"+++ b/src/new.ts",
		"@@ -0,0 +1,2 @@",
		"+line one",
		"+line two",
		"diff --git a/assets/logo.png b/assets/logo.png",
		"index 2222222..3333333 100644",
		"Binary files a/assets/logo.png and b/assets/logo.png differ",
		"diff --git a/src/gone.ts b/src/gone.ts",
		"deleted file mode 100644",
		"index 4444444..0000000",
		"--- a/src/gone.ts",
		"+++ /dev/null",
		"@@ -1,9 +0,0 @@",
		"-old body",
		"",
	].join("\n");

	test("parseRawDiff extracts status and blob shas", () => {
		const changes = parseRawDiff(rawText);
		expect(changes).toHaveLength(3);
		expect(changes[0]).toMatchObject({ status: "A", path: "src/new.ts" });
		expect(changes[2]).toMatchObject({ status: "D", path: "src/gone.ts" });
	});
	test("parseRawDiff handles rename rows", () => {
		const renamed = parseRawDiff(
			":100644 100644 5555555555555555555555555555555555555555 6666666666666666666666666666666666666666 R95\told/name.ts\tnew/name.ts\n",
		);
		expect(renamed[0]).toMatchObject({ status: "R", oldPath: "old/name.ts", path: "new/name.ts" });
	});
	test("parseHunks captures headers and bodies per path", () => {
		const hunks = parseHunks(diffText);
		expect(hunks).toEqual([
			{ path: "src/new.ts", locator: "-0,0 +1,2", body: "+line one\n+line two" },
			{ path: "src/gone.ts", locator: "-1,9 +0,0", body: "-old body" },
		]);
	});
	test("computeSourceRecords emits hunk, binary, and delete records with stable ids", () => {
		const records = computeSourceRecords(rawText, numstatText, diffText);
		const kinds = records.map(r => `${r.kind}:${r.path}`).sort();
		expect(kinds).toEqual(["binary:assets/logo.png", "delete:src/gone.ts", "hunk:src/gone.ts", "hunk:src/new.ts"]);
		for (const record of records) {
			expect(record.id).toBe(sourceId(record.kind, record.path, record.locator, record.bodySha));
		}
		expect(computeSourceRecords(rawText, numstatText, diffText)).toEqual(records);
	});
	test("computeSourceRecords emits a meta record for content-less changes", () => {
		const emptyRaw =
			":000000 100644 0000000000000000000000000000000000000000 e69de29bb2d1d6434b8b29ae775ad8c2e48c5391 A\tpython/omp-work/src/omp_work/py.typed\n";
		const records = computeSourceRecords(emptyRaw, "0\t0\tpython/omp-work/src/omp_work/py.typed\n", "");
		expect(records).toHaveLength(1);
		expect(records[0]).toMatchObject({
			kind: "meta",
			path: "python/omp-work/src/omp_work/py.typed",
			locator: "status:A",
		});
	});
	test("unquoteGitPath decodes C-quoted paths", () => {
		expect(unquoteGitPath('"a b.txt"')).toBe("a b.txt");
		expect(unquoteGitPath("plain/path.ts")).toBe("plain/path.ts");
		expect(unquoteGitPath('"caf\\303\\251.md"')).toBe("café.md");
	});
});

describe("parseMergeTreeConflicts", () => {
	test("extracts unique conflicted paths from conflicted-file-info lines", () => {
		const output = [
			"1234567890123456789012345678901234567890123456789012345678901234",
			"100644 6062e9bd4be864b11a73ad3fc7496e701ee87df1 2\t.config/nextest.toml",
			"100644 e1bbbb17f8d702ff4c963c83c2cde99001e7ab04 3\t.config/nextest.toml",
			"100644 831aa5aa75de36a7ec19088849168e52468033b9 1\tpackages/x/src/a.ts",
			"",
		].join("\n");
		expect([...parseMergeTreeConflicts(output)].sort()).toEqual([".config/nextest.toml", "packages/x/src/a.ts"]);
	});
	test("a clean merge yields no conflicts", () => {
		expect(parseMergeTreeConflicts("1234567890123456789012345678901234567890123456789012345678901234\n").size).toBe(
			0,
		);
	});
});

describe("tsv parsing", () => {
	test("sources round-trips through format/parse", () => {
		const records: SourceRecord[] = [
			{ id: "sabc", path: "a.ts", kind: "hunk", locator: "-1,2 +1,3", bodySha: "deadbeef0000" },
		];
		expect(parseSourcesTsv(formatSourcesTsv(records))).toEqual(records);
	});
	test("rejects a bad header", () => {
		expect(() => parseMatrixTsv("wrong\theader\n")).toThrow(/bad header/);
	});
	test("rejects rows with the wrong field count", () => {
		const header = "entry_id\tpackage\tversion\tsection\ttext\tdisposition\tproof\n";
		expect(() => parseChangelogTsv(`${header}too\tfew\n`)).toThrow(/row 2/);
	});
});

describe("parseRecord", () => {
	const record = {
		upstream_repo: "https://github.com/can1357/oh-my-pi",
		upstream_version: "18.0.6",
		base: "ae2d3d6ea16a47aa5208bd123dcc4cfcc8756472",
		fork: "79b037e420943010e03727d7cdb22f05e64507b7",
		target: "b4e8e856ad40294167679a3f88417c07429fe59b",
		version_min: "17.3.3",
		version_max: "18.0.6",
		sources: "docs/upstream-18.0.6-fork-sources.tsv",
		matrix: "docs/upstream-18.0.6-fork-matrix.tsv",
		changelog: "docs/upstream-18.0.6-changelog.tsv",
		handoff: "docs/upstream-18.0.6-upgrade.md",
	};

	test("parses a complete record", () => {
		const parsed = parseRecord(JSON.stringify(record), "record");
		expect(parsed).toMatchObject({
			base: record.base,
			fork: record.fork,
			target: record.target,
			versionMin: "17.3.3",
			versionMax: "18.0.6",
		});
	});
	test("rejects abbreviated commits", () => {
		expect(() => parseRecord(JSON.stringify({ ...record, target: "b4e8e856ad" }), "record")).toThrow(/40-hex/);
	});
	test("rejects non-final versions", () => {
		expect(() => parseRecord(JSON.stringify({ ...record, version_max: "18.1.0-rc.1" }), "record")).toThrow(
			/final x\.y\.z/,
		);
	});
	test("rejects missing fields and invalid JSON", () => {
		const { handoff: _handoff, ...incomplete } = record;
		expect(() => parseRecord(JSON.stringify(incomplete), "record")).toThrow(/missing or empty handoff/);
		expect(() => parseRecord("not json", "record")).toThrow(/invalid JSON/);
	});
});

// ---------------------------------------------------------------------------
// validate()
// ---------------------------------------------------------------------------

function fixture(): ValidateInput {
	const sources: SourceRecord[] = [
		{ id: "s1", path: "shared.ts", kind: "hunk", locator: "-1,1 +1,2", bodySha: "aaaaaaaaaaaa" },
		{ id: "s2", path: "forkonly.ts", kind: "hunk", locator: "-4,0 +5,3", bodySha: "bbbbbbbbbbbb" },
		{ id: "s3", path: "asset.bin", kind: "binary", locator: "binary", bodySha: "cccccccccccc>dddddddddddd" },
	];
	const matrix: MatrixRow[] = [
		{
			surfaceId: "shared.ts",
			path: "shared.ts",
			scope: "shared",
			sourceIds: ["s1"],
			forkBehavior: "fork keeps X",
			upstreamChange: "upstream reworks X",
			classification: "re-fitted",
			resolution: "merge upstream shape, reapply fork X",
			proof: "bun test shared.test.ts",
		},
		{
			surfaceId: "forkonly.ts",
			path: "forkonly.ts",
			scope: "fork-only",
			sourceIds: ["s2"],
			forkBehavior: "fork-only feature",
			upstreamChange: "none (fork-only path)",
			classification: "retained",
			resolution: "kept verbatim",
			proof: "bun test forkonly.test.ts",
		},
		{
			surfaceId: "asset.bin",
			path: "asset.bin",
			scope: "fork-only",
			sourceIds: ["s3"],
			forkBehavior: "binary asset",
			upstreamChange: "none (fork-only path)",
			classification: "retained",
			resolution: "kept verbatim",
			proof: "sha256 matches frozen manifest",
		},
	];
	const derived = [
		{ id: "pkg@18.0.6:added:1", pkg: "pkg", version: "18.0.6", section: "Added", text: "New thing." },
		{ id: "pkg@18.0.6:breaking:1", pkg: "pkg", version: "18.0.6", section: "Breaking Changes", text: "Broke thing." },
	];
	const changelogRows: ChangelogRow[] = [
		{ ...derived[0], disposition: "adopted", proof: "merged with upstream" },
		{ ...derived[1], disposition: "re-fitted", proof: "fork API preserved via adapter" },
	];
	return {
		frozenSources: sources,
		computedSources: structuredClone(sources),
		matrix,
		changelogRows,
		derivedEntries: derived,
		forkPaths: new Set(["shared.ts", "forkonly.ts", "asset.bin"]),
		sharedPaths: new Set(["shared.ts"]),
		conflictPaths: new Set(["shared.ts"]),
		handoffText: "s1 s2 s3 shared.ts forkonly.ts asset.bin pkg@18.0.6:added:1 pkg@18.0.6:breaking:1",
		allowPending: false,
	};
}

describe("validate", () => {
	test("clean fixture passes", () => {
		expect(validate(fixture())).toEqual([]);
	});
	test("flags source set mismatch in both directions", () => {
		const input = fixture();
		input.computedSources.push({ id: "s9", path: "new.ts", kind: "hunk", locator: "-1 +1", bodySha: "e" });
		const errors = validate(input);
		expect(errors.some(e => e.includes("missing record s9"))).toBe(true);

		const input2 = fixture();
		input2.computedSources = input2.computedSources.filter(r => r.id !== "s3");
		expect(validate(input2).some(e => e.includes("stale record s3"))).toBe(true);
	});
	test("flags changed record content under the same id", () => {
		const input = fixture();
		input.computedSources[0] = { ...input.computedSources[0], bodySha: "ffffffffffff" };
		expect(validate(input).some(e => e.includes("differs from recomputed diff"))).toBe(true);
	});
	test("flags unmapped source ids", () => {
		const input = fixture();
		input.matrix[2].sourceIds = ["s2"];
		expect(validate(input).some(e => e.includes("unmapped source_id s3"))).toBe(true);
	});
	test("flags unknown source ids referenced by the matrix", () => {
		const input = fixture();
		input.matrix[0].sourceIds = ["s1", "sX"];
		expect(validate(input).some(e => e.includes("unknown source_id sX"))).toBe(true);
	});
	test("flags missing changed paths", () => {
		const input = fixture();
		input.matrix = input.matrix.filter(r => r.path !== "forkonly.ts");
		const errors = validate(input);
		expect(errors.some(e => e.includes("changed path missing — forkonly.ts"))).toBe(true);
	});
	test("flags scope contradicting the computed intersection", () => {
		const input = fixture();
		input.matrix[1].scope = "shared";
		const errors = validate(input);
		expect(errors.some(e => e.includes("contradicts computed"))).toBe(true);
		expect(errors.some(e => e.includes("shared outside computed intersection"))).toBe(true);
	});
	test("flags duplicate surface ids", () => {
		const input = fixture();
		input.matrix[1].surfaceId = "shared.ts";
		expect(validate(input).some(e => e.includes("duplicate surface_id"))).toBe(true);
	});
	test("flags empty fields and invalid enums", () => {
		const input = fixture();
		input.matrix[0].resolution = " ";
		input.matrix[1].classification = "kept";
		const errors = validate(input);
		expect(errors.some(e => e.includes("empty resolution"))).toBe(true);
		expect(errors.some(e => e.includes("invalid classification 'kept'"))).toBe(true);
	});
	test("forbids dropped classifications without an owner ruling", () => {
		const input = fixture();
		input.matrix[1].classification = "dropped";
		expect(validate(input).some(e => e.includes("requires an explicit owner-ruling:"))).toBe(true);
	});
	test("accepts dropped classifications carrying an owner-ruling reference", () => {
		const input = fixture();
		input.matrix[1].classification = "dropped";
		input.matrix[1].resolution = "dropped per owner-ruling: OMP-999 (2026-09-01)";
		expect(validate(input)).toEqual([]);
	});
	test("flags predicted conflicts with no matrix row", () => {
		const input = fixture();
		input.conflictPaths = new Set(["shared.ts", "unlisted.ts"]);
		const errors = validate(input);
		expect(errors.some(e => e.includes("conflict: predicted merge conflict in unlisted.ts"))).toBe(true);
		expect(errors.some(e => e.includes("shared.ts has no matrix row"))).toBe(false);
	});
	test("gates pending proofs on --allow-pending", () => {
		const input = fixture();
		input.matrix[0].proof = "pending:bun test shared.test.ts";
		expect(validate(input).some(e => e.includes("unresolved pending proof"))).toBe(true);
		input.allowPending = true;
		expect(validate(input)).toEqual([]);
	});
	test("rejects pending proofs without a concrete command", () => {
		const input = fixture();
		input.allowPending = true;
		input.matrix[0].proof = "pending: ";
		expect(validate(input).some(e => e.includes("must name an exact command"))).toBe(true);
	});
	test("flags changelog rows missing, extra, and text drift", () => {
		const missing = fixture();
		missing.changelogRows = missing.changelogRows.slice(0, 1);
		expect(validate(missing).some(e => e.includes("missing entry pkg@18.0.6:breaking:1"))).toBe(true);

		const extra = fixture();
		extra.changelogRows.push({
			id: "pkg@18.0.5:added:1",
			pkg: "pkg",
			version: "18.0.5",
			section: "Added",
			text: "Ghost.",
			disposition: "adopted",
			proof: "x",
		});
		expect(validate(extra).some(e => e.includes("not derived from pinned-target changelogs"))).toBe(true);

		const drift = fixture();
		drift.changelogRows[0] = { ...drift.changelogRows[0], text: "Edited." };
		expect(validate(drift).some(e => e.includes("text differs from derived entry"))).toBe(true);

		const dupe = fixture();
		dupe.changelogRows.push(dupe.changelogRows[0]);
		expect(validate(dupe).some(e => e.includes("duplicate entry_id"))).toBe(true);
	});
	test("restricts Breaking/Removed dispositions to re-fitted or not-applicable", () => {
		const input = fixture();
		input.changelogRows[1] = { ...input.changelogRows[1], disposition: "adopted" };
		expect(validate(input).some(e => e.includes("invalid for section 'Breaking Changes'"))).toBe(true);
	});
	test("flags handoff links missing for surface, source, and changelog ids", () => {
		const input = fixture();
		input.handoffText = "s1 s2 shared.ts forkonly.ts asset.bin pkg@18.0.6:added:1";
		const errors = validate(input);
		expect(errors.some(e => e.includes("missing source link s3"))).toBe(true);
		expect(errors.some(e => e.includes("missing changelog link pkg@18.0.6:breaking:1"))).toBe(true);
	});
});

// ---------------------------------------------------------------------------
// Incompatibility report
// ---------------------------------------------------------------------------

describe("incompatibility report", () => {
	test("categorizes errors by guardrail dimension", () => {
		expect(categorizeError("sources: missing record s9 (hunk a.ts -1 +1)")).toBe("fork");
		expect(categorizeError("matrix: changed path missing — a.ts")).toBe("fork");
		expect(categorizeError("matrix a.ts: empty resolution")).toBe("fork");
		expect(categorizeError("changelog pkg@18.0.6:added:1: not derived from pinned-target changelogs")).toBe(
			"upstream",
		);
		expect(categorizeError("conflict: predicted merge conflict in a.ts has no matrix row")).toBe("conflict");
		expect(categorizeError("matrix a.ts: unresolved pending proof (pending:bun test)")).toBe("proof");
		expect(categorizeError("handoff: missing source link s3")).toBe("record");
	});
	test("builds an itemized report grouped by category, omitting empty sections", () => {
		const report = buildIncompatibilityReport([
			"matrix: changed path missing — a.ts",
			"changelog pkg@18.0.6:added:1: missing entry pkg@18.0.6:added:1",
			"conflict: predicted merge conflict in b.ts has no matrix row",
		]);
		expect(report).toContain("# Incompatibility report");
		expect(report).toContain("3 finding(s) block acceptance.");
		expect(report).toContain("## Unaccounted fork behavior (1)");
		expect(report).toContain("## Unaccounted upstream changes (1)");
		expect(report).toContain("## Unresolved merge conflicts (1)");
		expect(report).toContain("- conflict: predicted merge conflict in b.ts has no matrix row");
		expect(report).not.toContain("## Failed or pending proofs");
		expect(report).not.toContain("## Record integrity");
	});
});

// ---------------------------------------------------------------------------
// parseArgs
// ---------------------------------------------------------------------------

describe("parseArgs", () => {
	const base = "ae2d3d6ea16a47aa5208bd123dcc4cfcc8756472";
	const fork = "79b037e420943010e03727d7cdb22f05e64507b7";
	const target = "b4e8e856ad40294167679a3f88417c07429fe59b";
	const fixed = [
		"--base",
		base,
		"--fork",
		fork,
		"--target",
		target,
		"--version-min",
		"17.3.3",
		"--version-max",
		"18.0.6",
		"--sources",
		"docs/upstream-18.0.6-fork-sources.tsv",
		"--matrix",
		"docs/upstream-18.0.6-fork-matrix.tsv",
		"--changelog",
		"docs/upstream-18.0.6-changelog.tsv",
		"--handoff",
		"docs/upstream-18.0.6-upgrade.md",
	];

	test("parses the explicit pinned invocation", () => {
		const args = parseArgs(fixed);
		expect(args.pins).toMatchObject({ base, fork, target, versionMin: "17.3.3", versionMax: "18.0.6" });
		expect(args).toMatchObject({ writeSources: false, allowPending: false });
	});
	test("parses the record-driven invocation", () => {
		const args = parseArgs(["--record", "docs/upstream/baseline.json", "--allow-pending"]);
		expect(args.record).toBe("docs/upstream/baseline.json");
		expect(args.pins).toBeUndefined();
		expect(args.allowPending).toBe(true);
	});
	test("record mode excludes explicit pin flags", () => {
		expect(() => parseArgs(["--record", "r.json", "--base", base])).toThrow(/--record excludes --base/);
	});
	test("parses the mode flags", () => {
		expect(parseArgs([...fixed, "--write-sources"]).writeSources).toBe(true);
		expect(parseArgs([...fixed, "--allow-pending"]).allowPending).toBe(true);
		expect(parseArgs([...fixed, "--report", "/tmp/report.md"]).report).toBe("/tmp/report.md");
	});
	test("rejects abbreviated commits", () => {
		const short = [...fixed];
		short[1] = "b4e8e856ad";
		expect(() => parseArgs(short)).toThrow(/40-hex/);
	});
	test("rejects missing required flags", () => {
		expect(() => parseArgs(fixed.slice(0, 6))).toThrow(/missing --/);
	});
});

// ---------------------------------------------------------------------------
// Calibration against the accepted 18.0.6 record (criterion: the accepted
// record passes; a deliberately incomplete record fails). Requires the pinned
// commits locally; skipped on shallow checkouts.
// ---------------------------------------------------------------------------

const PINNED = [
	"ae2d3d6ea16a47aa5208bd123dcc4cfcc8756472",
	"79b037e420943010e03727d7cdb22f05e64507b7",
	"b4e8e856ad40294167679a3f88417c07429fe59b",
];
const repoRoot = join(import.meta.dir, "..");
const havePinnedCommits = PINNED.every(
	sha => spawnSync("git", ["cat-file", "-e", `${sha}^{commit}`], { cwd: repoRoot }).status === 0,
);
const calibrationDirs: string[] = [];

afterAll(() => {
	for (const dir of calibrationDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

describe.if(havePinnedCommits)("18.0.6 calibration", () => {
	test("the accepted baseline record passes", () => {
		const result = spawnSync(
			"bun",
			["scripts/verify-upstream-handoff.ts", "--record", "docs/upstream/baseline.json", "--allow-pending"],
			{ cwd: repoRoot, encoding: "utf8" },
		);
		expect(result.status, result.stderr).toBe(0);
		expect(result.stdout).toContain("PASS: sources=869 forkPaths=378 shared=50 changelogEntries=129");
	}, 30000);

	test("a deliberately incomplete record fails with an itemized report", async () => {
		const dir = mkdtempSync(join(tmpdir(), "omp-guardrail-calib-"));
		calibrationDirs.push(dir);
		const matrixText = await Bun.file(join(repoRoot, "docs/upstream-18.0.6-fork-matrix.tsv")).text();
		const lines = matrixText.split("\n");
		const removed = lines.splice(1, 1)[0];
		const removedPath = removed.split("\t")[0];
		writeFileSync(join(dir, "matrix.tsv"), lines.join("\n"));
		const record = JSON.parse(await Bun.file(join(repoRoot, "docs/upstream/baseline.json")).text());
		record.matrix = join(dir, "matrix.tsv");
		writeFileSync(join(dir, "record.json"), JSON.stringify(record));
		const result = spawnSync(
			"bun",
			["scripts/verify-upstream-handoff.ts", "--record", join(dir, "record.json"), "--allow-pending"],
			{ cwd: repoRoot, encoding: "utf8" },
		);
		expect(result.status).toBe(1);
		expect(result.stderr).toContain("# Incompatibility report");
		expect(result.stderr).toContain("## Unaccounted fork behavior");
		expect(result.stderr).toContain(`changed path missing — ${removedPath}`);
		expect(result.stderr).toContain("FAIL:");
	}, 30000);
});
