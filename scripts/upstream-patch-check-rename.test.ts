import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";

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
	while (dirs.length) await fs.rm(dirs.pop() as string, { recursive: true, force: true });
});

describe("upstream-patch-check rename handling", () => {
	test("upstream rename does not displace conflict to uninventoried new path", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-patch-check-rename-"));
		dirs.push(dir);
		await ok(dir, ["init", "-b", "main"]);

		const baseContent = Array.from({ length: 12 }, (_, i) => `line ${i + 1}\n`).join("");
		await Bun.write(path.join(dir, "r.txt"), baseContent);
		const base = await commitAll(dir, "base: add r.txt (12 lines)");

		// Branch target from base: git mv r.txt moved.txt, edit line 1
		await ok(dir, ["checkout", "-b", "target", base]);
		await ok(dir, ["mv", "r.txt", "moved.txt"]);
		const targetContent = ["line 1 target\n", ...Array.from({ length: 11 }, (_, i) => `line ${i + 2}\n`)].join("");
		await Bun.write(path.join(dir, "moved.txt"), targetContent);
		const target = await commitAll(dir, "target: rename r.txt to moved.txt and edit line 1");

		// Branch fork from base: edit line 1 of r.txt, write docs/upstream-baseline.json and docs/upstream-fork-inventory.tsv
		await ok(dir, ["checkout", "-b", "fork", base]);
		const forkContent = ["line 1 fork\n", ...Array.from({ length: 11 }, (_, i) => `line ${i + 2}\n`)].join("");
		await Bun.write(path.join(dir, "r.txt"), forkContent);
		await Bun.write(
			path.join(dir, "docs", "upstream-baseline.json"),
			`${JSON.stringify({ upstream_repo: "https://github.com/can1357/oh-my-pi", target: base }, null, "\t")}\n`,
		);
		await Bun.write(
			path.join(dir, "docs", "upstream-fork-inventory.tsv"),
			[
				"path\tscope\tstate\thead_blob\tbehavior\tclassification",
				"r.txt\tshared\tmodified\t111111111111\tr.txt fork patch\tretained",
				"",
			].join("\n"),
		);
		await commitAll(dir, "fork: edit line 1 of r.txt and add baseline/inventory");

		const script = path.join(import.meta.dir, "upstream-patch-check.ts");
		const proc = await $`bun ${script} --target ${target}`.cwd(dir).quiet().nothrow().env(GIT_ENV);
		const exitCode = proc.exitCode;
		const stdout = proc.text();

		expect(exitCode).toBe(1);
		expect(stdout).toContain("BROKEN r.txt [shared] r.txt fork patch");
		expect(stdout.split("\n").some(line => line.includes("moved.txt"))).toBe(false);
		expect(stdout).not.toContain("[uninventoried]");
	});
});
