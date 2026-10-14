#!/usr/bin/env bun
// Selected installed-qualification accounting run (OMP-617).
//
//   bun scripts/work-ledger-installed-accounting.ts \
//     --unit RECEIPT --out DIR --run ID --mode subset|full --release DIR --manifest-sha SHA
//
// Every flag is required. Values are not read from the environment. The command
// runs only the selected subset or full qualification targets under the skip
// accounting plugin and requires exactly one receipt.

import type { Dirent } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import {
	type CommandOptions,
	type CommandResult,
	type CommandRunner,
	spawnWithCoreDumpGuard,
} from "../session-system/qualify-installed.ts";
import { FULL_TARGETS, SUBSET_TARGETS, selectQualificationMode } from "./installed-qualification-mode.ts";

const repoRoot = path.resolve(import.meta.dir, "..");
const RECEIPT_SUFFIX = ".skip-receipt.json";

export interface InstalledAccountingOptions {
	unit: string;
	out: string;
	run: string;
	mode: string;
	release: string;
	manifestSha: string;
	root?: string;
	runner?: CommandRunner;
	uv?: string;
}

export interface InstalledAccountingRun {
	name: "selected";
	mode: "subset" | "full";
	exitCode: number;
	receipts: string[];
}

export interface InstalledAccountingResult {
	exitCode: number;
	runs: InstalledAccountingRun[];
}

interface FlagValues {
	unit: string;
	out: string;
	run: string;
	mode: string;
	release: string;
	manifestSha: string;
}

const FLAG_FIELDS = {
	"--unit": "unit",
	"--out": "out",
	"--run": "run",
	"--mode": "mode",
	"--release": "release",
	"--manifest-sha": "manifestSha",
} as const;

type AccountingFlag = keyof typeof FLAG_FIELDS;

function isFlag(argument: string): argument is AccountingFlag {
	return Object.hasOwn(FLAG_FIELDS, argument);
}

function blank(value: string | undefined): boolean {
	return typeof value !== "string" || value.trim() === "";
}

function refuse(reason: string): InstalledAccountingResult {
	console.error(`work-ledger-installed-accounting: ${reason}`);
	return { exitCode: 1, runs: [] };
}

function sameList(left: readonly string[], right: readonly string[]): boolean {
	return left.length === right.length && left.every((item, index) => item === right[index]);
}

/** Map CLI flags to option fields. Omitted flags stay empty; the environment is not consulted. */
export function parseInstalledAccountingArgs(argv: readonly string[]): FlagValues | { error: string } {
	const values: FlagValues = { unit: "", out: "", run: "", mode: "", release: "", manifestSha: "" };
	const seen = new Set<AccountingFlag>();
	for (let index = 0; index < argv.length; index++) {
		const argument = argv[index] ?? "";
		if (!isFlag(argument)) return { error: `unknown argument ${argument}` };
		if (seen.has(argument)) return { error: `duplicate ${argument}` };
		const value = argv[index + 1];
		if (value === undefined || isFlag(value)) return { error: `missing value for ${argument}` };
		seen.add(argument);
		values[FLAG_FIELDS[argument]] = value;
		index++;
	}
	return values;
}

function resolveTargets(mode: "subset" | "full"): { targets: readonly string[] } | { error: string } {
	const selected =
		mode === "full"
			? selectQualificationMode({ event: "workflow_dispatch" })
			: selectQualificationMode({ event: "pull_request", changedFiles: [] });
	if (selected.mode !== mode) return { error: `qualification mode is ${selected.mode}, not ${mode}` };
	const expected = mode === "full" ? FULL_TARGETS : SUBSET_TARGETS;
	if (!sameList(selected.targets, expected)) {
		return { error: `qualification targets do not match ${mode === "full" ? "FULL_TARGETS" : "SUBSET_TARGETS"}` };
	}
	return { targets: selected.targets };
}

async function listReceipts(dir: string): Promise<{ receipts: string[] } | { error: string }> {
	let entries: Dirent[];
	try {
		entries = await fs.readdir(dir, { withFileTypes: true });
	} catch (err) {
		const code = (err as NodeJS.ErrnoException).code;
		if (code === "ENOENT") return { receipts: [] };
		return { error: `cannot read ${dir}: ${err instanceof Error ? err.message : String(err)}` };
	}
	const receipts = entries
		.filter(entry => (entry.isFile() || entry.isSymbolicLink()) && entry.name.endsWith(RECEIPT_SUFFIX))
		.map(entry => entry.name)
		.sort()
		.map(name => path.join(dir, name));
	return { receipts };
}

async function readUnitRunId(unit: string): Promise<{ runId: string } | { error: string }> {
	let text: string;
	try {
		text = await fs.readFile(unit, "utf8");
	} catch (err) {
		return { error: `unit receipt unreadable: ${err instanceof Error ? err.message : String(err)}` };
	}
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch {
		return { error: "unit receipt is not JSON" };
	}
	if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
		return { error: "unit receipt is not a JSON object with run_id" };
	}
	const runId = (parsed as { run_id?: unknown }).run_id;
	if (typeof runId !== "string" || runId.length === 0) return { error: "unit receipt has no run_id" };
	return { runId };
}

