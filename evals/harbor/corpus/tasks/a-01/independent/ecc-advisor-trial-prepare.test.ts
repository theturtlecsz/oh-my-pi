import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import * as vcs from "@oh-my-pi/pi-natives/vcs";
import { discoverAdvisorConfigs } from "../../packages/coding-agent/src/advisor/config";
import {
	ADVISOR_NAME,
	cleanupCase,
	enableRosterEntry,
	loadCases,
	prepareCase,
	renderCasePrompt,
	runPrepareCli,
} from "../../scripts/ecc-advisor-trial/prepare";
import { ECC_ROOT } from "../ecc/adapter/catalog";
import { runEccCli } from "../ecc/adapter/cli";

const repoRoot = path.resolve(import.meta.dir, "../..");
const casesPath = path.join(repoRoot, "docs/report/ecc-database-advisor-trial/cases.json");
const tempDirs: string[] = [];
const worktreesToClean: string[] = [];

afterEach(async () => {
	const gitRepo = vcs.requireGit(repoRoot);
	for (const wt of worktreesToClean.splice(0)) {
		try {
			await gitRepo.worktreeRemove(wt, true);
		} catch {
			// Ignore if already removed
		}
	}
	await gitRepo.worktreePrune();
	for (const dir of tempDirs.splice(0)) {
		await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
	}
});

