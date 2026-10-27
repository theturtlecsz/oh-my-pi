import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";

interface HarnessReport {
	loaded: boolean;
	loadErrors: string[];
	startupGitHits: string[];
	tripwireControl: boolean;
	statuses: string[];
	refusalText: string;
	explicitText: string;
}

const SPAWN_TIMEOUT_MS = 20_000;

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-scope-"));
const home = path.join(tempRoot, "home");
const repo = path.join(tempRoot, "repo");
const nested = path.join(repo, "nested", "deep");
const worktree = path.join(tempRoot, "linked-worktree");
const outside = path.join(tempRoot, "outside");
const broken = path.join(tempRoot, "broken-git");
const harness = path.join(import.meta.dir, "fixtures/scope-enforcement-harness.ts");

function runGit(args: string[], cwd: string): string {
	const result = Bun.spawnSync(["git", ...args], { cwd, timeout: SPAWN_TIMEOUT_MS, killSignal: "SIGKILL" });
	if (result.exitCode !== 0) {
		throw new Error(`git ${args.join(" ")} failed (exit ${result.exitCode}): ${result.stderr.toString()}`);
	}
	return result.stdout.toString();
}

beforeAll(() => {
	fs.mkdirSync(path.join(home, ".omp", "agent"), { recursive: true });
	fs.mkdirSync(path.join(home, ".config", "omp-work"), { recursive: true });
	fs.writeFileSync(
		path.join(home, ".config", "omp-work", "client.json"),
		JSON.stringify({
			base_url: "http://127.0.0.1:54322",
			workspace_id: "00000000-0000-7000-8000-000000000001",
			owner_id: "00000000-0000-7000-8000-000000000002",
		}),
	);
	fs.writeFileSync(
		path.join(home, ".omp", "agent", "work-now.json"),
		JSON.stringify({
			backend: "work",
			issueId: "old-now-id",
			identifier: "HOME-1",
			title: "Old global NOW",
			project: "Old Global Project",
			setAt: Date.now(),
		}),
	);

	fs.mkdirSync(nested, { recursive: true });
	fs.mkdirSync(outside, { recursive: true });
	fs.mkdirSync(broken, { recursive: true });
	runGit(["init", "-q"], repo);
	runGit(["config", "user.email", "scope@example.com"], repo);
	runGit(["config", "user.name", "Scope Fixture"], repo);
	fs.writeFileSync(path.join(repo, "README.md"), "fixture\n");
	runGit(["add", "-A"], repo);
	runGit(["commit", "-qm", "fixture"], repo);
	runGit(["worktree", "add", "--detach", worktree, "HEAD"], repo);
	// An unreadable `.git` pointer (invalid UTF-8) makes native discovery throw:
	// a scope the host cannot prove must abort registration, never read "outside Git".
	fs.writeFileSync(path.join(broken, ".git"), Buffer.from([0xff, 0xfe, 0x00, 0x62, 0x72, 0x6f, 0x6b, 0x65, 0x6e]));
});

afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

function run(cwd: string): HarnessReport {
	const child = Bun.spawnSync([process.execPath, harness, cwd], {
		cwd,
		// Pin XDG_CONFIG_HOME to the temp HOME: hosted CI images export it globally,
		// and ompWorkConfigDir() prefers it over $HOME/.config (OMP-254).
		env: {
			...process.env,
			HOME: home,
			XDG_CONFIG_HOME: path.join(home, ".config"),
			OMP_WORK_BEARER: "test-token",
			PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent"),
		},
		timeout: SPAWN_TIMEOUT_MS,
		killSignal: "SIGKILL",
	});
	expect(child.exitCode, child.stderr.toString()).toBe(0);
	return JSON.parse(child.stdout.toString()) as HarnessReport;
}

/** A loadable scenario: the extension binds, so scope gating is observable. */
function expectLoaded(cwd: string): HarnessReport {
	const report = run(cwd);
	expect(report.loadErrors, report.loadErrors.join("\n")).toEqual([]);
	expect(report.loaded).toBe(true);
	// The tripwire shim really is on PATH (positive control) and scope discovery
	// spawned no git process.
	expect(report.tripwireControl).toBe(true);
	expect(report.startupGitHits).toEqual([]);
	return report;
}

describe("unscoped repo write enforcement", () => {
	test("refuses restored global NOW project but accepts an explicit project", () => {
		const report = expectLoaded(repo);
		expect(report.statuses.join("\n")).toContain("Old Global Project"); // state.project really restored
		expect(report.refusalText).toContain("refused: unscoped git repo");
		expect(report.refusalText).not.toContain("Old Global Project");
		expect(report.explicitText).toContain("CONFIRM REQUIRED");
		expect(report.explicitText).toContain("project Chosen Project");
		expect(report.explicitText).not.toContain("refused: unscoped git repo");
	}, 30_000);

	test("a nested cwd inside the repo is still scope-gated", () => {
		const report = expectLoaded(nested);
		expect(report.refusalText).toContain("refused: unscoped git repo");
		expect(report.refusalText).not.toContain("Old Global Project");
	}, 30_000);

	test("a linked worktree root is scope-gated", () => {
		const report = expectLoaded(worktree);
		expect(report.refusalText).toContain("refused: unscoped git repo");
		expect(report.refusalText).not.toContain("Old Global Project");
	}, 30_000);

	test("outside any repo the restored global NOW project remains the fallback target", () => {
		const report = expectLoaded(outside);
		expect(report.refusalText).not.toContain("refused: unscoped git repo");
		expect(report.refusalText).toContain("project Old Global Project");
	}, 30_000);

	test("a discovery exception aborts registration instead of reporting outside Git", () => {
		const report = run(broken);
		expect(report.tripwireControl).toBe(true);
		expect(report.startupGitHits).toEqual([]);
		expect(report.loaded).toBe(false);
		expect(report.loadErrors.length).toBeGreaterThan(0);
	}, 30_000);
});
