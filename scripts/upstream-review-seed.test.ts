import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import {
	applyHandoffIndex,
	buildChangelogRows,
	buildHandoffIndex,
	findVersionMin,
	parseArgs,
	seedReview,
	settleReview,
	TargetNotContainedError,
} from "./upstream-review-seed.ts";
import {
	type ChangelogRow,
	formatChangelogTsv,
	formatMatrixTsv,
	type MatrixRow,
	parseChangelogTsv,
	parseMatrixTsv,
	parseRecord,
	parseSourcesTsv,
} from "./verify-upstream-handoff.ts";

const dirs: string[] = [];
const GIT_ENV = {
	...process.env,
	GIT_CONFIG_GLOBAL: "/dev/null",
	GIT_CONFIG_SYSTEM: "/dev/null",
	GIT_AUTHOR_NAME: "test",
	GIT_AUTHOR_EMAIL: "test@example.com",
	GIT_COMMITTER_NAME: "test",
	GIT_COMMITTER_EMAIL: "test@example.com",
};

async function run(dir: string, args: string[]): Promise<{ exitCode: number; out: string; err: string }> {
	const proc = await $`git ${args}`.cwd(dir).quiet().nothrow().env(GIT_ENV);
	return { exitCode: proc.exitCode, out: proc.text(), err: proc.stderr.toString() };
}

