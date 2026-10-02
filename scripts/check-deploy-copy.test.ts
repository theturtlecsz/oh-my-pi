import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import { checkDeployCopy, classifyCopy, UnknownRevisionError } from "./check-deploy-copy.ts";

const SCRIPT = path.join(import.meta.dir, "check-deploy-copy.ts");

const dirs: string[] = [];

afterEach(async () => {
	while (dirs.length) await fs.rm(dirs.pop() as string, { recursive: true, force: true });
});

async function git(dir: string, args: string[]): Promise<{ exitCode: number; out: string; err: string }> {
	const proc = await $`git ${args}`
		.cwd(dir)
		.quiet()
		.nothrow()
		.env({
			...process.env,
			GIT_CONFIG_GLOBAL: "/dev/null",
			GIT_CONFIG_SYSTEM: "/dev/null",
			GIT_AUTHOR_NAME: "deploy-copy-test",
			GIT_AUTHOR_EMAIL: "deploy-copy@test",
			GIT_COMMITTER_NAME: "deploy-copy-test",
			GIT_COMMITTER_EMAIL: "deploy-copy@test",
		});
	return { exitCode: proc.exitCode, out: proc.text(), err: proc.stderr.toString() };
}

async function ok(dir: string, args: string[]): Promise<string> {
	const result = await git(dir, args);
	if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${result.err}`);
	return result.out;
}

/** A repo whose `target` branch is one commit behind its `main`. */
async function makeRepo(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-deploy-copy-"));
	dirs.push(dir);
	await ok(dir, ["init", "-b", "main"]);
	await Bun.write(path.join(dir, "file.txt"), "one\n");
	await Bun.write(path.join(dir, "uv.lock"), "lock one\n");
	await Bun.write(path.join(dir, "packages/foo/uv.lock"), "lock one\n");
	await ok(dir, ["add", "-A"]);
	await ok(dir, ["commit", "-m", "one"]);
	await ok(dir, ["branch", "target"]);
	await Bun.write(path.join(dir, "file.txt"), "two\n");
	await ok(dir, ["add", "-A"]);
	await ok(dir, ["commit", "-m", "two"]);
	return dir;
}

async function cli(dir: string, args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
	const proc = await $`bun ${SCRIPT} --target target ${dir} ${args}`.quiet().nothrow();
	return { exitCode: proc.exitCode, stdout: proc.text(), stderr: proc.stderr.toString() };
}

describe("edit classification", () => {
	test("keeps generated uv.lock edits apart from edits that stop a deploy", async () => {
		const dir = await makeRepo();
		await ok(dir, ["checkout", "--detach", "target"]);
		await Bun.write(path.join(dir, "file.txt"), "hand edit\n");
		await Bun.write(path.join(dir, "packages/foo/uv.lock"), "lock\n");

		const [status] = await checkDeployCopy({ copies: [dir], target: "target" });
		expect(status.edits.map(line => line.slice(3))).toEqual(["file.txt"]);
		expect(status.generatedEdits.map(line => line.slice(3))).toEqual(["packages/foo/uv.lock"]);
		expect(classifyCopy(status)).toBe("dirty");
	});
});

describe("checkDeployCopy", () => {
	test("reports ok for a clean copy pinned to the target", async () => {
		const dir = await makeRepo();
		await ok(dir, ["checkout", "--detach", "target"]);

		const [status] = await checkDeployCopy({ copies: [dir], target: "target" });
		expect(status.atTarget).toBe(true);
		expect(status.edits).toEqual([]);
		expect(classifyCopy(status)).toBe("ok");

		const result = await cli(dir, []);
		expect(result.exitCode).toBe(0);
		expect(result.stdout).toContain(`${dir} ok:`);
	});

	test("flags a copy whose HEAD passed the target as stale", async () => {
		const dir = await makeRepo(); // HEAD is main, target is one commit behind

		const [status] = await checkDeployCopy({ copies: [dir], target: "target" });
		expect(status.atTarget).toBe(false);
		expect(classifyCopy(status)).toBe("stale");

		const result = await cli(dir, []);
		expect(result.exitCode).toBe(1);
		expect(result.stdout).toContain(`${dir} stale:`);
		expect(result.stdout).toContain("target ");
	});

	test("flags a tracked local edit even when HEAD equals the target (the 2026-10-02 bug)", async () => {
		const dir = await makeRepo();
		await ok(dir, ["checkout", "--detach", "target"]);
		await Bun.write(path.join(dir, "file.txt"), "hand edit\n");

		const [status] = await checkDeployCopy({ copies: [dir], target: "target" });
		expect(status.atTarget).toBe(true);
		expect(status.edits.map(line => line.slice(3))).toEqual(["file.txt"]);
		expect(classifyCopy(status)).toBe("dirty");

		const result = await cli(dir, []);
		expect(result.exitCode).toBe(1);
		expect(result.stdout).toContain(`${dir} dirty:`);
		expect(result.stdout).toContain("file.txt");
	});

	test("reports a stale copy with an edit as stale+dirty", async () => {
		const dir = await makeRepo();
		await Bun.write(path.join(dir, "file.txt"), "edit on main\n");

		const [status] = await checkDeployCopy({ copies: [dir], target: "target" });
		expect(classifyCopy(status)).toBe("stale+dirty");
		expect((await cli(dir, [])).exitCode).toBe(1);
	});

	test("passes a copy whose only edit is a generated uv.lock, and still reports it", async () => {
		const dir = await makeRepo();
		await ok(dir, ["checkout", "--detach", "target"]);
		await Bun.write(path.join(dir, "uv.lock"), "lock\n");

		expect((await cli(dir, [])).exitCode).toBe(0);
		const result = await cli(dir, []);
		expect(result.stdout).toContain("discardable generated edit(s): uv.lock");
	});

	test("checks several copies and fails when any one is not clean at the target", async () => {
		const clean = await makeRepo();
		await ok(clean, ["checkout", "--detach", "target"]);
		const dirty = await makeRepo();
		await ok(dirty, ["checkout", "--detach", "target"]);
		await Bun.write(path.join(dirty, "file.txt"), "edit\n");

		const result = await cli(clean, [dirty]);
		expect(result.exitCode).toBe(1);
		expect(result.stdout).toContain(`${clean} ok:`);
		expect(result.stdout).toContain(`${dirty} dirty:`);
	});

	test("throws UnknownRevisionError for a non-worktree, and the CLI exits 2", async () => {
		const plain = await fs.mkdtemp(path.join(os.tmpdir(), "omp-deploy-copy-plain-"));
		dirs.push(plain);

		await expect(checkDeployCopy({ copies: [plain], target: "target" })).rejects.toBeInstanceOf(UnknownRevisionError);
		const unknown = await cli(plain, []);
		expect(unknown.exitCode).toBe(2);

		const dir = await makeRepo();
		await ok(dir, ["checkout", "--detach", "target"]);
		expect((await cli(dir, [])).exitCode).toBe(0);
		const missingTarget = await $`bun ${SCRIPT} --target nowhere ${dir}`.quiet().nothrow();
		expect(missingTarget.exitCode).toBe(2);
	});
});
