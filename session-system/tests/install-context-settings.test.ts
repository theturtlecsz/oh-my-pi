import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const installSh = path.join(import.meta.dir, "..", "install.sh");
const tempDirs: string[] = [];

afterEach(() => {
	for (const dir of tempDirs.splice(0)) {
		fs.rmSync(dir, { recursive: true, force: true });
	}
});

interface RunResult {
	exitCode: number;
	stdout: string;
	stderr: string;
}

function runInstall(home: string, customEnv: Record<string, string> = {}, ...args: string[]): RunResult {
	const env: Record<string, string> = {};
	for (const [k, v] of Object.entries(process.env)) {
		if (v === undefined) continue;
		if (k === "XDG_CONFIG_HOME" || k.startsWith("OMP_KNOWLEDGE_")) continue;
		env[k] = v;
	}
	env.HOME = home;
	Object.assign(env, customEnv);

	const proc = Bun.spawnSync(["bash", installSh, ...args], {
		env,
		stdout: "pipe",
		stderr: "pipe",
	});
	return { exitCode: proc.exitCode, stdout: proc.stdout.toString(), stderr: proc.stderr.toString() };
}

function fakeHome(): string {
	const home = fs.mkdtempSync(path.join(os.tmpdir(), "omp-install-context-test-"));
	tempDirs.push(home);
	return home;
}

function createFilteredBinDir(exclude: string[]): string {
	const binDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-bin-filter-"));
	tempDirs.push(binDir);
	const pathEntries = (process.env.PATH || "").split(":");
	for (const p of pathEntries) {
		if (!fs.existsSync(p)) continue;
		try {
			for (const file of fs.readdirSync(p)) {
				if (exclude.includes(file)) continue;
				const dest = path.join(binDir, file);
				if (!fs.existsSync(dest)) {
					try {
						fs.symlinkSync(path.join(p, file), dest);
					} catch {}
				}
			}
		} catch {}
	}
	return binDir;
}

// install.sh writes context.json only when both uv and bun resolve on PATH.
// Stub both in a temp dir so that write is the same on every host.
function pathWithUvAndBunStubs(): string {
	const binDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-uv-bun-stubs-"));
	tempDirs.push(binDir);
	for (const name of ["uv", "bun"]) {
		const stub = path.join(binDir, name);
		fs.writeFileSync(stub, "#!/bin/sh\nexit 0\n");
		fs.chmodSync(stub, 0o755);
	}
	return `${binDir}:${process.env.PATH ?? ""}`;
}

function runInstallWithTools(home: string, customEnv: Record<string, string> = {}, ...args: string[]): RunResult {
	return runInstall(home, { ...customEnv, PATH: pathWithUvAndBunStubs() }, ...args);
}

