import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as vcs from "@oh-my-pi/pi-natives/vcs";
import { prompt } from "@oh-my-pi/pi-utils";
import { loadWatchdogConfigFile, saveWatchdogConfigFile } from "../../packages/coding-agent/src/advisor/config";
import { ECC_ROOT } from "../../session-system/ecc/adapter/catalog";
import { runEccCli } from "../../session-system/ecc/adapter/cli";
import primaryPromptTemplate from "./primary-prompt.md" with { type: "text" };

export const ADVISOR_NAME = "ECC Database Reviewer";
export const DEFAULT_CASES_PATH = "docs/report/ecc-database-advisor-trial/cases.json";

export interface KnownDefect {
	ref: string;
	summary: string;
}

export type CaseKind = "known_bad" | "ordinary";

export interface CaseItem {
	id: string;
	commit: string;
	kind: CaseKind;
	scope: string[];
	known_defect: KnownDefect | null;
}

export interface CasePromptContext {
	commit: string;
	scope: string[];
	diff?: string;
}

/**
 * Render the primary review prompt for a case using the Handlebars template.
 * Passes only `commit`, `scope`, and `diff` into the template context to prevent
 * leaking case identity, kind, or defect details to the advisor.
 */
export function renderCasePrompt(
	caseOrContext: { commit: string; scope: string[]; diff?: string },
	diff?: string,
): string {
	const resolvedDiff = diff ?? caseOrContext.diff ?? "";
	return prompt.render(primaryPromptTemplate, {
		commit: caseOrContext.commit,
		scope: caseOrContext.scope,
		diff: resolvedDiff,
	});
}

/**
 * Enable a specific advisor entry in WATCHDOG.yml by name.
 * Only toggles `enabled: true`; preserves all other properties (tools, instructions, etc.).
 * Throws if the advisor name is not found in the config.
 */
export async function enableRosterEntry(filePath: string, name: string): Promise<void> {
	const doc = await loadWatchdogConfigFile(filePath);
	const entry = doc.advisors.find(a => a.name === name);
	if (!entry) {
		throw new Error(`Advisor roster entry "${name}" not found in ${filePath}`);
	}
	entry.enabled = true;
	await saveWatchdogConfigFile(filePath, doc);
}

/**
 * Load cases from cases.json.
 */
export async function loadCases(casesPath: string): Promise<CaseItem[]> {
	const content = await Bun.file(casesPath).text();
	return JSON.parse(content) as CaseItem[];
}

export interface PrepareCaseOptions {
	caseId: string;
	workDir: string;
	outDir: string;
	casesPath?: string;
	repoDir?: string;
	eccRoot?: string;
}

/**
 * Prepare a detached worktree at <work>/tree for a trial case, install ECC assets,
 * enable the ECC Database Reviewer roster entry, and write <out>/prompt.md.
 */
export async function prepareCase(options: PrepareCaseOptions): Promise<{ promptPath: string; treeDir: string }> {
	const repoDir = options.repoDir ?? process.cwd();
	const gitRepo = vcs.requireGit(repoDir);
	const repoRoot = gitRepo.info().repoRoot;

	let caseItem: CaseItem | undefined;
	if (options.caseId.startsWith("{")) {
		caseItem = JSON.parse(options.caseId) as CaseItem;
	} else if (options.caseId.endsWith(".json")) {
		const loaded = await loadCases(options.caseId);
		caseItem = loaded[0];
	} else {
		const casesPath = options.casesPath ? path.resolve(options.casesPath) : path.join(repoRoot, DEFAULT_CASES_PATH);
		const cases = await loadCases(casesPath);
		caseItem = cases.find(c => c.id === options.caseId);
		if (!caseItem) {
			throw new Error(`Case "${options.caseId}" not found in ${casesPath}`);
		}
	}

	const workDir = path.resolve(options.workDir);
	await fs.mkdir(workDir, { recursive: true });
	const treeDir = path.join(workDir, "tree");

	try {
		await gitRepo.worktreeRemove(treeDir, true);
	} catch {
		// Ignore if treeDir did not exist
	}
	await gitRepo.worktreePrune();
	await fs.rm(treeDir, { recursive: true, force: true }).catch(() => {});

	await gitRepo.worktreeAdd(treeDir, caseItem.commit, { detach: true, clone: false });

	const eccRoot = options.eccRoot ? path.resolve(options.eccRoot) : ECC_ROOT;
	await runEccCli({ command: "install", project: treeDir, eccRoot });

	const watchdogPath = path.join(treeDir, ".omp", "WATCHDOG.yml");
	await enableRosterEntry(watchdogPath, ADVISOR_NAME);

	const diff = await gitRepo.diffText({
		base: `${caseItem.commit}^`,
		head: caseItem.commit,
		files: caseItem.scope,
	});

	const promptText = renderCasePrompt(caseItem, diff);

	const outDir = path.resolve(options.outDir);
	await fs.mkdir(outDir, { recursive: true });
	const promptPath = path.join(outDir, "prompt.md");
	await Bun.write(promptPath, promptText);

	return { promptPath, treeDir };
}

