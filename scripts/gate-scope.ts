#!/usr/bin/env bun
import { $ } from "bun";
import * as fs from "node:fs/promises";

export type GateScopeGroup = "python-ledger" | "ts-check";

export const GATE_SCOPE_GROUPS: readonly GateScopeGroup[] = ["python-ledger", "ts-check"] as const;

export interface GateScopeGroupResult {
	group: GateScopeGroup;
	run: boolean;
	reason: string;
}

export type GateScopeResult = GateScopeGroupResult[] & {
	"python-ledger": GateScopeGroupResult;
	"ts-check": GateScopeGroupResult;
};

const TS_CHECK_FORCE_EXTENSIONS: readonly string[] = [
	".ts",
	".tsx",
	".js",
	".mjs",
	".cjs",
	".json",
	".jsonc",
] as const;

function normalizePath(filePath: string): string {
	return filePath.trim().replace(/\\/g, "/").replace(/^\.\//, "");
}

export function isSkipSafePythonLedger(filePath: string): boolean {
	const p = normalizePath(filePath);
	if (!p) return false;

	if (p === "docs/upstream-fork-inventory.tsv") {
		return true;
	}

	if (p === "crates" || p.startsWith("crates/")) {
		return true;
	}

	if (p === "evals" || p.startsWith("evals/")) {
		return true;
	}

	if (p === "scripts" || p.startsWith("scripts/")) {
		if (p === "scripts/release.ts") {
			return false;
		}
		return true;
	}

	if (p === "packages" || p.startsWith("packages/")) {
		if (p === "packages/work-client" || p.startsWith("packages/work-client/")) {
			return false;
		}
		if (p === "packages/coding-agent/src/cli.ts" || p === "packages/coding-agent/src/index.ts") {
			return false;
		}
		return true;
	}

	return false;
}

export function isSkipSafeTsCheck(filePath: string): boolean {
	const p = normalizePath(filePath);
	if (!p) return false;

	const isCandidate =
		p === "python" ||
		p.startsWith("python/") ||
		p === "docs" ||
		p.startsWith("docs/") ||
		(!p.includes("/") && p.endsWith(".md"));

	if (!isCandidate) {
		return false;
	}

	if (p === "python/robomp/web" || p.startsWith("python/robomp/web/")) {
		return false;
	}

	const lower = p.toLowerCase();
	if (TS_CHECK_FORCE_EXTENSIONS.some(ext => lower.endsWith(ext))) {
		return false;
	}

	return true;
}

export function selectGateScope(changedFiles: string[]): GateScopeResult {
	const normalizedFiles = changedFiles
		.map(f => normalizePath(f))
		.filter(f => f.length > 0);

	if (normalizedFiles.length === 0) {
		const pythonLedgerResult: GateScopeGroupResult = {
			group: "python-ledger",
			run: true,
			reason: "no changed files",
		};
		const tsCheckResult: GateScopeGroupResult = {
			group: "ts-check",
			run: true,
			reason: "no changed files",
		};
		const res = [pythonLedgerResult, tsCheckResult] as GateScopeResult;
		res["python-ledger"] = pythonLedgerResult;
		res["ts-check"] = tsCheckResult;
		return res;
	}

	let pythonLedgerForcing: string | null = null;
	for (const file of normalizedFiles) {
		if (!isSkipSafePythonLedger(file)) {
			pythonLedgerForcing = file;
			break;
		}
	}

	let tsCheckForcing: string | null = null;
	for (const file of normalizedFiles) {
		if (!isSkipSafeTsCheck(file)) {
			tsCheckForcing = file;
			break;
		}
	}

	const pythonLedgerResult: GateScopeGroupResult = pythonLedgerForcing
		? { group: "python-ledger", run: true, reason: pythonLedgerForcing }
		: { group: "python-ledger", run: false, reason: "all files skip-safe" };

	const tsCheckResult: GateScopeGroupResult = tsCheckForcing
		? { group: "ts-check", run: true, reason: tsCheckForcing }
		: { group: "ts-check", run: false, reason: "all files skip-safe" };

	const res = [pythonLedgerResult, tsCheckResult] as GateScopeResult;
	res["python-ledger"] = pythonLedgerResult;
	res["ts-check"] = tsCheckResult;
	return res;
}

export async function changedFiles(base: string, cwd?: string): Promise<string[]> {
	const trimmedBase = base.trim();
	if (!trimmedBase) {
		throw new Error("Base revision is required");
	}
	const dir = cwd ?? process.cwd();
	const result = await $`git diff --no-renames --name-only ${trimmedBase}...HEAD`.cwd(dir).quiet().nothrow();
	if (result.exitCode !== 0) {
		const stderr = result.stderr.toString().trim();
		throw new Error(`git diff failed with exit code ${result.exitCode}${stderr ? `: ${stderr}` : ""}`);
	}
	return result
		.text()
		.split("\n")
		.map(line => line.trim())
		.filter(line => line.length > 0);
}

export async function runCli(): Promise<void> {
	const base = process.env.GATE_SCOPE_BASE ?? Bun.env.GATE_SCOPE_BASE;
	if (!base || base.trim().length === 0) {
		console.error("GATE_SCOPE_BASE is required");
		process.exit(1);
	}

	const cwd = process.env.GATE_SCOPE_CWD ?? Bun.env.GATE_SCOPE_CWD ?? process.cwd();

	let files: string[];
	try {
		files = await changedFiles(base, cwd);
	} catch (err) {
		console.error((err as Error).message);
		process.exit(1);
	}

	const results = selectGateScope(files);

	for (const res of results) {
		const action = res.run ? "run" : "skip";
		console.log(`gate scope: ${action} ${res.group} (${res.reason})`);
	}

	const gateScopeOutput = process.env.GATE_SCOPE_OUTPUT ?? Bun.env.GATE_SCOPE_OUTPUT;
	if (gateScopeOutput) {
		try {
			const lines = results.map(res => `${res.group}=${res.run ? "run" : "skip"}\n`).join("");
			await fs.appendFile(gateScopeOutput, lines, "utf8");
		} catch (err) {
			console.error(`Failed to write to GATE_SCOPE_OUTPUT (${gateScopeOutput}): ${(err as Error).message}`);
			process.exit(1);
		}
	}
}

if (import.meta.main) {
	await runCli();
}
