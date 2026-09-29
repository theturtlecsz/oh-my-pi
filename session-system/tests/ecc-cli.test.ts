import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

const repoRoot = path.resolve(import.meta.dir, "../..");
const cli = path.join(repoRoot, "session-system/ecc/adapter/cli.ts");
const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true });
});

async function makeProject(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-cli-"));
	tempDirs.push(dir);
	return dir;
}

/** Spawn the CLI with stdin closed so a prompt would see EOF instead of a TTY. */
async function runCli(args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
	const env = { ...process.env };
	delete env.FORCE_COLOR;
	delete env.NO_COLOR;
	const proc = Bun.spawn(["bun", cli, ...args], {
		cwd: repoRoot,
		stdin: "ignore",
		stdout: "pipe",
		stderr: "pipe",
		env,
	});
	const [stdout, stderr, code] = await Promise.all([
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
		proc.exited,
	]);
	return { code, stdout, stderr };
}

describe("ECC lifecycle CLI", () => {
	test("installs owned files, reinstalls unchanged, and remove deletes them", async () => {
		const project = await makeProject();
		const installed = await runCli(["install", "--project", project]);
		expect(installed.stderr).toBe("");
		expect(installed.code).toBe(0);
		const first = JSON.parse(installed.stdout) as { status: string; written: string[] };
		expect(first.status).toBe("installed");
		expect(first.written.length).toBeGreaterThan(0);
		const lockPath = path.join(project, ".omp", "ecc", "adapted.lock.json");
		expect(await Bun.file(lockPath).exists()).toBe(true);
		for (const rel of first.written) {
			expect(await Bun.file(path.join(project, rel)).exists()).toBe(true);
		}

		const again = await runCli(["install", "--project", project]);
		expect(again.stderr).toBe("");
		expect(again.code).toBe(0);
		expect(JSON.parse(again.stdout).status).toBe("unchanged");

		const removed = await runCli(["remove", "--project", project]);
		expect(removed.stderr).toBe("");
		expect(removed.code).toBe(0);
		expect(JSON.parse(removed.stdout).status).toBe("removed");
		expect(await Bun.file(lockPath).exists()).toBe(false);
		for (const rel of first.written) {
			expect(await Bun.file(path.join(project, rel)).exists()).toBe(false);
		}
	}, 30_000);

	test("InstallError prints an error object and exits 1 without prompting", async () => {
		const project = await makeProject();
		const owned = path.join(project, ".omp", "rules", "ecc-common-coding-style.md");
		await fs.mkdir(path.dirname(owned), { recursive: true });
		await Bun.write(owned, "# user-owned rule\n");

		const result = await runCli(["install", "--project", project]);
		expect(result.code).toBe(1);
		expect(result.stderr).toBe("");
		const body = JSON.parse(result.stdout) as { error?: string };
		expect(body.error).toContain("refusing to overwrite unowned");
		expect(await Bun.file(path.join(project, ".omp", "ecc", "adapted.lock.json")).exists()).toBe(false);
	}, 30_000);

	test("preview writes nothing", async () => {
		const project = await makeProject();
		const result = await runCli(["preview", "--project", project]);
		expect(result.code).toBe(0);
		expect(result.stderr).toBe("");
		const body = JSON.parse(result.stdout) as { willWrite: string[]; conflicts: string[] };
		expect(body.willWrite.length).toBeGreaterThan(0);
		expect(body.conflicts).toEqual([]);
		expect(await Bun.file(path.join(project, ".omp", "ecc", "adapted.lock.json")).exists()).toBe(false);
	}, 30_000);
});
