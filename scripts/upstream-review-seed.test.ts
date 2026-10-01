import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import { findVersionMin, parseArgs, seedReview } from "./upstream-review-seed.ts";
import {
	type ChangelogRow,
	formatChangelogTsv,
	formatMatrixTsv,
	type MatrixRow,
	parseChangelogTsv,
	parseMatrixTsv,
	parseRecord,
	parseSourcesTsv,
	validate,
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

describe("upstream-review-seed end-to-end fixture", () => {
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

		// Missing changelog is header-only, handoff is empty
		expect(await Bun.file(changelogPath).text()).toBe(
			"entry_id\tpackage\tversion\tsection\ttext\tdisposition\tproof\n",
		);
		expect(await Bun.file(handoffPath).text()).toBe("");

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
	});

	test("--allow-pending verify shows no sources, upstream, matrix or conflict errors", async () => {
		const { dir, targetSha, forkSha } = await createFixtureRepo();
		const target12 = targetSha.slice(0, 12);

		await seedReview({
			target: targetSha,
			version: "1.0.2",
			fork: forkSha,
			dir: "docs",
			cwd: dir,
		});

		const recordPath = path.join(dir, `docs/upstream-review-${target12}.json`);
		const sourcesPath = path.join(dir, `docs/upstream-review-${target12}-sources.tsv`);
		const matrixPath = path.join(dir, `docs/upstream-review-${target12}-matrix.tsv`);
		const changelogPath = path.join(dir, `docs/upstream-review-${target12}-changelog.tsv`);
		const handoffPath = path.join(dir, `docs/upstream-review-${target12}-handoff.md`);

		const record = parseRecord(await Bun.file(recordPath).text(), recordPath);
		const frozenSources = parseSourcesTsv(await Bun.file(sourcesPath).text());
		const matrix = parseMatrixTsv(await Bun.file(matrixPath).text());
		const changelogRows = parseChangelogTsv(await Bun.file(changelogPath).text());
		const handoffText = await Bun.file(handoffPath).text();

		// Run validate() directly with allowPending: true
		const errors = validate({
			frozenSources,
			computedSources: frozenSources,
			matrix,
			changelogRows,
			derivedEntries: [],
			forkPaths: new Set(["a.txt", "c.txt", "f.txt"]),
			sharedPaths: new Set(["a.txt", "c.txt"]),
			conflictPaths: new Set(["a.txt"]),
			frozenUpstream: record.upstreamChanges,
			computedUpstream: record.upstreamChanges,
			handoffText,
			allowPending: true,
		});

		// Only handoff missing links are expected because handoff is written empty
		const nonHandoffErrors = errors.filter(
			e =>
				e.startsWith("sources:") ||
				e.startsWith("upstream:") ||
				e.startsWith("matrix") ||
				e.startsWith("conflict:"),
		);
		expect(nonHandoffErrors).toEqual([]);
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