/**
 * Remove a detached worktree at <work>/tree and prune git worktree records.
 */
export async function cleanupCase(options: { workDir: string; repoDir?: string }): Promise<void> {
	const repoDir = options.repoDir ?? process.cwd();
	const gitRepo = vcs.requireGit(repoDir);
	const treeDir = path.join(path.resolve(options.workDir), "tree");
	try {
		await gitRepo.worktreeRemove(treeDir, true);
	} catch {
		// Ignore if already removed
	}
	await gitRepo.worktreePrune();
	await fs.rm(treeDir, { recursive: true, force: true }).catch(() => {});
}

interface CliOptions {
	caseId?: string;
	workDir?: string;
	outDir?: string;
	casesPath?: string;
	cleanup: boolean;
}

export function parsePrepareCliArgs(args: string[]): CliOptions {
	const options: CliOptions = { cleanup: false };
	for (let i = 0; i < args.length; i++) {
		const arg = args[i]!;
		if (arg === "--cleanup") {
			options.cleanup = true;
		} else if (arg === "--case" || arg.startsWith("--case=")) {
			const val = arg.startsWith("--case=") ? arg.slice("--case=".length) : args[++i];
			if (!val) throw new Error("--case requires a value");
			options.caseId = val;
		} else if (arg === "--work" || arg.startsWith("--work=")) {
			const val = arg.startsWith("--work=") ? arg.slice("--work=".length) : args[++i];
			if (!val) throw new Error("--work requires a value");
			options.workDir = val;
		} else if (arg === "--out" || arg.startsWith("--out=")) {
			const val = arg.startsWith("--out=") ? arg.slice("--out=".length) : args[++i];
			if (!val) throw new Error("--out requires a value");
			options.outDir = val;
		} else if (arg === "--cases" || arg.startsWith("--cases=")) {
			const val = arg.startsWith("--cases=") ? arg.slice("--cases=".length) : args[++i];
			if (!val) throw new Error("--cases requires a value");
			options.casesPath = val;
		} else {
			throw new Error(`Unknown argument: ${arg}`);
		}
	}
	return options;
}

export async function runPrepareCli(args: string[]): Promise<void> {
	const options = parsePrepareCliArgs(args);
	if (options.cleanup) {
		if (!options.workDir) {
			throw new Error("--cleanup requires --work <dir>");
		}
		await cleanupCase({ workDir: options.workDir });
		return;
	}
	if (!options.caseId) {
		throw new Error("--case is required");
	}
	if (!options.workDir) {
		throw new Error("--work is required");
	}
	if (!options.outDir) {
		throw new Error("--out is required");
	}
	await prepareCase({
		caseId: options.caseId,
		workDir: options.workDir,
		outDir: options.outDir,
		casesPath: options.casesPath,
	});
}

if (import.meta.main) {
	try {
		await runPrepareCli(process.argv.slice(2));
	} catch (err) {
		process.stderr.write(`${err instanceof Error ? err.message : String(err)}\n`);
		process.exit(1);
	}
}
