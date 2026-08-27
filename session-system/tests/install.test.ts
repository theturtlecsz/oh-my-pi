import { afterEach, describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// install.sh --print-manifest contract (OMP-156): a read-only, LC_ALL=C-sorted TSV of
// every managed live destination — extensions root walked recursively plus the
// singleton files and skill links — comparable across runs with cmp(1).

const installSh = join(import.meta.dir, "..", "install.sh");
const tempDirs: string[] = [];

afterEach(() => {
	for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

interface RunResult {
	exitCode: number;
	stdout: string;
	stderr: string;
}

function runInstall(home: string, ...args: string[]): RunResult {
	const proc = Bun.spawnSync(["bash", installSh, ...args], {
		env: { ...process.env, HOME: home } as Record<string, string>,
		stdout: "pipe",
		stderr: "pipe",
	});
	return { exitCode: proc.exitCode, stdout: proc.stdout.toString(), stderr: proc.stderr.toString() };
}

function fakeHome(): string {
	const home = mkdtempSync(join(tmpdir(), "omp-install-test-"));
	tempDirs.push(home);
	return home;
}

const MANAGED_SINGLETONS = [
	"/.omp/agent/agents/auditor.md",
	"/.omp/agent/rules/work-plan.md",
	"/.omp/agent/rules/linear-plan.md",
	"/AGENTS.md",
	"/.omp/agent/AGENTS.md",
	"/.omp/agent/hook/task-observer-first-tool.mjs",
];
const AGENT_SKILLS = ["summary", "questionyourself", "whatsmissing"];
const OMP_SKILLS = ["intake", "caveman", "caveman-commit", "caveman-review", "task-observer"];
const CLAUDE_SKILLS = ["summary", "questionyourself", "whatsmissing", "intake", "task-observer"];
const CODEX_SKILLS = ["task-observer"];
const RETIRED_SKILLS = [
	"caveman-compress",
	"caveman-help",
	"notebooklm",
	"prompt-master",
	"vibe-check",
	"wiz-ccr-creator",
	"wiz-mcp",
];

describe("install.sh --print-manifest", () => {
	test("is read-only and reports every managed destination absent on a fresh home", () => {
		const home = fakeHome();
		const result = runInstall(home, "--print-manifest");
		expect(result.exitCode, result.stderr).toBe(0);
		const lines = result.stdout.split("\n").filter(Boolean);
		// ext root + 6 singletons + 3 agent skills + 5 omp skills + 5 claude skills + 1 codex skill + 21 retired skill destinations, all absent
		expect(lines).toHaveLength(42);
		for (const line of lines) {
			const cells = line.split("\t");
			expect(cells).toHaveLength(4);
			expect(cells[1]).toBe("absent");
		}
		// read-only: nothing was created under the fake home
		expect(readdirSync(home)).toEqual([]);
	});

	test("covers the extensions tree, singletons, and both skill sets after install", () => {
		const home = fakeHome();
		const install = runInstall(home);
		expect(install.exitCode, install.stderr).toBe(0);

		const result = runInstall(home, "--print-manifest");
		expect(result.exitCode, result.stderr).toBe(0);
		const lines = result.stdout.split("\n").filter(Boolean);
		const byPath: Record<string, string[]> = {};
		for (const line of lines) {
			const cells = line.split("\t");
			expect(cells).toHaveLength(4);
			byPath[cells[0]] = cells;
		}

		expect(byPath[`${home}/.omp/agent/extensions`]?.[1]).toBe("dir");
		for (const child of ["workflow", "work-now.ts", "model-bookends.ts", "model-bookends-audit.md"]) {
			expect(byPath[`${home}/.omp/agent/extensions/${child}`]?.[1]).toBe("symlink");
		}
		for (const singleton of MANAGED_SINGLETONS) {
			const entry = byPath[`${home}${singleton}`];
			expect(entry, singleton).toBeDefined();
			if (singleton.endsWith("linear-plan.md")) {
				expect(entry[1]).toBe("absent"); // retired backend artifact must stay absent
			} else {
				expect(entry[1]).toBe("symlink");
			}
		}
		for (const skill of AGENT_SKILLS) {
			expect(byPath[`${home}/.agents/skills/${skill}`]?.[1]).toBe("symlink");
		}
		for (const skill of OMP_SKILLS) {
			expect(byPath[`${home}/.omp/agent/skills/${skill}`]?.[1]).toBe("symlink");
		}
		for (const skill of CLAUDE_SKILLS) {
			expect(byPath[`${home}/.claude/skills/${skill}`]?.[1]).toBe("symlink");
		}
		for (const skill of CODEX_SKILLS) {
			expect(byPath[`${home}/.codex/skills/${skill}`]?.[1]).toBe("symlink");
		}
		for (const skill of RETIRED_SKILLS) {
			expect(byPath[`${home}/.omp/agent/skills/${skill}`]?.[1], `retired omp skill ${skill}`).toBe("absent");
			expect(byPath[`${home}/.claude/skills/${skill}`]?.[1], `retired claude skill ${skill}`).toBe("absent");
			expect(byPath[`${home}/.codex/skills/${skill}`]?.[1], `retired codex skill ${skill}`).toBe("absent");
		}
		// every managed symlink resolves into this repository checkout
		const repoRoot = join(import.meta.dir, "..", "..");
		for (const cells of Object.values(byPath)) {
			if (cells[1] === "symlink") {
				expect(cells[3].startsWith(repoRoot), `${cells[0]} -> ${cells[3]}`).toBe(true);
			}
		}
	});

	test("is byte-stable across runs and LC_ALL=C sorted", () => {
		const home = fakeHome();
		expect(runInstall(home).exitCode).toBe(0);
		const first = runInstall(home, "--print-manifest");
		const second = runInstall(home, "--print-manifest");
		expect(first.stdout).toBe(second.stdout);
		const paths = first.stdout
			.split("\n")
			.filter(Boolean)
			.map(line => line.split("\t")[0]);
		expect(paths).toEqual([...paths].sort());
	});

	test("--expect-backend work verifies without touching the live set", () => {
		const home = fakeHome();
		expect(runInstall(home, "--expect-backend", "work").exitCode).toBe(1); // nothing installed yet
		expect(runInstall(home).exitCode).toBe(0);
		const verify = runInstall(home, "--expect-backend", "work");
		expect(verify.exitCode, verify.stderr).toBe(0);
		expect(verify.stdout).toContain("backend work");
		expect(existsSync(join(home, ".omp/agent/extensions/work-now.ts"))).toBe(true);
	});
});
