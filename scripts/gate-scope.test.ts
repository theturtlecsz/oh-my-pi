import { afterEach, describe, expect, test } from "bun:test";
import { $ } from "bun";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { changedFiles, isSkipSafePythonLedger, isSkipSafeTsCheck, selectGateScope } from "./gate-scope.ts";

const repoRoot = path.join(import.meta.dir, "..");
const tempDirs: string[] = [];

afterEach(async () => {
	await Promise.all(tempDirs.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

async function makeTempDir(prefix = "omp-gate-scope-test-"): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

async function runCliSubprocess(
	env: Record<string, string>,
	cwd?: string,
): Promise<{
	exitCode: number;
	stdout: string;
	stderr: string;
}> {
	const scriptPath = path.resolve(repoRoot, "scripts/gate-scope.ts");
	const proc = Bun.spawn(["bun", scriptPath], {
		cwd: cwd ?? repoRoot,
		env: {
			...process.env,
			...env,
		},
		stdout: "pipe",
		stderr: "pipe",
	});

	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);

	return { exitCode, stdout, stderr };
}

describe("selectGateScope", () => {
	test("[packages/ai/src/a.ts, docs/upstream-fork-inventory.tsv]: python-ledger skip, ts-check run", () => {
		const result = selectGateScope(["packages/ai/src/a.ts", "docs/upstream-fork-inventory.tsv"]);
		expect(result["python-ledger"].run).toBe(false);
		expect(result["ts-check"].run).toBe(true);
		expect(result["ts-check"].reason).toBe("packages/ai/src/a.ts");
	});

	test("[python/omp-work/src/omp_work/a.py]: python-ledger run, ts-check skip", () => {
		const result = selectGateScope(["python/omp-work/src/omp_work/a.py"]);
		expect(result["python-ledger"].run).toBe(true);
		expect(result["python-ledger"].reason).toBe("python/omp-work/src/omp_work/a.py");
		expect(result["ts-check"].run).toBe(false);
	});

	test("[python/omp-work/src/omp_work/contracts/v1/a.json]: ts-check run", () => {
		const result = selectGateScope(["python/omp-work/src/omp_work/contracts/v1/a.json"]);
		expect(result["ts-check"].run).toBe(true);
		expect(result["ts-check"].reason).toBe("python/omp-work/src/omp_work/contracts/v1/a.json");
		expect(result["python-ledger"].run).toBe(true);
	});

	test("[packages/work-client/src/contract.ts]: both run", () => {
		const result = selectGateScope(["packages/work-client/src/contract.ts"]);
		expect(result["python-ledger"].run).toBe(true);
		expect(result["python-ledger"].reason).toBe("packages/work-client/src/contract.ts");
		expect(result["ts-check"].run).toBe(true);
		expect(result["ts-check"].reason).toBe("packages/work-client/src/contract.ts");
	});

	test("[]: both run with reason 'no changed files'", () => {
		const result = selectGateScope([]);
		expect(result["python-ledger"].run).toBe(true);
		expect(result["python-ledger"].reason).toBe("no changed files");
		expect(result["ts-check"].run).toBe(true);
		expect(result["ts-check"].reason).toBe("no changed files");
	});

	test("reason is the first forcing file when multiple files force", () => {
		const result = selectGateScope([
			"packages/ai/src/a.ts", // skip-safe for python-ledger, forces ts-check
			"python/omp-work/src/omp_work/a.py", // forces python-ledger, skip-safe for ts-check
			"packages/work-client/src/contract.ts",
		]);
		expect(result["python-ledger"].run).toBe(true);
		expect(result["python-ledger"].reason).toBe("python/omp-work/src/omp_work/a.py");
		expect(result["ts-check"].run).toBe(true);
		expect(result["ts-check"].reason).toBe("packages/ai/src/a.ts");
	});
});

describe("isSkipSafePythonLedger", () => {
	test("packages/** skip-safe except work-client and coding-agent entrypoints", () => {
		expect(isSkipSafePythonLedger("packages/ai/src/index.ts")).toBe(true);
		expect(isSkipSafePythonLedger("packages/tui/src/components/box.ts")).toBe(true);
		expect(isSkipSafePythonLedger("packages/coding-agent/src/utils/file.ts")).toBe(true);
		expect(isSkipSafePythonLedger("packages/work-client/src/contract.ts")).toBe(false);
		expect(isSkipSafePythonLedger("packages/work-client/package.json")).toBe(false);
		expect(isSkipSafePythonLedger("packages/coding-agent/src/cli.ts")).toBe(false);
		expect(isSkipSafePythonLedger("packages/coding-agent/src/index.ts")).toBe(false);
	});

	test("crates/**, evals/**, scripts/** (except release.ts), docs/upstream-fork-inventory.tsv are skip-safe", () => {
		expect(isSkipSafePythonLedger("crates/pi-natives/src/lib.rs")).toBe(true);
		expect(isSkipSafePythonLedger("evals/benchmark.json")).toBe(true);
		expect(isSkipSafePythonLedger("scripts/gate-scope.ts")).toBe(true);
		expect(isSkipSafePythonLedger("scripts/release.ts")).toBe(false);
		expect(isSkipSafePythonLedger("docs/upstream-fork-inventory.tsv")).toBe(true);
		expect(isSkipSafePythonLedger("docs/README.md")).toBe(false);
		expect(isSkipSafePythonLedger("package.json")).toBe(false);
	});
});

describe("isSkipSafeTsCheck", () => {
	test("under python/ skip-safe except robomp/web and script/config extensions", () => {
		expect(isSkipSafeTsCheck("python/omp-work/src/omp_work/models.py")).toBe(true);
		expect(isSkipSafeTsCheck("python/omp-work/pyproject.toml")).toBe(true);
		expect(isSkipSafeTsCheck("python/robomp/web/App.vue")).toBe(false);
		expect(isSkipSafeTsCheck("python/robomp/web/index.html")).toBe(false);
		expect(isSkipSafeTsCheck("python/robomp/web/src/main.ts")).toBe(false);
		expect(isSkipSafeTsCheck("python/omp-work/tests/test.ts")).toBe(false);
		expect(isSkipSafeTsCheck("python/omp-work/config.json")).toBe(false);
		expect(isSkipSafeTsCheck("python/omp-work/schema.jsonc")).toBe(false);
		expect(isSkipSafeTsCheck("python/script.js")).toBe(false);
		expect(isSkipSafeTsCheck("python/script.mjs")).toBe(false);
		expect(isSkipSafeTsCheck("python/script.cjs")).toBe(false);
		expect(isSkipSafeTsCheck("python/component.tsx")).toBe(false);
	});

	test("under docs/ skip-safe except ts/js/json extensions", () => {
		expect(isSkipSafeTsCheck("docs/upstream-guardrail.md")).toBe(true);
		expect(isSkipSafeTsCheck("docs/upstream-fork-inventory.tsv")).toBe(true);
		expect(isSkipSafeTsCheck("docs/diagram.png")).toBe(true);
		expect(isSkipSafeTsCheck("docs/spec.json")).toBe(false);
		expect(isSkipSafeTsCheck("docs/code.ts")).toBe(false);
	});

	test("root *.md is skip-safe, non-root *.md or non-md root is not", () => {
		expect(isSkipSafeTsCheck("README.md")).toBe(true);
		expect(isSkipSafeTsCheck("AGENTS.md")).toBe(true);
		expect(isSkipSafeTsCheck("MASTER.md")).toBe(true);
		expect(isSkipSafeTsCheck("package.json")).toBe(false);
		expect(isSkipSafeTsCheck("packages/coding-agent/README.md")).toBe(false);
	});
});

describe("changedFiles", () => {
	test("returns changed files list between base and HEAD", async () => {
		const files = await changedFiles("HEAD~1", repoRoot);
		expect(Array.isArray(files)).toBe(true);
	});

	test("throws on invalid base", async () => {
		await expect(changedFiles("nonexistent-base-rev-12345", repoRoot)).rejects.toThrow();
	});
});

describe("gate-scope CLI", () => {
	test("CLI with an unknown base exits 1", async () => {
		const { exitCode, stderr } = await runCliSubprocess({
			GATE_SCOPE_BASE: "nonexistent-base-12345",
		});
		expect(exitCode).toBe(1);
		expect(stderr.length).toBeGreaterThan(0);
	});

	test("CLI without GATE_SCOPE_BASE exits 1", async () => {
		const { exitCode, stderr } = await runCliSubprocess({
			GATE_SCOPE_BASE: "",
		});
		expect(exitCode).toBe(1);
		expect(stderr).toContain("GATE_SCOPE_BASE is required");
	});

	test("CLI writes output to GATE_SCOPE_OUTPUT and prints gate scope lines", async () => {
		const tempDir = await makeTempDir();
		const outputFile = path.join(tempDir, "scope_output");

		const { exitCode, stdout } = await runCliSubprocess({
			GATE_SCOPE_BASE: "HEAD",
			GATE_SCOPE_OUTPUT: outputFile,
		});

		expect(exitCode).toBe(0);
		expect(stdout).toContain("gate scope:");
		const outputContent = await Bun.file(outputFile).text();
		expect(outputContent).toContain("python-ledger=");
		expect(outputContent).toContain("ts-check=");
	});

	test("Rename boundary: in a temp git repo a branch commits git mv python/omp-work/src/omp_work/m.py scripts/m.py; the CLI there writes python-ledger=run", async () => {
		const tempRepo = await makeTempDir("omp-gate-scope-git-");
		const outputFile = path.join(tempRepo, "scope_output");

		await $`git init -b main`.cwd(tempRepo).quiet();
		await $`git config user.name "Test"`.cwd(tempRepo).quiet();
		await $`git config user.email "test@example.com"`.cwd(tempRepo).quiet();

		await fs.mkdir(path.join(tempRepo, "python/omp-work/src/omp_work"), { recursive: true });
		await fs.writeFile(path.join(tempRepo, "python/omp-work/src/omp_work/m.py"), "print('hello')\n");
		await $`git add -A && git commit -m "initial commit"`.cwd(tempRepo).quiet();

		await $`git checkout -b feature`.cwd(tempRepo).quiet();
		await fs.mkdir(path.join(tempRepo, "scripts"), { recursive: true });
		await $`git mv python/omp-work/src/omp_work/m.py scripts/m.py`.cwd(tempRepo).quiet();
		await $`git commit -m "move python file to scripts"`.cwd(tempRepo).quiet();

		const { exitCode, stdout } = await runCliSubprocess(
			{
				GATE_SCOPE_BASE: "main",
				GATE_SCOPE_OUTPUT: outputFile,
			},
			tempRepo,
		);

		expect(exitCode).toBe(0);
		expect(stdout).toContain("gate scope: run python-ledger (python/omp-work/src/omp_work/m.py)");

		const outputContent = await Bun.file(outputFile).text();
		expect(outputContent).toContain("python-ledger=run\n");
	});
});