async function ok(dir: string, args: string[]): Promise<string> {
	const result = await run(dir, args);
	if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${result.err}`);
	return result.out;
}

async function commitAll(dir: string, subject: string): Promise<string> {
	await ok(dir, ["add", "-A"]);
	await ok(dir, ["commit", "-m", subject]);
	return (await ok(dir, ["rev-parse", "HEAD"])).trim();
}

function indexIds(handoff: string): string[] {
	const begin = "<!-- seed:index:begin -->";
	const end = "<!-- seed:index:end -->";
	const start = handoff.indexOf(begin);
	const stop = handoff.indexOf(end, start + begin.length);
	expect(start).toBeGreaterThanOrEqual(0);
	expect(stop).toBeGreaterThan(start);
	const lines = handoff
		.slice(start + begin.length, stop)
		.split("\n")
		.filter(line => line.length > 0);
	expect(lines.every(line => line.startsWith("- "))).toBe(true);
	return lines.map(line => line.slice(2));
}

afterEach(async () => {
	while (dirs.length) {
		const dir = dirs.pop();
		if (dir) await fs.rm(dir, { recursive: true, force: true });
	}
});

describe("formatMatrixTsv and formatChangelogTsv exports", () => {
	test("formatMatrixTsv round-trips through parseMatrixTsv", () => {
		const rows: MatrixRow[] = [
			{
				surfaceId: "path/to/a.txt",
				path: "path/to/a.txt",
				scope: "shared",
				sourceIds: ["s111111111111", "s222222222222"],
				forkBehavior: "behavior for a",
				upstreamChange: "upstream change for a",
				classification: "retained",
				resolution: "auto-merged",
				proof: "pending:gate 1",
			},
			{
				surfaceId: "fork-only.txt",
				path: "fork-only.txt",
				scope: "fork-only",
				sourceIds: ["s333333333333"],
				forkBehavior: "fork-only behavior",
				upstreamChange: "none (fork-only path)",
				classification: "retained",
				resolution: "carried unchanged",
				proof: "pending:git diff",
			},
		];

		const tsv = formatMatrixTsv(rows);
		const parsed = parseMatrixTsv(tsv);
		expect(parsed).toEqual(rows);
	});

	test("formatChangelogTsv round-trips through parseChangelogTsv", () => {
		const rows: ChangelogRow[] = [
			{
				id: "pkg@1.0.1:added:1",
				pkg: "pkg",
				version: "1.0.1",
				section: "Added",
				text: "some feature added",
				disposition: "adopted",
				proof: "gate pass",
			},
		];

		const tsv = formatChangelogTsv(rows);
		const parsed = parseChangelogTsv(tsv);
		expect(parsed).toEqual(rows);
	});

	test("formatChangelogTsv([]) writes header only", () => {
		const tsv = formatChangelogTsv([]);
		expect(tsv).toBe("entry_id\tpackage\tversion\tsection\ttext\tdisposition\tproof\n");
	});
});

describe("changelog ledger and handoff index", () => {
	test("buildChangelogRows keeps an existing entry_id row and drops rows that are not derived", () => {
		const kept: ChangelogRow = {
			id: "x@1.0.1:added:1",
			pkg: "x",
			version: "1.0.1",
			section: "Added",
			text: "hand edited text",
			disposition: "not-applicable",
			proof: "pending:custom",
		};
		const rows = buildChangelogRows(
			[
				{ id: "x@1.0.1:added:1", pkg: "x", version: "1.0.1", section: "Added", text: "fresh" },
				{ id: "x@1.0.1:breaking:1", pkg: "x", version: "1.0.1", section: "Breaking Changes", text: "break" },
				{ id: "x@1.0.1:removed:1", pkg: "x", version: "1.0.1", section: "Removed", text: "gone" },
			],
			[
				kept,
				{
					id: "x@0.9.0:added:1",
					pkg: "x",
					version: "0.9.0",
					section: "Added",
					text: "stale",
					disposition: "adopted",
					proof: "old",
				},
			],
		);
		expect(rows).toEqual([
			kept,
			{
				id: "x@1.0.1:breaking:1",
				pkg: "x",
				version: "1.0.1",
				section: "Breaking Changes",
				text: "break",
				disposition: "re-fitted",
				proof: "pending:decide re-fitted or not-applicable",
			},
			{
				id: "x@1.0.1:removed:1",
				pkg: "x",
				version: "1.0.1",
				section: "Removed",
				text: "gone",
				disposition: "re-fitted",
				proof: "pending:decide re-fitted or not-applicable",
			},
		]);
	});

	test("buildHandoffIndex sorts every surface, source, and changelog id", () => {
		expect(buildHandoffIndex(["f.txt", "a.txt"], ["sfff", "s000"], ["x@1.0.1:breaking:1", "m@1.0.1:added:1"])).toBe(
			["- a.txt", "- f.txt", "- m@1.0.1:added:1", "- s000", "- sfff", "- x@1.0.1:breaking:1"].join("\n"),
		);
	});

	test("applyHandoffIndex appends missing markers and keeps text outside them", () => {
		const body = buildHandoffIndex(["b.txt"], ["s1"], ["x@1.0.1:added:1"]);
		const appended = applyHandoffIndex("Hand-written review note.\n", body);
		expect(appended.startsWith("Hand-written review note.\n")).toBe(true);
		expect(appended).toContain("<!-- seed:index:begin -->");
		expect(appended).toContain("<!-- seed:index:end -->");
		expect(applyHandoffIndex(appended, body)).toBe(appended);

		const withBelow = appended.replace("<!-- seed:index:end -->\n", "<!-- seed:index:end -->\n\nNotes below.\n");
		const replaced = applyHandoffIndex(withBelow, buildHandoffIndex(["c.txt"], [], []));
		expect(replaced.startsWith("Hand-written review note.\n")).toBe(true);
		expect(replaced.endsWith("<!-- seed:index:end -->\n\nNotes below.\n")).toBe(true);
		expect(replaced).toContain("- c.txt\n");
		expect(replaced).not.toContain("b.txt");
		expect(applyHandoffIndex(replaced, buildHandoffIndex(["c.txt"], [], []))).toBe(replaced);
	});
});

describe("parseArgs", () => {
	test("parses valid CLI options", () => {
		const args = parseArgs([
			"--target",
			"abc12345",
			"--version",
			"18.0.7",
			"--fork",
			"fork-branch",
			"--dir",
			"custom-docs",
		]);
		expect(args.target).toBe("abc12345");
		expect(args.version).toBe("18.0.7");
		expect(args.fork).toBe("fork-branch");
		expect(args.dir).toBe("custom-docs");
	});

	test("rejects missing --target", () => {
		expect(() => parseArgs(["--version", "18.0.7"])).toThrow(/missing required --target/);
	});

	test("rejects missing --version", () => {
		expect(() => parseArgs(["--target", "abc12345"])).toThrow(/missing required --version/);
	});

	test("rejects invalid --version format", () => {
		expect(() => parseArgs(["--target", "abc12345", "--version", "v18.0.7"])).toThrow(/X.Y.Z format/);
	});

	test("rejects unexpected arguments", () => {
		expect(() => parseArgs(["--target", "abc12345", "--version", "18.0.7", "--unknown"])).toThrow(
			/unexpected argument --unknown/,
		);
	});

	test("parses valid settle CLI options", () => {
		const args = parseArgs(["--record", "docs/rec.json", "--settle", "--gates-passed-at", "1234567890ab"]);
		expect(args).toEqual({
			settle: true,
			record: "docs/rec.json",
			gatesPassedAt: "1234567890ab",
			fork: "HEAD",
			dir: "docs",
		});
	});

	test("rejects --settle missing --record", () => {
		expect(() => parseArgs(["--settle", "--gates-passed-at", "1234567890ab"])).toThrow(/missing required --record/);
	});

	test("rejects --settle missing --gates-passed-at", () => {
		expect(() => parseArgs(["--settle", "--record", "docs/rec.json"])).toThrow(/missing required --gates-passed-at/);
	});

	test("rejects --record without --settle", () => {
		expect(() => parseArgs(["--record", "docs/rec.json"])).toThrow(/--record requires --settle/);
	});

	test("rejects --gates-passed-at without --settle", () => {
		expect(() => parseArgs(["--gates-passed-at", "1234567890ab"])).toThrow(/--gates-passed-at requires --settle/);
	});

	test("rejects combining --target or --version with --settle", () => {
		expect(() => parseArgs(["--record", "r.json", "--settle", "--gates-passed-at", "c1", "--target", "t1"])).toThrow(
			/--target cannot be combined with --settle/,
		);
		expect(() =>
			parseArgs(["--record", "r.json", "--settle", "--gates-passed-at", "c1", "--version", "1.0.0"]),
		).toThrow(/--version cannot be combined with --settle/);
	});
});

describe("findVersionMin", () => {
	test("finds lowest ## [x.y.z] above baselineVersion across changelogs", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-version-min-test-"));
		dirs.push(dir);
		await ok(dir, ["init", "-b", "main"]);

		await Bun.write(
			path.join(dir, "packages", "pkg-a", "CHANGELOG.md"),
			["# Changelog", "", "## [1.0.3]", "", "## [1.0.1]", "", "## [0.9.0]", ""].join("\n"),
		);
		await Bun.write(
			path.join(dir, "packages", "pkg-b", "CHANGELOG.md"),
			["# Changelog", "", "## [1.0.2]", "", "## [1.0.1]", ""].join("\n"),
		);
		const targetSha = await commitAll(dir, "target commit with changelogs");

		const gitRunner = (args: string[]) => ok(dir, args);
		const vMin = await findVersionMin(targetSha, "1.0.0", "1.0.3", gitRunner);
		expect(vMin).toBe("1.0.1");
	});

	test("falls back to versionMax when no versions are above baselineVersion", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-version-min-test-"));
		dirs.push(dir);
		await ok(dir, ["init", "-b", "main"]);

		await Bun.write(path.join(dir, "readme.txt"), "no changelogs here\n");
		const targetSha = await commitAll(dir, "commit without changelogs");

		const gitRunner = (args: string[]) => ok(dir, args);
		const vMin = await findVersionMin(targetSha, "1.0.0", "2.0.0", gitRunner);
		expect(vMin).toBe("2.0.0");
	});
});

async function createFixtureRepo(): Promise<{
	dir: string;
	baseSha: string;
	targetSha: string;
	forkSha: string;
}> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-review-seed-test-"));
	dirs.push(dir);
	await ok(dir, ["init", "-b", "main"]);

	// Base commit (upstream baseline):
	// - a.txt: 3 lines
	// - b.txt: 2 lines
	// - c.txt: 10 lines (for clean 3-way merge)
	await Bun.write(path.join(dir, "a.txt"), "line 1\nline 2\nline 3\n");
	await Bun.write(path.join(dir, "b.txt"), "b line 1\nb line 2\n");
	await Bun.write(path.join(dir, "c.txt"), Array.from({ length: 10 }, (_, i) => `line ${i + 1}\n`).join(""));
	const baseSha = await commitAll(dir, "upstream baseline commit");

	// Target branch:
	// - a.txt: edit line 1 (conflicting with fork)
	// - b.txt: edit line 2 (target-only change)
	// - c.txt: edit line 10 (merges cleanly with fork editing line 1)
	// - packages/core/CHANGELOG.md: adds ## [1.0.1]
	await ok(dir, ["checkout", "-b", "target", baseSha]);
	await Bun.write(path.join(dir, "a.txt"), "line 1 TARGET EDIT\nline 2\nline 3\n");
	await Bun.write(path.join(dir, "b.txt"), "b line 1\nb line 2 TARGET EDIT\n");
	const cLinesTarget = Array.from({ length: 10 }, (_, i) => `line ${i + 1}\n`);
	cLinesTarget[9] = "line 10 TARGET EDIT\n";
	await Bun.write(path.join(dir, "c.txt"), cLinesTarget.join(""));
	await Bun.write(
		path.join(dir, "packages", "core", "CHANGELOG.md"),
		["# Changelog", "", "## [1.0.1]", "", "- Update core module", ""].join("\n"),
	);
	await Bun.write(
		path.join(dir, "packages", "x", "CHANGELOG.md"),
		[
			"# Changelog",
			"",
			"## [1.0.1]",
			"",
			"### Added",
			"",
			"- Ship the x widget",
			"",
			"### Breaking Changes",
			"",
			"- Drop the x legacy flag",
			"",
			"## [0.9.0]",
			"",
			"### Added",
			"",
			"- Old x helper below the range",
			"",
		].join("\n"),
	);
	const targetSha = await commitAll(dir, "target release commit");

	// Fork branch (branched from baseSha):
	// - a.txt: edit line 1 (conflicting with target)
	// - c.txt: edit line 1 (clean with target's line 10)
	// - f.txt: new fork-only file
	await ok(dir, ["checkout", "-b", "fork", baseSha]);
	await Bun.write(path.join(dir, "a.txt"), "line 1 FORK EDIT\nline 2\nline 3\n");
	const cLinesFork = Array.from({ length: 10 }, (_, i) => `line ${i + 1}\n`);
	cLinesFork[0] = "line 1 FORK EDIT\n";
	await Bun.write(path.join(dir, "c.txt"), cLinesFork.join(""));
	await Bun.write(path.join(dir, "f.txt"), "fork only file content\n");
	const forkSha = await commitAll(dir, "fork changes");

	// Working tree files: baseline record and inventory
	await Bun.write(
		path.join(dir, "docs", "upstream-baseline.json"),
		`${JSON.stringify(
			{
				upstream_repo: "https://github.com/can1357/oh-my-pi",
				upstream_version: "1.0.0",
				target: baseSha,
				version_max: "1.0.0",
			},
			null,
			"\t",
		)}\n`,
	);
	await Bun.write(
		path.join(dir, "docs", "upstream-fork-inventory.tsv"),
		[
			"path\tscope\tstate\thead_blob\tbehavior\tclassification",
			"a.txt\tshared\tmodified\t111111111111\ta.txt fork patch description\tretained",
			"c.txt\tshared\tmodified\t222222222222\tc.txt fork patch description\tre-fitted",
			"f.txt\tfork-only\tadded\t333333333333\tf.txt fork patch description\tretained",
			"",
		].join("\n"),
	);

	return { dir, baseSha, targetSha, forkSha };
}

describe("upstream-review-seed end-to-end fixture", () => {
	test("seeds review record and matrix with all three row kinds", async () => {
		const { dir, baseSha, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);
		const fork12 = forkSha.slice(0, 12);

		const result = await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		expect(result.targetSha).toBe(targetSha);
		expect(result.forkSha).toBe(forkSha);
		expect(result.baseSha).toBe(baseSha);
		expect(result.versionMin).toBe("1.0.1");
		expect(result.versionMax).toBe("1.0.2");

		// Check companions created
		const recordPath = path.join(dir, `docs/upstream-review-${target12}.json`);
		const sourcesPath = path.join(dir, `docs/upstream-review-${target12}-sources.tsv`);
		const matrixPath = path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`);
		const changelogPath = path.join(dir, `docs/upstream-review-${target12}-changelog.tsv`);
		const handoffPath = path.join(dir, `docs/upstream-review-${target12}-handoff.md`);

		expect(await Bun.file(recordPath).exists()).toBe(true);
		expect(await Bun.file(sourcesPath).exists()).toBe(true);
		expect(await Bun.file(matrixPath).exists()).toBe(true);
		expect(await Bun.file(changelogPath).exists()).toBe(true);
		expect(await Bun.file(handoffPath).exists()).toBe(true);

		const changelogRows = parseChangelogTsv(await Bun.file(changelogPath).text());
		expect(changelogRows.map(row => row.id)).toEqual(["x@1.0.1:added:1", "x@1.0.1:breaking:1"]);
		expect(changelogRows[0]).toMatchObject({
			pkg: "x",
			version: "1.0.1",
			section: "Added",
			text: "Ship the x widget",
			disposition: "adopted",
			proof: "pending:session-system/update.sh gates 3-12",
		});
		expect(changelogRows[1]).toMatchObject({
			section: "Breaking Changes",
			text: "Drop the x legacy flag",
			disposition: "re-fitted",
			proof: "pending:decide re-fitted or not-applicable",
		});
		expect(await Bun.file(changelogPath).text()).not.toContain("0.9.0");
		expect(await Bun.file(changelogPath).text()).not.toContain("Old x helper below the range");

		// Verify record JSON pins
		const record = parseRecord(await Bun.file(recordPath).text(), recordPath);
		expect(record.upstreamRepo).toBe("https://github.com/can1357/oh-my-pi");
		expect(record.upstreamVersion).toBe("1.0.2");
		expect(record.base).toBe(baseSha);
		expect(record.fork).toBe(forkSha);
		expect(record.target).toBe(targetSha);
		expect(record.versionMin).toBe("1.0.1");
		expect(record.versionMax).toBe("1.0.2");
		expect(record.sources).toBe(`docs/upstream-review-${target12}-sources.tsv`);
		expect(record.matrix).toBe(`docs/upstream-review-${target12}-matrix.tsv`);
		expect(record.changelog).toBe(`docs/upstream-review-${target12}-changelog.tsv`);
		expect(record.handoff).toBe(`docs/upstream-review-${target12}-handoff.md`);

		// upstream_changes includes b.txt (target-only edit)
		const upstreamPaths = record.upstreamChanges.map(c => c.path);
		expect(upstreamPaths).toContain("a.txt");
		expect(upstreamPaths).toContain("b.txt");
		expect(upstreamPaths).toContain("c.txt");
		expect(upstreamPaths).toContain("packages/core/CHANGELOG.md");
		expect(upstreamPaths).not.toContain("f.txt");

		// Matrix rows: all 3 row kinds
		const matrixRows = parseMatrixTsv(await Bun.file(matrixPath).text());
		expect(matrixRows.length).toBe(3);

		// Kind 1: conflicted shared row (a.txt)
		const aRow = matrixRows.find(r => r.path === "a.txt");
		expect(aRow).toBeDefined();
		expect(aRow?.scope).toBe("shared");
		expect(aRow?.forkBehavior).toBe("a.txt fork patch description");
		expect(aRow?.upstreamChange).toBe("upstream changed; conflict predicted");
		expect(aRow?.classification).toBe("retained");
		expect(aRow?.resolution).toBe("resolve during integration");
		expect(aRow?.proof).toBe("pending:resolve and name the focused test");

		// Kind 2: cleanly merged shared row (c.txt)
		const cRow = matrixRows.find(r => r.path === "c.txt");
		expect(cRow).toBeDefined();
		expect(cRow?.scope).toBe("shared");
		expect(cRow?.forkBehavior).toBe("c.txt fork patch description");
		expect(cRow?.upstreamChange).toBe("upstream changed; merges cleanly");
		expect(cRow?.classification).toBe("re-fitted");
		expect(cRow?.resolution).toBe("auto-merged");
		expect(cRow?.proof).toBe("pending:session-system/update.sh gates 3-12");

		// Kind 3: fork-only row (f.txt)
		const fRow = matrixRows.find(r => r.path === "f.txt");
		expect(fRow).toBeDefined();
		expect(fRow?.scope).toBe("fork-only");
		expect(fRow?.forkBehavior).toBe("f.txt fork patch description");
		expect(fRow?.upstreamChange).toBe("none (fork-only path)");
		expect(fRow?.classification).toBe("retained");
		expect(fRow?.resolution).toBe("carried unchanged");
		expect(fRow?.proof).toBe(`pending:git diff --exit-code ${fork12} HEAD -- f.txt`);

		const sources = parseSourcesTsv(await Bun.file(sourcesPath).text());
		const listed = indexIds(await Bun.file(handoffPath).text());
		expect(listed).toEqual(
			[
				...matrixRows.map(row => row.surfaceId),
				...sources.map(source => source.id),
				...changelogRows.map(row => row.id),
			].sort(),
		);
	});

	test("allow-pending verify passes and the strict run lists pending proofs", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);
		const fork12 = forkSha.slice(0, 12);

		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const verify = path.join(import.meta.dir, "verify-upstream-handoff.ts");
		const recordRel = `docs/upstream-review-${target12}.json`;
		const allowed = await $`bun ${verify} --record ${recordRel} --allow-pending`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);
		expect(allowed.exitCode).toBe(0);
		expect(allowed.text()).toContain("PASS");

		const strict = await $`bun ${verify} --record ${recordRel}`.cwd(dir).quiet().nothrow().env(GIT_ENV);
		expect(strict.exitCode).toBe(1);
		const err = strict.stderr.toString();
		expect(err).toContain("pending:session-system/update.sh gates 3-12");
		expect(err).toContain("pending:decide re-fitted or not-applicable");
		expect(err).toContain("pending:resolve and name the focused test");
		expect(err).toContain(`pending:git diff --exit-code ${fork12} HEAD -- f.txt`);
	});

	test("hand edits survive re-seeding and re-seed is byte-identical", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);

		// Seed for the first time
		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const matrixPath = path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`);
		const initialMatrixText = await Bun.file(matrixPath).text();
		const initialRows = parseMatrixTsv(initialMatrixText);

		// Make hand edits to human columns of a.txt
		const editedRows = initialRows.map(row => {
			if (row.path === "a.txt") {
				return {
					...row,
					forkBehavior: "custom hand-edited behavior for a",
					upstreamChange: "custom upstream change note",
					classification: "re-fitted",
					resolution: "custom manual resolution",
					proof: "pending:custom test proof",
				};
			}
			return row;
		});

		const handEditedMatrixText = formatMatrixTsv(editedRows);
		await Bun.write(matrixPath, handEditedMatrixText);

		// Re-seed
		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const reseededMatrixText = await Bun.file(matrixPath).text();
		expect(reseededMatrixText).toBe(handEditedMatrixText);

		const reseededRows = parseMatrixTsv(reseededMatrixText);
		const reseededA = reseededRows.find(r => r.path === "a.txt");
		expect(reseededA?.forkBehavior).toBe("custom hand-edited behavior for a");
		expect(reseededA?.upstreamChange).toBe("custom upstream change note");
		expect(reseededA?.classification).toBe("re-fitted");
		expect(reseededA?.resolution).toBe("custom manual resolution");
		expect(reseededA?.proof).toBe("pending:custom test proof");

		// Byte-identical re-seed without further edits
		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const finalMatrixText = await Bun.file(matrixPath).text();
		expect(finalMatrixText).toBe(reseededMatrixText);
	});

	test("changelog and handoff hand edits survive a byte-identical re-seed", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);
		const seed = () =>
			seedReview({
				target: targetSha,
				version: "1.0.2",
				fork: forkSha,
				dir: "docs",
				cwd: dir,
			});

		await seed();
		const changelogPath = path.join(dir, `docs/upstream-review-${target12}-changelog.tsv`);
		const handoffPath = path.join(dir, `docs/upstream-review-${target12}-handoff.md`);
		const firstChangelog = await Bun.file(changelogPath).text();
		const firstHandoff = await Bun.file(handoffPath).text();
		await seed();
		expect(await Bun.file(changelogPath).text()).toBe(firstChangelog);
		expect(await Bun.file(handoffPath).text()).toBe(firstHandoff);

		const edited = parseChangelogTsv(firstChangelog).map(row =>
			row.id === "x@1.0.1:breaking:1" ? { ...row, proof: "pending:custom changelog proof" } : row,
		);
		edited.push({
			id: "x@0.9.0:added:1",
			pkg: "x",
			version: "0.9.0",
			section: "Added",
			text: "Old x helper below the range",
			disposition: "adopted",
			proof: "should be dropped",
		});
		await Bun.write(changelogPath, formatChangelogTsv(edited));
		await Bun.write(handoffPath, "Hand-written review note.\n\nStill above the index.\n");

		await seed();
		const afterChangelog = await Bun.file(changelogPath).text();
		const afterRows = parseChangelogTsv(afterChangelog);
		expect(afterRows.map(row => row.id)).toEqual(["x@1.0.1:added:1", "x@1.0.1:breaking:1"]);
		expect(afterRows.find(row => row.id === "x@1.0.1:added:1")?.disposition).toBe("adopted");
		expect(afterRows.find(row => row.id === "x@1.0.1:breaking:1")?.proof).toBe("pending:custom changelog proof");
		expect(afterChangelog).not.toContain("0.9.0");

		const afterHandoff = await Bun.file(handoffPath).text();
		expect(afterHandoff.startsWith("Hand-written review note.\n\nStill above the index.\n")).toBe(true);
		expect(afterHandoff.indexOf("Hand-written review note.")).toBeLessThan(
			afterHandoff.indexOf("<!-- seed:index:begin -->"),
		);
		const listed = indexIds(afterHandoff);
		expect(listed).toContain("x@1.0.1:added:1");
		expect(listed).toContain("x@1.0.1:breaking:1");
		expect(listed).not.toContain("x@0.9.0:added:1");

		const withBelow = afterHandoff.replace(
			"<!-- seed:index:end -->\n",
			"<!-- seed:index:end -->\n\nSettled by hand.\n",
		);
		await Bun.write(handoffPath, withBelow);
		await seed();
		const keptHandoff = await Bun.file(handoffPath).text();
		expect(keptHandoff.startsWith("Hand-written review note.\n\nStill above the index.\n")).toBe(true);
		expect(keptHandoff.endsWith("<!-- seed:index:end -->\n\nSettled by hand.\n")).toBe(true);
		expect(await Bun.file(changelogPath).text()).toBe(afterChangelog);

		await seed();
		expect(await Bun.file(changelogPath).text()).toBe(afterChangelog);
		expect(await Bun.file(handoffPath).text()).toBe(keptHandoff);
	});

	test("CLI execution via bun scripts/upstream-review-seed.ts", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);
		const script = path.join(import.meta.dir, "upstream-review-seed.ts");

		const proc = await $`bun ${script} --target ${targetSha} --version 1.0.2 --fork ${forkSha} --dir docs`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);

		expect(proc.exitCode).toBe(0);
		expect(proc.text()).toContain(`seeded review docs/upstream-review-${target12}.json`);
		expect(await Bun.file(path.join(dir, `docs/upstream-review-${target12}.json`)).exists()).toBe(true);
	});
});

describe("upstream-review-seed rename conflicts", () => {
	// Regression (OMP-401 18.4.8 intake): merge-tree's rename detection reported a conflict
	// on a fork-changed file at upstream's new path, which no matrix row can carry (rows must
	// be fork-changed paths), so the strict verifier could never pass.
	test("an upstream rename of a fork-changed file conflicts at the fork path", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-review-seed-rename-"));
		dirs.push(dir);
		await ok(dir, ["init", "-b", "main"]);
		const lines = Array.from({ length: 12 }, (_, i) => `line ${i + 1}\n`);
		await Bun.write(path.join(dir, "r.txt"), lines.join(""));
		const baseSha = await commitAll(dir, "upstream baseline commit");

		await ok(dir, ["checkout", "-b", "target", baseSha]);
		await ok(dir, ["mv", "r.txt", "moved.txt"]);
		await Bun.write(path.join(dir, "moved.txt"), ["line 1 TARGET EDIT\n", ...lines.slice(1)].join(""));
		const targetSha = await commitAll(dir, "target renames r.txt");

		await ok(dir, ["checkout", "-b", "fork", baseSha]);
		await Bun.write(path.join(dir, "r.txt"), ["line 1 FORK EDIT\n", ...lines.slice(1)].join(""));
		const forkSha = await commitAll(dir, "fork edits r.txt");

		await Bun.write(
			path.join(dir, "docs", "upstream-baseline.json"),
			`${JSON.stringify({ upstream_repo: "https://github.com/can1357/oh-my-pi", upstream_version: "1.0.0", target: baseSha, version_max: "1.0.0" }, null, "\t")}\n`,
		);
		await Bun.write(
			path.join(dir, "docs", "upstream-fork-inventory.tsv"),
			"path\tscope\tstate\thead_blob\tbehavior\tclassification\nr.txt\tshared\tmodified\t111111111111\tr.txt fork patch\tretained\n",
		);

		await seedReview({ target: targetSha, version: "1.0.1", fork: forkSha, dir: "docs", cwd: dir });
		const target12 = targetSha.slice(0, 12);
		const matrixRows = parseMatrixTsv(
			await Bun.file(path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`)).text(),
		);
		expect(matrixRows.find(r => r.path === "r.txt")?.proof).toBe("pending:resolve and name the focused test");

		const verify = path.join(import.meta.dir, "verify-upstream-handoff.ts");
		const recordRel = `docs/upstream-review-${target12}.json`;
		const allowed = await $`bun ${verify} --record ${recordRel} --allow-pending`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);
		expect(allowed.stderr.toString()).not.toContain("has no matrix row");
		expect(allowed.exitCode).toBe(0);
	});

	test("an add/add at an upstream rename destination stays on destination without a source row", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-review-seed-rename-"));
		dirs.push(dir);
		await ok(dir, ["init", "-b", "main"]);
		const lines = Array.from({ length: 12 }, (_, i) => `line ${i + 1}\n`);
		await Bun.write(path.join(dir, "a.txt"), lines.join(""));
		const baseSha = await commitAll(dir, "upstream baseline commit");

		await ok(dir, ["checkout", "-b", "target", baseSha]);
		await ok(dir, ["mv", "a.txt", "b.txt"]);
		const targetSha = await commitAll(dir, "target renames a.txt to b.txt unchanged");

		await ok(dir, ["checkout", "-b", "fork", baseSha]);
		await Bun.write(path.join(dir, "b.txt"), "fork-added b.txt content\n");
		const forkSha = await commitAll(dir, "fork adds b.txt");

		await Bun.write(
			path.join(dir, "docs", "upstream-baseline.json"),
			`${JSON.stringify({ upstream_repo: "https://github.com/can1357/oh-my-pi", upstream_version: "1.0.0", target: baseSha, version_max: "1.0.0" }, null, "\t")}\n`,
		);
		await Bun.write(
			path.join(dir, "docs", "upstream-fork-inventory.tsv"),
			"path\tscope\tstate\thead_blob\tbehavior\tclassification\nb.txt\tshared\tadded\t111111111111\tb.txt fork patch\tretained\n",
		);

		await seedReview({ target: targetSha, version: "1.0.1", fork: forkSha, dir: "docs", cwd: dir });
		const target12 = targetSha.slice(0, 12);
		const matrixRows = parseMatrixTsv(
			await Bun.file(path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`)).text(),
		);
		expect(matrixRows.find(r => r.path === "b.txt")?.proof).toBe("pending:resolve and name the focused test");
		expect(matrixRows.find(r => r.path === "a.txt")).toBeUndefined();

		const verify = path.join(import.meta.dir, "verify-upstream-handoff.ts");
		const recordRel = `docs/upstream-review-${target12}.json`;
		const allowed = await $`bun ${verify} --record ${recordRel} --allow-pending`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);
		expect(allowed.stderr.toString()).not.toContain("has no matrix row");
		expect(allowed.exitCode).toBe(0);
	});
});

describe("upstream-review-seed --settle mode", () => {
	test("a commit without the target exits 2 with record bytes unchanged", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);

		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const recordRel = `docs/upstream-review-${target12}.json`;
		const recordPath = path.join(dir, recordRel);
		const matrixPath = path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`);
		const changelogPath = path.join(dir, `docs/upstream-review-${target12}-changelog.tsv`);
		const handoffPath = path.join(dir, `docs/upstream-review-${target12}-handoff.md`);

		const beforeRecord = await Bun.file(recordPath).text();
		const beforeMatrix = await Bun.file(matrixPath).text();
		const beforeChangelog = await Bun.file(changelogPath).text();
		const beforeHandoff = await Bun.file(handoffPath).text();

		const script = path.join(import.meta.dir, "upstream-review-seed.ts");
		// forkSha does not contain targetSha
		const proc = await $`bun ${script} --record ${recordRel} --settle --gates-passed-at ${forkSha}`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);

		expect(proc.exitCode).toBe(2);
		expect(proc.stderr.toString()).toContain("does not contain target");

		expect(await Bun.file(recordPath).text()).toBe(beforeRecord);
		expect(await Bun.file(matrixPath).text()).toBe(beforeMatrix);
		expect(await Bun.file(changelogPath).text()).toBe(beforeChangelog);
		expect(await Bun.file(handoffPath).text()).toBe(beforeHandoff);
	});

	test("at fixture merge commit, settle rewrites fork-only and gate proofs, leaves conflict and Breaking rows pending, exits 1 listing them; filling rows makes strict verify pass", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);
		const fork12 = forkSha.slice(0, 12);

		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const recordRel = `docs/upstream-review-${target12}.json`;

		// Create merge commit merging target into fork
		await run(dir, ["merge", "--no-ff", targetSha]);
		// a.txt conflicted; resolve it
		await Bun.write(path.join(dir, "a.txt"), "line 1 RESOLVED\nline 2\nline 3\n");
		await run(dir, ["add", "a.txt"]);
		const mergeSha = await commitAll(dir, "merge target into fork");
		const merge12 = mergeSha.slice(0, 12);

		const script = path.join(import.meta.dir, "upstream-review-seed.ts");
		const proc = await $`bun ${script} --record ${recordRel} --settle --gates-passed-at ${mergeSha}`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);

		expect(proc.exitCode).toBe(1);
		const err = proc.stderr.toString();
		expect(err).toContain("matrix a.txt: pending:resolve and name the focused test");
		expect(err).toContain("changelog x@1.0.1:breaking:1: pending:decide re-fitted or not-applicable");
		expect(err).not.toContain("c.txt");
		expect(err).not.toContain("f.txt");
		expect(err).not.toContain("x@1.0.1:added:1");

		const matrixPath = path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`);
		const matrixRows = parseMatrixTsv(await Bun.file(matrixPath).text());
		const aRow = matrixRows.find(r => r.path === "a.txt");
		const cRow = matrixRows.find(r => r.path === "c.txt");
		const fRow = matrixRows.find(r => r.path === "f.txt");

		expect(aRow?.proof).toBe("pending:resolve and name the focused test");
		expect(cRow?.proof).toBe(`session-system/update.sh gates 3-12 passed at ${merge12} (operator-recorded)`);
		expect(fRow?.proof).toBe(`fork-only sweep: git diff ${fork12}..${merge12} -- f.txt is empty`);

		const changelogPath = path.join(dir, `docs/upstream-review-${target12}-changelog.tsv`);
		const changelogRows = parseChangelogTsv(await Bun.file(changelogPath).text());
		const addedRow = changelogRows.find(r => r.id === "x@1.0.1:added:1");
		const breakingRow = changelogRows.find(r => r.id === "x@1.0.1:breaking:1");

		expect(addedRow?.proof).toBe(`session-system/update.sh gates 3-12 passed at ${merge12} (operator-recorded)`);
		expect(breakingRow?.proof).toBe("pending:decide re-fitted or not-applicable");

		// Fill the remaining pending rows
		const filledMatrix = matrixRows.map(r =>
			r.path === "a.txt" ? { ...r, proof: "focused test: test/a.test.ts passes" } : r,
		);
		await Bun.write(matrixPath, formatMatrixTsv(filledMatrix));

		const filledChangelog = changelogRows.map(r =>
			r.id === "x@1.0.1:breaking:1" ? { ...r, proof: "re-fitted: callers migrated to new api" } : r,
		);
		await Bun.write(changelogPath, formatChangelogTsv(filledChangelog));

		// Strict verify passes once rows are filled
		const verifyScript = path.join(import.meta.dir, "verify-upstream-handoff.ts");
		const verifyStrict = await $`bun ${verifyScript} --record ${recordRel}`.cwd(dir).quiet().nothrow().env(GIT_ENV);

		expect(verifyStrict.exitCode).toBe(0);
		expect(verifyStrict.text()).toContain("PASS");

		// Settle now passes with exit code 0
		const settleAgain = await $`bun ${script} --record ${recordRel} --settle --gates-passed-at ${mergeSha}`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);

		expect(settleAgain.exitCode).toBe(0);
		expect(settleAgain.text()).toContain("PASS: review settled");
	});

	test("a fork-only path that changed after the pin stays pending", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);
		const fork12 = forkSha.slice(0, 12);

		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const recordRel = `docs/upstream-review-${target12}.json`;

		await run(dir, ["merge", "--no-ff", targetSha]);
		await Bun.write(path.join(dir, "a.txt"), "line 1 RESOLVED\nline 2\nline 3\n");
		await Bun.write(path.join(dir, "f.txt"), "fork only file modified in merge commit\n");
		await run(dir, ["add", "a.txt", "f.txt"]);
		const mergeSha = await commitAll(dir, "merge target into fork with f.txt changed");

		const script = path.join(import.meta.dir, "upstream-review-seed.ts");
		const proc = await $`bun ${script} --record ${recordRel} --settle --gates-passed-at ${mergeSha}`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);

		expect(proc.exitCode).toBe(1);
		const err = proc.stderr.toString();
		expect(err).toContain(`matrix f.txt: pending:git diff --exit-code ${fork12} HEAD -- f.txt`);

		const matrixPath = path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`);
		const matrixRows = parseMatrixTsv(await Bun.file(matrixPath).text());
		const fRow = matrixRows.find(r => r.path === "f.txt");
		expect(fRow?.proof).toBe(`pending:git diff --exit-code ${fork12} HEAD -- f.txt`);
	});

	test("re-seeding a newer fork leaves an older fork prefix pending, and a non-fork-only row is untouched", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);
		const fork12 = forkSha.slice(0, 12);

		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		await Bun.write(path.join(dir, "note.txt"), "newer fork pin\n");
		await ok(dir, ["add", "note.txt"]);
		await ok(dir, ["commit", "-m", "newer fork pin"]);
		const newerSha = (await ok(dir, ["rev-parse", "HEAD"])).trim();
		const newer12 = newerSha.slice(0, 12);

		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: newerSha,
			dir: "docs",
			cwd: dir,
		});

		const recordRel = `docs/upstream-review-${target12}.json`;
		const record = parseRecord(await Bun.file(path.join(dir, recordRel)).text(), recordRel);
		expect(record.fork).toBe(newerSha);

		const matrixPath = path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`);
		let matrixRows = parseMatrixTsv(await Bun.file(matrixPath).text());
		expect(matrixRows.find(r => r.path === "f.txt")?.proof).toBe(
			`pending:git diff --exit-code ${fork12} HEAD -- f.txt`,
		);
		const notePending = `pending:git diff --exit-code ${newer12} HEAD -- note.txt`;
		expect(matrixRows.find(r => r.path === "note.txt")?.proof).toBe(notePending);
		matrixRows = matrixRows.map(r => (r.path === "note.txt" ? { ...r, scope: "shared" } : r));
		await Bun.write(matrixPath, formatMatrixTsv(matrixRows));

		await run(dir, ["merge", "--no-ff", targetSha]);
		await Bun.write(path.join(dir, "a.txt"), "line 1 RESOLVED\nline 2\nline 3\n");
		await run(dir, ["add", "a.txt"]);
		const mergeSha = await commitAll(dir, "merge target into newer fork");

		const script = path.join(import.meta.dir, "upstream-review-seed.ts");
		const proc = await $`bun ${script} --record ${recordRel} --settle --gates-passed-at ${mergeSha}`
			.cwd(dir)
			.quiet()
			.nothrow()
			.env(GIT_ENV);

		expect(proc.exitCode).toBe(1);
		const err = proc.stderr.toString();
		expect(err).toContain(`matrix f.txt: pending:git diff --exit-code ${fork12} HEAD -- f.txt`);
		expect(err).toContain(`matrix note.txt: ${notePending}`);

		const after = parseMatrixTsv(await Bun.file(matrixPath).text());
		expect(after.find(r => r.path === "f.txt")?.proof).toBe(`pending:git diff --exit-code ${fork12} HEAD -- f.txt`);
		expect(after.find(r => r.path === "note.txt")?.scope).toBe("shared");
		expect(after.find(r => r.path === "note.txt")?.proof).toBe(notePending);
		expect(after.some(r => r.proof.includes(`${fork12}..`))).toBe(false);
	});

	test("settleReview throws TargetNotContainedError when commit does not contain target", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);

		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const recordRel = `docs/upstream-review-${target12}.json`;
		expect(
			settleReview({
				record: recordRel,
				gatesPassedAt: forkSha,
				cwd: dir,
			}),
		).rejects.toThrow(TargetNotContainedError);
	});
});