async function makeTempDir(prefix: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

describe("ecc database advisor trial prepare", () => {
	test("prompt has commit, scope, diff, read step, no id/kind/defect", async () => {
		const cases = await loadCases(casesPath);
		expect(cases.length).toBeGreaterThanOrEqual(5);

		const sampleDiff = "diff --git a/test.py b/test.py\n+new line\n";

		for (const c of cases) {
			const prompt = renderCasePrompt(c, sampleDiff);

			expect(prompt).toContain(c.commit);
			expect(prompt).toContain(sampleDiff);
			for (const file of c.scope) {
				expect(prompt).toContain(file);
			}

			// Read step and instruction checks
			expect(prompt.toLowerCase()).toContain("read");
			expect(prompt.toLowerCase()).toContain("no edit");

			// Must not leak case ID, classification kind, or defect details
			expect(prompt).not.toContain(c.id);
			expect(prompt).not.toContain(c.kind);
			if (c.known_defect) {
				expect(prompt).not.toContain(c.known_defect.ref);
				expect(prompt).not.toContain(c.known_defect.summary);
			}
		}
	});

	test("discoverAdvisorConfigs sees entry enabled, tools kept", async () => {
		const tempDir = await makeTempDir("ecc-prep-advisor-");
		const treeDir = path.join(tempDir, "tree");
		const agentDir = path.join(tempDir, "agent");
		await fs.mkdir(agentDir, { recursive: true });

		const gitRepo = vcs.requireGit(repoRoot);
		const cases = await loadCases(casesPath);
		const firstCase = cases[0]!;

		worktreesToClean.push(treeDir);
		await gitRepo.worktreeAdd(treeDir, firstCase.commit, { detach: true, clone: false });

		await runEccCli({ command: "install", project: treeDir, eccRoot: ECC_ROOT });

		const watchdogPath = path.join(treeDir, ".omp", "WATCHDOG.yml");

		// Initially, the installed advisor should be present but disabled
		const initialDiscovered = await discoverAdvisorConfigs(treeDir, agentDir);
		const initialAdvisor = initialDiscovered.advisors.find(a => a.name === ADVISOR_NAME);
		expect(initialAdvisor).toBeDefined();
		expect(initialAdvisor?.enabled).toBe(false);

		// Enable the entry
		await enableRosterEntry(watchdogPath, ADVISOR_NAME);

		// Now discoverAdvisorConfigs should see the entry enabled with tools preserved
		const discovered = await discoverAdvisorConfigs(treeDir, agentDir);
		const advisor = discovered.advisors.find(a => a.name === ADVISOR_NAME);
		expect(advisor).toBeDefined();
		expect(advisor?.enabled).toBe(true);
		expect(advisor?.tools).toEqual(["read", "grep", "glob"]);
	});

	test("absent throws", async () => {
		const tempDir = await makeTempDir("ecc-absent-");
		const treeDir = path.join(tempDir, "tree");

		const gitRepo = vcs.requireGit(repoRoot);
		const cases = await loadCases(casesPath);
		const firstCase = cases[0]!;

		worktreesToClean.push(treeDir);
		await gitRepo.worktreeAdd(treeDir, firstCase.commit, { detach: true, clone: false });

		await runEccCli({ command: "install", project: treeDir, eccRoot: ECC_ROOT });
		const watchdogPath = path.join(treeDir, ".omp", "WATCHDOG.yml");

		// Non-existent advisor entry in existing file
		await expect(enableRosterEntry(watchdogPath, "NonExistentAdvisor")).rejects.toThrow(
			'Advisor roster entry "NonExistentAdvisor" not found',
		);

		// Missing config file
		await expect(
			enableRosterEntry(path.join(tempDir, "nonexistent", "WATCHDOG.yml"), ADVISOR_NAME),
		).rejects.toThrow();
	});

	test("commits are HEAD ancestors changing scope", async () => {
		const gitRepo = vcs.requireGit(repoRoot);
		const cases = await loadCases(casesPath);

		for (const c of cases) {
			const mergeBase = await gitRepo.mergeBase(c.commit, "HEAD");
			expect(mergeBase).toBe(c.commit);

			const changed = await gitRepo.changedFiles({ base: `${c.commit}^`, head: c.commit });
			for (const file of c.scope) {
				expect(changed).toContain(file);
			}
		}
	});

	test("cleanup leaves no worktree", async () => {
		const tempDir = await makeTempDir("ecc-cleanup-");
		const workDir = path.join(tempDir, "work");
		const outDir = path.join(tempDir, "out");
		const treeDir = path.join(workDir, "tree");

		worktreesToClean.push(treeDir);

		// Run prepareCase
		const result = await prepareCase({
			caseId: "k1",
			workDir,
			outDir,
			casesPath,
			repoDir: repoRoot,
		});

		expect(result.treeDir).toBe(treeDir);
		expect(await Bun.file(result.promptPath).exists()).toBe(true);
		expect(await Bun.file(path.join(treeDir, "package.json")).exists()).toBe(true);

		const gitRepo = vcs.requireGit(repoRoot);
		const worktreesBefore = await gitRepo.worktrees();
		expect(worktreesBefore.some(w => w.path === treeDir)).toBe(true);

		// Run cleanupCase
		await cleanupCase({ workDir, repoDir: repoRoot });

		const worktreesAfter = await gitRepo.worktrees();
		expect(worktreesAfter.some(w => w.path === treeDir)).toBe(false);
		expect(await Bun.file(treeDir).exists()).toBe(false);
	});

	test("rejects occupied destination and preserves existing data", async () => {
		const tempDir = await makeTempDir("ecc-preservation-");
		const workDir = path.join(tempDir, "work");
		const outDir = path.join(tempDir, "out");
		const treeDir = path.join(workDir, "tree");

		await fs.mkdir(treeDir, { recursive: true });
		const sentinelFile = path.join(treeDir, "uncommitted-work.txt");
		const sentinelContent = "important uncommitted user data";
		await Bun.write(sentinelFile, sentinelContent);

		// Calling prepareCase on an occupied tree directory must reject
		await expect(
			prepareCase({
				caseId: "k1",
				workDir,
				outDir,
				casesPath,
				repoDir: repoRoot,
			}),
		).rejects.toThrow(/already exists; explicit cleanup required/i);

		// The existing user data must still be intact
		expect(await Bun.file(sentinelFile).exists()).toBe(true);
		expect(await Bun.file(sentinelFile).text()).toBe(sentinelContent);

		// Explicit cleanup removes the directory
		await cleanupCase({ workDir, repoDir: repoRoot });
		expect(await Bun.file(sentinelFile).exists()).toBe(false);
		expect(await Bun.file(treeDir).exists()).toBe(false);

		// Now prepareCase succeeds
		worktreesToClean.push(treeDir);
		const prepared = await prepareCase({
			caseId: "k1",
			workDir,
			outDir,
			casesPath,
			repoDir: repoRoot,
		});
		expect(await Bun.file(prepared.promptPath).exists()).toBe(true);
		expect(await Bun.file(path.join(treeDir, "package.json")).exists()).toBe(true);

		// Second call to prepareCase without cleanup must reject and preserve existing worktree
		await expect(
			prepareCase({
				caseId: "k2",
				workDir,
				outDir,
				casesPath,
				repoDir: repoRoot,
			}),
		).rejects.toThrow(/already exists; explicit cleanup required/i);
		expect(await Bun.file(path.join(treeDir, "package.json")).exists()).toBe(true);

		// Explicit cleanup removes the worktree
		await cleanupCase({ workDir, repoDir: repoRoot });
		expect(await Bun.file(treeDir).exists()).toBe(false);
	});

	test("CLI --case --work --out and --cleanup --work", async () => {
		const tempDir = await makeTempDir("ecc-cli-test-");
		const workDir = path.join(tempDir, "work");
		const outDir = path.join(tempDir, "out");
		const treeDir = path.join(workDir, "tree");

		worktreesToClean.push(treeDir);

		await runPrepareCli(["--case", "k2", "--work", workDir, "--out", outDir, "--cases", casesPath]);

		const promptPath = path.join(outDir, "prompt.md");
		expect(await Bun.file(promptPath).exists()).toBe(true);
		const promptText = await Bun.file(promptPath).text();
		expect(promptText).toContain("44a2f9446f00786188e17567f5eaee79aa020658");
		expect(promptText).toContain("python/omp-work/src/omp_work/jobs/usage.py");

		const gitRepo = vcs.requireGit(repoRoot);
		const wtsBefore = await gitRepo.worktrees();
		expect(wtsBefore.some(w => w.path === treeDir)).toBe(true);

		await runPrepareCli(["--cleanup", "--work", workDir]);

		const wtsAfter = await gitRepo.worktrees();
		expect(wtsAfter.some(w => w.path === treeDir)).toBe(false);
		expect(await Bun.file(treeDir).exists()).toBe(false);
	});
});
