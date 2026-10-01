import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import type { InventoryRow } from "./upstream-inventory.ts";
import { classifyConflicts } from "./upstream-patch-check.ts";

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

interface Fixture {
	dir: string;
	base: string;
	fork: string;
	upstreamA: string;
	upstreamB: string;
}

/** Temp repo: baseline target = base; fork patch edits a.txt line 1; upstream edits a.txt or b.txt. */
async function makeRepo(): Promise<Fixture> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-patch-check-"));
	dirs.push(dir);
	await ok(dir, ["init", "-b", "main"]);
	await Bun.write(path.join(dir, "a.txt"), "a1\na2\na3\n");
	await Bun.write(path.join(dir, "b.txt"), "b1\nb2\nb3\n");
	const base = await commitAll(dir, "base");

	await ok(dir, ["checkout", "-b", "fork"]);
	await Bun.write(path.join(dir, "a.txt"), "a1-fork\na2\na3\n");
	const fork = await commitAll(dir, "fork edit a.txt line 1");

	await ok(dir, ["checkout", "-b", "upstream-a", base]);
	await Bun.write(path.join(dir, "a.txt"), "a1-upstream\na2\na3\n");
	const upstreamA = await commitAll(dir, "upstream edit a.txt line 1");

	await ok(dir, ["checkout", "-b", "upstream-b", base]);
	await Bun.write(path.join(dir, "b.txt"), "b1-upstream\nb2\nb3\n");
	const upstreamB = await commitAll(dir, "upstream edit b.txt line 1");

	await ok(dir, ["checkout", "fork"]);
	await Bun.write(
		path.join(dir, "docs", "upstream-baseline.json"),
		`${JSON.stringify({ upstream_repo: "https://github.com/can1357/oh-my-pi", target: base }, null, "\t")}\n`,
	);
	await Bun.write(
		path.join(dir, "docs", "upstream-fork-inventory.tsv"),
		[
			"path\tscope\tstate\thead_blob\tbehavior\tclassification",
			"a.txt\tshared\tmodified\t111111111111\tfork prepends a guard line to a.txt\tretained",
			"",
		].join("\n"),
	);
	return { dir, base, fork, upstreamA, upstreamB };
}

async function patchCheck(dir: string, args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
	const script = path.join(import.meta.dir, "upstream-patch-check.ts");
	const proc = await $`bun ${script} ${args}`.cwd(dir).quiet().nothrow().env(GIT_ENV);
	return { exitCode: proc.exitCode, stdout: proc.text(), stderr: proc.stderr.toString() };
}

afterEach(async () => {
	while (dirs.length) await fs.rm(dirs.pop() as string, { recursive: true, force: true });
});

describe("classifyConflicts", () => {
	const rows: InventoryRow[] = [
		{
			path: "a.txt",
			scope: "shared",
			state: "modified",
			headBlob: "111111111111",
			behavior: "fork prepends a guard line",
			classification: "retained",
		},
	];
	test("maps a conflicted path to its row scope and behavior", () => {
		expect(classifyConflicts(["a.txt"], rows)).toEqual([
			{ path: "a.txt", scope: "shared", behavior: "fork prepends a guard line" },
		]);
	});
	test("marks a path without a row uninventoried", () => {
		expect(classifyConflicts(["missing.ts"], rows)).toEqual([
			{ path: "missing.ts", scope: "uninventoried", behavior: "no inventory row" },
		]);
	});
});

describe("upstream-patch-check CLI", () => {
	test("an upstream edit of a.txt line 1 exits 1 and names the broken shared patch", async () => {
		const { dir, upstreamA } = await makeRepo();
		const result = await patchCheck(dir, ["--target", upstreamA]);
		expect(result.exitCode).toBe(1);
		expect(result.stdout).toContain("BROKEN a.txt [shared]");
		expect(result.stdout).toContain("fork prepends a guard line to a.txt");
		expect(result.stdout).toContain(`FAIL: 1 fork patch(es) broken by ${upstreamA.slice(0, 12)} (1 shared rows)`);
	});

	test("an upstream edit of b.txt only exits 0 and prints PASS", async () => {
		const { dir, upstreamB } = await makeRepo();
		const result = await patchCheck(dir, ["--target", upstreamB]);
		expect(result.exitCode).toBe(0);
		expect(result.stdout).toContain(`PASS: 1 fork patches merge cleanly with ${upstreamB.slice(0, 12)}`);
	});

	test("an unknown target sha exits 2 with a git fetch hint", async () => {
		const { dir } = await makeRepo();
		const result = await patchCheck(dir, ["--target", "f".repeat(40)]);
		expect(result.exitCode).toBe(2);
		expect(`${result.stdout}${result.stderr}`).toContain("git fetch");
	});
});
