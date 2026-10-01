import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import { checkInventory, type DivergingPath, type InventoryRow, parseNumstat } from "./upstream-inventory.ts";

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

describe("checkInventory seed placeholder check", () => {
	const computed: DivergingPath[] = [
		{ path: "shared.txt", scope: "shared", state: "modified", headBlob: "111111111111" },
		{ path: "fork-only.txt", scope: "fork-only", state: "added", headBlob: "222222222222" },
	];

	test("flags seed placeholder on shared rows", () => {
		const rows: InventoryRow[] = [
			{
				path: "shared.txt",
				scope: "shared",
				state: "modified",
				headBlob: "111111111111",
				behavior: "fork change (describe)",
				classification: "retained",
			},
			{
				path: "fork-only.txt",
				scope: "fork-only",
				state: "added",
				headBlob: "222222222222",
				behavior: "custom feature",
				classification: "retained",
			},
		];
		const errors = checkInventory(computed, rows);
		expect(errors).toContain(
			"inventory: shared.txt changes upstream-owned code but its behavior is the seed placeholder — name the fork patch",
		);
	});

	test("flags seed placeholder with appended text on shared rows", () => {
		const rows: InventoryRow[] = [
			{
				path: "shared.txt",
				scope: "shared",
				state: "modified",
				headBlob: "111111111111",
				behavior: "fork change (describe); OMP-246: some fix",
				classification: "retained",
			},
			{
				path: "fork-only.txt",
				scope: "fork-only",
				state: "added",
				headBlob: "222222222222",
				behavior: "custom feature",
				classification: "retained",
			},
		];
		const errors = checkInventory(computed, rows);
		expect(errors).toContain(
			"inventory: shared.txt changes upstream-owned code but its behavior is the seed placeholder — name the fork patch",
		);
	});

	test("accepts seed placeholder on fork-only rows", () => {
		const rows: InventoryRow[] = [
			{
				path: "shared.txt",
				scope: "shared",
				state: "modified",
				headBlob: "111111111111",
				behavior: "named fork patch",
				classification: "retained",
			},
			{
				path: "fork-only.txt",
				scope: "fork-only",
				state: "added",
				headBlob: "222222222222",
				behavior: "fork change (describe)",
				classification: "retained",
			},
		];
		const errors = checkInventory(computed, rows);
		expect(errors).toEqual([]);
	});
});

describe("parseNumstat", () => {
	test("parses added, removed, and path from diff numstat text", () => {
		const text = ["2\t1\tfile-a.txt", "10\t0\tfile-b.txt", "0\t5\tfile-c.txt", ""].join("\n");
		const stats = parseNumstat(text);
		expect(stats.get("file-a.txt")).toEqual({ added: 2, removed: 1 });
		expect(stats.get("file-b.txt")).toEqual({ added: 10, removed: 0 });
		expect(stats.get("file-c.txt")).toEqual({ added: 0, removed: 5 });
	});

	test("handles binary files and unquotes git C-quoted paths", () => {
		const text = ["-\t-\tlogo.png", '3\t2\t"path with spaces/file.txt"', ""].join("\n");
		const stats = parseNumstat(text);
		expect(stats.get("logo.png")).toEqual({ added: 0, removed: 0 });
		expect(stats.get("path with spaces/file.txt")).toEqual({ added: 3, removed: 2 });
	});
});

describe("--patches CLI", () => {
	test("prints shared row counts and total, ignoring fork-only files", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-patches-test-"));
		dirs.push(dir);
		await ok(dir, ["init", "-b", "main"]);

		// Baseline: file with 3 lines
		await Bun.write(path.join(dir, "baseline.txt"), "line1\nline2\nline3\n");
		const base = await commitAll(dir, "base commit");

		// Fork: edit baseline +2/-1 and add fork-only file
		await ok(dir, ["checkout", "-b", "fork"]);
		await Bun.write(path.join(dir, "baseline.txt"), "line1a\nline1b\nline2\nline3\n");
		await Bun.write(path.join(dir, "fork-only.txt"), "new file\n");
		await commitAll(dir, "fork changes");

		await Bun.write(
			path.join(dir, "docs", "upstream-baseline.json"),
			`${JSON.stringify({ upstream_repo: "https://github.com/can1357/oh-my-pi", target: base }, null, "\t")}\n`,
		);
		await Bun.write(
			path.join(dir, "docs", "upstream-fork-inventory.tsv"),
			[
				"path\tscope\tstate\thead_blob\tbehavior\tclassification",
				"baseline.txt\tshared\tmodified\t111111111111\tfork patch description\tretained",
				"fork-only.txt\tfork-only\tadded\t222222222222\tfork-only description\tretained",
				"",
			].join("\n"),
		);

		const script = path.join(import.meta.dir, "upstream-inventory.ts");
		const proc = await $`bun ${script} --patches`.cwd(dir).quiet().nothrow().env(GIT_ENV);

		expect(proc.exitCode).toBe(0);
		const stdout = proc.text();
		expect(stdout).toContain("baseline.txt\tmodified\t+2\t-1\tretained\tfork patch description");
		expect(stdout).toContain("TOTAL 1 patches +2 -1");
		expect(stdout).not.toContain("fork-only.txt");
	});

	test("exits 2 when --patches is combined with --write", async () => {
		const script = path.join(import.meta.dir, "upstream-inventory.ts");
		const proc = await $`bun ${script} --patches --write`.quiet().nothrow();
		expect(proc.exitCode).toBe(2);
	});
});