function selectedPaths(root: string, out: string): { envDir: string; dir: string; basetemp: string } {
	const envDir = path.join(out, "selected");
	return {
		envDir,
		dir: path.resolve(root, envDir),
		basetemp: path.join(out, "selected-state"),
	};
}

/**
 * Run the selected installed-qualification pytest targets and require one skip receipt.
 * Refusals return a nonzero exit and an empty `runs` list without spawning.
 */
export async function runInstalledAccounting(options: InstalledAccountingOptions): Promise<InstalledAccountingResult> {
	const unit = options.unit;
	const out = options.out;
	const run = options.run;
	const mode = options.mode;
	const release = options.release;
	const manifestSha = options.manifestSha;
	if (blank(unit) || blank(out) || blank(run) || blank(mode) || blank(release) || blank(manifestSha)) {
		return refuse("missing or empty --unit, --out, --run, --mode, --release, or --manifest-sha");
	}
	if (mode !== "subset" && mode !== "full") return refuse(`unknown mode ${mode}`);

	const resolved = resolveTargets(mode);
	if ("error" in resolved) return refuse(resolved.error);

	const unitRun = await readUnitRunId(unit);
	if ("error" in unitRun) return refuse(unitRun.error);
	if (unitRun.runId !== run) return refuse(`--run ${run} does not match unit run_id ${unitRun.runId}`);

	const requestedRoot = options.root;
	const root = path.resolve(requestedRoot !== undefined && requestedRoot.trim() !== "" ? requestedRoot : repoRoot);
	const paths = selectedPaths(root, out);
	const existing = await listReceipts(paths.dir);
	if ("error" in existing) return refuse(existing.error);
	if (existing.receipts.length > 0) return refuse(`${paths.dir} already has a ${RECEIPT_SUFFIX}`);

	const requestedUv = options.uv;
	const uv = requestedUv !== undefined && requestedUv.trim() !== "" ? requestedUv : "uv";
	const command = [
		uv,
		"run",
		"--project",
		"python/omp-work",
		"--extra",
		"dev",
		"pytest",
		"-n",
		"3",
		"-p",
		"omp_work.skip_accounting",
		...resolved.targets,
		`--basetemp=${paths.basetemp}`,
	];
	const env: CommandOptions["env"] = {
		...process.env,
		OMP_INSTALLED_RELEASE: release,
		OMP_INSTALLED_MANIFEST_SHA256: manifestSha,
		OMP_ACCOUNTING_DIR: paths.envDir,
		OMP_ACCOUNTING_RUN: run,
		OMP_ACCOUNTING_CHECK: "installed",
		OMP_ACCOUNTING_ROOT: root,
	};
	const runner = options.runner ?? spawnWithCoreDumpGuard;
	let pytest: CommandResult;
	try {
		pytest = await runner(command, { cwd: root, env });
	} catch (err) {
		pytest = { exitCode: 1, stdout: "", stderr: err instanceof Error ? err.message : String(err) };
	}

	const written = await listReceipts(paths.dir);
	if ("error" in written) {
		console.error(`work-ledger-installed-accounting: ${written.error}`);
		const exitCode = pytest.exitCode === 0 ? 1 : pytest.exitCode;
		return { exitCode, runs: [{ name: "selected", mode, exitCode, receipts: [] }] };
	}

	const problems: string[] = [];
	if (pytest.exitCode !== 0) {
		const detail = pytest.stderr.trim();
		problems.push(
			detail ? `pytest exited ${pytest.exitCode}: ${detail.slice(0, 400)}` : `pytest exited ${pytest.exitCode}`,
		);
	}
	if (written.receipts.length !== 1) {
		problems.push(`expected exactly one receipt in ${paths.dir}, found ${written.receipts.length}`);
	}
	const exitCode = pytest.exitCode !== 0 ? pytest.exitCode : written.receipts.length === 1 ? 0 : 1;
	if (problems.length > 0) console.error(`work-ledger-installed-accounting: ${problems.join("; ")}`);
	else console.log(`work-ledger-installed-accounting: selected mode=${mode} receipt=${written.receipts[0]}`);
	return { exitCode, runs: [{ name: "selected", mode, exitCode, receipts: written.receipts }] };
}

export async function runInstalledAccountingCli(argv: readonly string[]): Promise<InstalledAccountingResult> {
	const parsed = parseInstalledAccountingArgs(argv);
	if ("error" in parsed) return refuse(parsed.error);
	return runInstalledAccounting(parsed);
}

if (import.meta.main) {
	const result = await runInstalledAccountingCli(process.argv.slice(2));
	process.exit(result.exitCode);
}