describe("install.sh context settings", () => {
	test("fresh install writes exactly those keys, mode 0600, --project path and cli.ts inside this checkout", () => {
		const home = fakeHome();
		const result = runInstallWithTools(home);
		expect(result.exitCode, result.stderr).toBe(0);

		const configPath = path.join(home, ".config", "omp-knowledge", "context.json");
		expect(fs.existsSync(configPath)).toBe(true);

		const stat = fs.statSync(configPath);
		expect(stat.mode & 0o777).toBe(0o600);

		const raw = fs.readFileSync(configPath, "utf8");
		const json = JSON.parse(raw);

		const expectedKeys = [
			"command",
			"state_dir",
			"structural_state_dir",
			"token_cmd",
			"encoding",
			"token_budget",
			"engine",
			"reranker_url",
			"reranker_model",
		];
		expect(Object.keys(json).sort()).toEqual([...expectedKeys].sort());

		const repoRoot = path.resolve(import.meta.dir, "..", "..");

		expect(Array.isArray(json.command)).toBe(true);
		expect(json.command).toHaveLength(8);
		expect(path.isAbsolute(json.command[0])).toBe(true);
		expect(json.command.slice(1, 4)).toEqual(["run", "--frozen", "--project"]);
		expect(json.command[4]).toBe(path.join(repoRoot, "python", "omp-knowledge"));
		expect(json.command[4].startsWith(repoRoot)).toBe(true);
		expect(json.command.slice(5)).toEqual(["python", "-m", "omp_knowledge.context"]);

		const expectedStateDir = path.join(home, ".local", "state", "omp-fleet-knowledge", "knowledge-data");
		expect(json.state_dir).toBe(expectedStateDir);
		expect(json.structural_state_dir).toBe(expectedStateDir);

		expect(Array.isArray(json.token_cmd)).toBe(true);
		expect(json.token_cmd).toHaveLength(3);
		expect(path.isAbsolute(json.token_cmd[0])).toBe(true);
		expect(json.token_cmd[1]).toBe(path.join(repoRoot, "packages", "coding-agent", "src", "cli.ts"));
		expect(json.token_cmd[1].startsWith(repoRoot)).toBe(true);
		expect(json.token_cmd[2]).toBe("tokens");

		expect(json.encoding).toBe("O200kBase");
		expect(json.token_budget).toBe(8000);

		expect(json.engine).toBe("none");
		expect(json.reranker_url).toBeNull();
		expect(json.reranker_model).toBeNull();
	});

	test("reranker URL/model, engine cognee and state dir set at install are recorded", () => {
		const home = fakeHome();
		const customStateDir = path.join(home, "custom-state-dir");
		const result = runInstallWithTools(home, {
			OMP_KNOWLEDGE_ENGINE: "cognee",
			OMP_KNOWLEDGE_RERANKER_URL: "http://127.0.0.1:9099/rerank",
			OMP_KNOWLEDGE_RERANKER_MODEL: "bge-reranker-large",
			OMP_KNOWLEDGE_STATE_DIR: customStateDir,
		});
		expect(result.exitCode, result.stderr).toBe(0);

		const configPath = path.join(home, ".config", "omp-knowledge", "context.json");
		const json = JSON.parse(fs.readFileSync(configPath, "utf8"));
		expect(json.engine).toBe("cognee");
		expect(json.reranker_url).toBe("http://127.0.0.1:9099/rerank");
		expect(json.reranker_model).toBe("bge-reranker-large");
		expect(json.state_dir).toBe(customStateDir);
		expect(json.structural_state_dir).toBe(customStateDir);
	});

	test("a rerun keeps an edited file byte-identical", () => {
		const home = fakeHome();
		const firstResult = runInstallWithTools(home);
		expect(firstResult.exitCode, firstResult.stderr).toBe(0);

		const configPath = path.join(home, ".config", "omp-knowledge", "context.json");
		const editedContent = '{\n  "custom": "edited content that must not be overwritten"\n}\n';
		fs.writeFileSync(configPath, editedContent, "utf8");

		const secondResult = runInstallWithTools(home, {
			OMP_KNOWLEDGE_ENGINE: "cognee",
			OMP_KNOWLEDGE_RERANKER_URL: "http://example.com/rerank",
		});
		expect(secondResult.exitCode, secondResult.stderr).toBe(0);

		const afterRerun = fs.readFileSync(configPath, "utf8");
		expect(afterRerun).toBe(editedContent);
	});

	test("OMP_KNOWLEDGE_CONFIG_DIR moves the file", () => {
		const home = fakeHome();
		const customConfigDir = path.join(fakeHome(), "my-custom-config-dir");
		const result = runInstallWithTools(home, {
			OMP_KNOWLEDGE_CONFIG_DIR: customConfigDir,
		});
		expect(result.exitCode, result.stderr).toBe(0);

		const customConfigPath = path.join(customConfigDir, "context.json");
		expect(fs.existsSync(customConfigPath)).toBe(true);
		expect(fs.existsSync(path.join(home, ".config", "omp-knowledge", "context.json"))).toBe(false);

		const stat = fs.statSync(customConfigPath);
		expect(stat.mode & 0o777).toBe(0o600);
	});

	test("engine bogus exits 2, HOME left empty", () => {
		const home = fakeHome();
		const result = runInstall(home, {
			OMP_KNOWLEDGE_ENGINE: "bogus",
		});
		expect(result.exitCode).toBe(2);
		expect(result.stderr).toContain("OMP_KNOWLEDGE_ENGINE");
		expect(fs.readdirSync(home)).toEqual([]);
	});

	test("PATH without uv: exit 0, warning, no file", () => {
		const home = fakeHome();
		const binDir = createFilteredBinDir(["uv"]);

		const result = runInstall(home, {
			PATH: binDir,
		});
		expect(result.exitCode, result.stderr).toBe(0);
		const combined = result.stdout + result.stderr;
		expect(combined.toLowerCase()).toContain("warning");
		expect(combined).toContain("uv");

		const configPath = path.join(home, ".config", "omp-knowledge", "context.json");
		expect(fs.existsSync(configPath)).toBe(false);
	});

	test("PATH without bun: exit 0, warning, no file", () => {
		const home = fakeHome();
		const binDir = createFilteredBinDir(["bun"]);

		const result = runInstall(home, {
			PATH: binDir,
		});
		expect(result.exitCode, result.stderr).toBe(0);
		const combined = result.stdout + result.stderr;
		expect(combined.toLowerCase()).toContain("warning");
		expect(combined).toContain("bun");

		const configPath = path.join(home, ".config", "omp-knowledge", "context.json");
		expect(fs.existsSync(configPath)).toBe(false);
	});
});
