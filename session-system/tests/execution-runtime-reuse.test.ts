import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test } from "bun:test";
import { dirtyPaths, ensureExecutionWorkspace } from "../extensions/workflow/git";

describe("execution runtime dependency reuse (OMP-472)", () => {
	const roots: string[] = [];

	afterEach(() => {
		for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true });
	});

	const git = (cwd: string, args: string[]): string => {
		const result = Bun.spawnSync(["git", ...args], { cwd, stdout: "pipe", stderr: "pipe" });
		if (result.exitCode !== 0) {
			throw new Error(`git ${args.join(" ")} failed: ${result.stderr.toString()}`);
		}
		return result.stdout.toString().trim();
	};

	const makeRepo = (
		withNodeModules: boolean,
	): { repo: string; head: string; worktreesRoot: string; marker: string } => {
		const root = fs.mkdtempSync(path.join(os.tmpdir(), "ss-exec-reuse-"));
		roots.push(root);
		const repo = path.join(root, "repo");
		fs.mkdirSync(path.join(repo, "packages/pkg"), { recursive: true });
		fs.writeFileSync(path.join(repo, "package.json"), JSON.stringify({ name: "exec-reuse-probe" }));
		fs.writeFileSync(path.join(repo, "bun.lock"), "lock\n");
		fs.writeFileSync(path.join(repo, ".gitignore"), "node_modules\n");
		fs.writeFileSync(path.join(repo, "packages/pkg/index.js"), "module.exports = {};\n");
		git(repo, ["init", "--initial-branch=main", "-q"]);
		git(repo, ["config", "user.email", "test@example.com"]);
		git(repo, ["config", "user.name", "Test"]);
		git(repo, ["add", "--", "package.json", "bun.lock", ".gitignore", "packages/pkg/index.js"]);
		git(repo, ["commit", "-q", "-m", "seed"]);
		if (withNodeModules) {
			fs.mkdirSync(path.join(repo, "node_modules/@ws"), { recursive: true });
			fs.mkdirSync(path.join(repo, "node_modules/lib"), { recursive: true });
			fs.symlinkSync("../../packages/pkg", path.join(repo, "node_modules/@ws/pkg"));
			fs.writeFileSync(path.join(repo, "node_modules/lib/a.js"), "a\n");
		}
		return {
			repo,
			head: git(repo, ["rev-parse", "HEAD"]),
			worktreesRoot: path.join(root, "worktrees"),
			marker: path.join(root, "install-marker"),
		};
	};

	const installStub = (marker: string) => ({
		command: [
			process.execPath,
			"-e",
			`require("node:fs").writeFileSync(${JSON.stringify(marker)}, "ran"); process.exit(1);`,
		],
		timeoutMs: 10_000,
	});

	test("matching lockfile clones primary node_modules and skips install", async () => {
		const { repo, head, worktreesRoot, marker } = makeRepo(true);
		const grantId = "00000000-0000-7000-8000-000000000301";
		const ws = await ensureExecutionWorkspace(repo, "OMP-472", grantId, head, {}, worktreesRoot, installStub(marker));
		expect(fs.existsSync(path.join(ws.path, "node_modules/lib/a.js"))).toBe(true);
		const pkgReal = fs.realpathSync(path.join(ws.path, "node_modules/@ws/pkg"));
		const wsReal = fs.realpathSync(ws.path);
		expect(pkgReal.startsWith(`${wsReal}${path.sep}`)).toBe(true);
		expect(fs.existsSync(marker)).toBe(false);
		expect(dirtyPaths(ws.path)).toEqual([]);
	});

	test("an uncommitted primary bun.lock edit falls through to install", async () => {
		const { repo, head, worktreesRoot, marker } = makeRepo(true);
		fs.appendFileSync(path.join(repo, "bun.lock"), "edited\n");
		const grantId = "00000000-0000-7000-8000-000000000302";
		await expect(
			ensureExecutionWorkspace(repo, "OMP-472", grantId, head, {}, worktreesRoot, installStub(marker)),
		).rejects.toThrow(/dependency install failed/);
		expect(fs.existsSync(marker)).toBe(true);
	});

	test("a primary checkout without node_modules falls through to install", async () => {
		const { repo, head, worktreesRoot, marker } = makeRepo(false);
		const grantId = "00000000-0000-7000-8000-000000000303";
		await expect(
			ensureExecutionWorkspace(repo, "OMP-472", grantId, head, {}, worktreesRoot, installStub(marker)),
		).rejects.toThrow(/dependency install failed/);
		expect(fs.existsSync(marker)).toBe(true);
	});
});
