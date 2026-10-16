import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as vcs from "@oh-my-pi/pi-natives/vcs";
import { discoverAdvisorConfigs } from "../../packages/coding-agent/src/advisor/config";
import { ADVISOR_NAME, cleanupCase, loadCases, prepareCase } from "../../scripts/ecc-advisor-trial/prepare";

const repoRoot = path.resolve(import.meta.dir, "../..");
const casesPath = path.join(repoRoot, "docs/report/ecc-database-advisor-trial/cases.json");
const recordedCase = (await loadCases(casesPath))[0]!;
let tempDir: string;
let repoDir: string;
let workDir: string;
let outDir: string;
let agentDir: string;

beforeAll(async () => {
	// Stay on the source filesystem so the local clone hardlinks Git objects.
	tempDir = await fs.mkdtemp(path.join(repoRoot, ".ecc-retired-prepare-"));
	repoDir = path.join(tempDir, "repo");
	workDir = path.join(tempDir, "work");
	outDir = path.join(tempDir, "out");
	agentDir = path.join(tempDir, "agent");
	await fs.mkdir(agentDir);
	// Own the worktree registry: preparation and cleanup cannot alter the host repo.
	await vcs.clone(repoRoot, repoDir, { sha: recordedCase.commit });
});

afterEach(async () => {
	await cleanupCase({ workDir, repoDir });
	await fs.rm(outDir, { recursive: true, force: true });
});

afterAll(async () => {
	await fs.rm(tempDir, { recursive: true, force: true });
});

async function expectHistoricalPreparation(treeDir: string, promptPath: string): Promise<void> {
	const gitRepo = vcs.requireGit(repoDir);
	expect(await vcs.requireGit(treeDir).resolveRef("HEAD")).toBe(recordedCase.commit);
	expect((await gitRepo.worktrees()).some(tree => tree.path === treeDir)).toBe(true);
	for (const relative of [".omp/WATCHDOG.yml", ".omp/WATCHDOG.yaml", ".omp/ecc/adapted.lock.json"]) {
		expect(await Bun.file(path.join(treeDir, relative)).exists(), relative).toBe(false);
	}
	const discovered = await discoverAdvisorConfigs(treeDir, agentDir);
	expect(discovered.advisors.find(advisor => advisor.name === ADVISOR_NAME)).toBeUndefined();
	const promptText = await Bun.file(promptPath).text();
	expect(promptText).toContain(recordedCase.commit);
	for (const file of recordedCase.scope) expect(promptText).toContain(file);
	const diff = await gitRepo.diffText({
		base: `${recordedCase.commit}^`,
		head: recordedCase.commit,
		files: recordedCase.scope,
	});
	expect(promptText).toContain(diff);
	if (recordedCase.known_defect) expect(promptText).not.toContain(recordedCase.known_defect.ref);
}

async function expectCleaned(treeDir: string): Promise<void> {
	expect(await Bun.file(treeDir).exists()).toBe(false);
	expect((await vcs.requireGit(repoDir).worktrees()).some(tree => tree.path === treeDir)).toBe(false);
}

async function runCli(args: string[]): Promise<void> {
	// The entrypoint invokes real runPrepareCli with an isolated repository cwd.
	const child = Bun.spawn([process.execPath, path.join(repoRoot, "scripts/ecc-advisor-trial/prepare.ts"), ...args], {
		cwd: repoDir,
		stdout: "pipe",
		stderr: "pipe",
	});
	const [exitCode, stderr] = await Promise.all([child.exited, new Response(child.stderr).text()]);
	expect(exitCode, stderr).toBe(0);
}

describe("historical preparation after ECC advisor retirement", () => {
	test("prepareCase returns a historical prompt without installing ECC, then cleanup removes the worktree", async () => {
		const result = await prepareCase({ caseId: recordedCase.id, workDir, outDir, casesPath, repoDir });
		await expectHistoricalPreparation(result.treeDir, result.promptPath);
		await cleanupCase({ workDir, repoDir });
		await expectCleaned(result.treeDir);
	});

	test("runPrepareCli writes a historical prompt without installing ECC and supports cleanup", async () => {
		await runCli(["--case", recordedCase.id, "--work", workDir, "--out", outDir, "--cases", casesPath]);
		const treeDir = path.join(workDir, "tree");
		await expectHistoricalPreparation(treeDir, path.join(outDir, "prompt.md"));
		await runCli(["--cleanup", "--work", workDir]);
		await expectCleaned(treeDir);
	});
});
