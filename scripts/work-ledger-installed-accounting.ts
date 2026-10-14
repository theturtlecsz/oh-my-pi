#!/usr/bin/env bun
// Selected installed-qualification accounting run (OMP-617).
//
//   bun scripts/work-ledger-installed-accounting.ts \
//     --unit RECEIPT --out DIR --run ID --mode subset|full --release DIR --manifest-sha SHA
//
// Every flag is required. Values are not read from the environment. The command
// runs only the selected subset or full qualification targets under the skip
// accounting plugin and requires exactly one receipt. It then asks the
// skip-accounting CLI which installed-file nodes of the unit receipt stay
// pending after this invocation's selected receipt. In subset mode those
// pending ids are executed by a supplement run in DIR/supplement, then the
// pending call repeats with both receipts. The selected and supplement receipts
// of this invocation are the only alternates the final judgement may see.
// Finally merge those receipts into installed.json and record argv, ids and the
// final pending JSON in installed.provenance.json, including failed verdicts.

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
	name: "selected" | "supplement";
	mode: "subset" | "full";
	exitCode: number;
	receipts: string[];
}

/** The final skip-accounting reconciliation of the unit receipt. */
export interface InstalledAccountingPending {
	pending: Array<{ id: string; phases?: Record<string, string> }>;
	failed: Array<{ id: string; check: string; phases?: Record<string, string> }>;
	rejected: Array<{ check: string; reason: string }>;
	absent: string[];
}

export interface InstalledAccountingResult {
	exitCode: number;
	runs: InstalledAccountingRun[];
	pending: InstalledAccountingPending;
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

function emptyPending(): InstalledAccountingPending {
	return { pending: [], failed: [], rejected: [], absent: [] };
}

function refuse(reason: string): InstalledAccountingResult {
	console.error(`work-ledger-installed-accounting: ${reason}`);
	return { exitCode: 1, runs: [], pending: emptyPending() };
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

interface AccountingPaths {
	envDir: string;
	dir: string;
	basetemp: string;
}

function accountingPaths(root: string, out: string, label: string): AccountingPaths {
	const envDir = path.join(out, label);
	return { envDir, dir: path.resolve(root, envDir), basetemp: path.join(out, `${label}-state`) };
}

function pytestCommand(uv: string, targets: readonly string[], basetemp: string): string[] {
	return [
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
		...targets,
		`--basetemp=${basetemp}`,
	];
}

/** `python -m omp_work.skip_accounting pending` on the unit receipt and this invocation's alternates. */
function pendingCommand(uv: string, unit: string, alternates: readonly string[]): string[] {
	return [
		uv,
		"run",
		"--project",
		"python/omp-work",
		"--extra",
		"dev",
		"python",
		"-m",
		"omp_work.skip_accounting",
		"pending",
		"--unit",
		unit,
		...alternates.flatMap(alternate => ["--alternate", alternate]),
		"--files",
		...FULL_TARGETS,
	];
}

function reasonMap(value: unknown): Record<string, string> | undefined {
	if (typeof value !== "object" || value === null || Array.isArray(value)) return undefined;
	return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, String(entry)]));
}

/** Parse the skip-accounting CLI's stdout JSON; any missing or malformed field is an error. */
export function parsePending(stdout: string): InstalledAccountingPending | { error: string } {
	let raw: unknown;
	try {
		raw = JSON.parse(stdout.trim());
	} catch {
		return { error: "pending output is not JSON" };
	}
	if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
		return { error: "pending output is not a JSON object" };
	}
	const value = raw as Record<string, unknown>;
	for (const key of ["pending", "failed", "rejected", "absent"] as const) {
		if (!Array.isArray(value[key])) return { error: `pending output has no ${key} array` };
	}
	const pending: InstalledAccountingPending["pending"] = [];
	for (const entry of value.pending as unknown[]) {
		if (typeof entry !== "object" || entry === null || typeof (entry as { id?: unknown }).id !== "string") {
			return { error: "pending output has a malformed pending entry" };
		}
		pending.push({ id: (entry as { id: string }).id, phases: reasonMap((entry as { phases?: unknown }).phases) });
	}
	const failed: InstalledAccountingPending["failed"] = [];
	for (const entry of value.failed as unknown[]) {
		if (
			typeof entry !== "object" ||
			entry === null ||
			typeof (entry as { id?: unknown }).id !== "string" ||
			typeof (entry as { check?: unknown }).check !== "string"
		) {
			return { error: "pending output has a malformed failed entry" };
		}
		failed.push({
			id: (entry as { id: string }).id,
			check: (entry as { check: string }).check,
			phases: reasonMap((entry as { phases?: unknown }).phases),
		});
	}
	const rejected: InstalledAccountingPending["rejected"] = [];
	for (const entry of value.rejected as unknown[]) {
		if (
			typeof entry !== "object" ||
			entry === null ||
			typeof (entry as { check?: unknown }).check !== "string" ||
			typeof (entry as { reason?: unknown }).reason !== "string"
		) {
			return { error: "pending output has a malformed rejected entry" };
		}
		rejected.push({ check: (entry as { check: string }).check, reason: (entry as { reason: string }).reason });
	}
	const absent: string[] = [];
	for (const entry of value.absent as unknown[]) {
		if (typeof entry !== "string") return { error: "pending output has a malformed absent entry" };
		absent.push(entry);
	}
	return { pending, failed, rejected, absent };
}

function summarize(text: string, max = 400): string {
	const collapsed = text.replace(/\s+/g, " ").trim();
	return collapsed.length <= max ? collapsed : collapsed.slice(0, max);
}

/**
 * Run the selected installed-qualification targets, then let the skip-accounting
 * CLI decide which unit-receipt nodes stay pending. In subset mode, uncovered
 * pending ids are executed by a supplement run in DIR/supplement, and the
 * pending call repeats with both of this invocation's receipts. The final
 * pending/failed/rejected/absent lists must all be empty. Refusals return a
 * nonzero exit and an empty `runs` list without spawning.
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
	const selected = accountingPaths(root, out, "selected");
	const existing = await listReceipts(selected.dir);
	if ("error" in existing) return refuse(existing.error);
	if (existing.receipts.length > 0) return refuse(`${selected.dir} already has a ${RECEIPT_SUFFIX}`);

	const requestedUv = options.uv;
	const uv = requestedUv !== undefined && requestedUv.trim() !== "" ? requestedUv : "uv";
	const runner = options.runner ?? spawnWithCoreDumpGuard;
	const baseEnv: CommandOptions["env"] = {
		...process.env,
		OMP_INSTALLED_RELEASE: release,
		OMP_INSTALLED_MANIFEST_SHA256: manifestSha,
		OMP_ACCOUNTING_RUN: run,
		OMP_ACCOUNTING_CHECK: "installed",
		OMP_ACCOUNTING_ROOT: root,
	};
	const resolvedUnit = path.resolve(unit);
	const outputDir = path.resolve(root, out);
	await fs.mkdir(outputDir, { recursive: true });

	const runs: InstalledAccountingRun[] = [];
	const provenanceRuns: Array<{
		role: "selected" | "supplement";
		command: string[];
		ids: readonly string[];
		receipt: string | null;
		exit_code: number;
	}> = [];
	let pendingJson: unknown = emptyPending();
	const problems: string[] = [];
	let exitCode = 0;
	const fail = (code: number): void => {
		if (exitCode === 0) exitCode = code === 0 ? 1 : code;
	};

	const spawn = async (command: string[], env: CommandOptions["env"]): Promise<CommandResult> => {
		try {
			return await runner(command, { cwd: root, env });
		} catch (err) {
			return { exitCode: 1, stdout: "", stderr: err instanceof Error ? err.message : String(err) };
		}
	};

	const runQualification = async (label: "selected" | "supplement", targets: readonly string[]): Promise<string[]> => {
		const paths = accountingPaths(root, out, label);
		const command = pytestCommand(uv, targets, paths.basetemp);
		const result = await spawn(command, {
			...baseEnv,
			OMP_ACCOUNTING_DIR: paths.envDir,
		});
		const written = await listReceipts(paths.dir);
		provenanceRuns.push({
			role: label,
			command,
			ids: [...targets],
			receipt: "receipts" in written && written.receipts.length === 1 ? written.receipts[0]! : null,
			exit_code: result.exitCode,
		});
		if ("error" in written) {
			problems.push(written.error);
			fail(1);
			return [];
		}
		runs.push({ name: label, mode, exitCode: result.exitCode, receipts: written.receipts });
		if (result.exitCode !== 0) {
			const detail = result.stderr.trim();
			problems.push(
				detail
					? `${label} pytest exited ${result.exitCode}: ${summarize(detail)}`
					: `${label} pytest exited ${result.exitCode}`,
			);
			fail(result.exitCode);
		}
		if (written.receipts.length !== 1) {
			problems.push(`expected exactly one receipt in ${paths.dir}, found ${written.receipts.length}`);
			fail(1);
		}
		return written.receipts;
	};

	/** Run the pending CLI and record its judgement; a failure keeps the previous judgement. */
	const judge = async (
		alternates: readonly string[],
		fallback: InstalledAccountingPending,
	): Promise<InstalledAccountingPending> => {
		const result = await spawn(pendingCommand(uv, resolvedUnit, alternates), baseEnv);
		if (result.exitCode !== 0) {
			const detail = result.stderr.trim();
			problems.push(
				detail ? `pending exited ${result.exitCode}: ${summarize(detail)}` : `pending exited ${result.exitCode}`,
			);
			fail(result.exitCode);
		}
		const parsed = parsePending(result.stdout);
		if ("error" in parsed) {
			problems.push(parsed.error);
			fail(1);
			return fallback;
		}
		pendingJson = JSON.parse(result.stdout.trim());
		return parsed;
	};

	const finalize = async (pending: InstalledAccountingPending): Promise<InstalledAccountingResult> => {
		const merged = await spawn(
			[
				uv,
				"run",
				"--project",
				"python/omp-work",
				"--extra",
				"dev",
				"python",
				"-m",
				"omp_work.skip_accounting",
				"merge",
				"--out",
				path.join(outputDir, "installed.json"),
				...runs.flatMap(entry => entry.receipts),
			],
			baseEnv,
		);
		if (merged.exitCode !== 0) {
			fail(merged.exitCode);
			console.error(
				`work-ledger-installed-accounting: merge exited ${merged.exitCode}: ${summarize(merged.stderr)}`,
			);
		}
		await Bun.write(
			path.join(outputDir, "installed.provenance.json"),
			`${JSON.stringify(
				{
					version: 1,
					check: "installed",
					run_id: run,
					mode,
					release,
					manifest_sha256: manifestSha,
					unit: resolvedUnit,
					runs: provenanceRuns,
					pending: pendingJson,
					exit_code: exitCode,
				},
				null,
				2,
			)}\n`,
		);
		return { exitCode, runs, pending };
	};

	// 1. Selected run on the mode's targets, exactly one skip receipt.
	const selectedReceipts = await runQualification("selected", resolved.targets);
	if (selectedReceipts.length !== 1) {
		console.error(`work-ledger-installed-accounting: ${problems.join("; ")}`);
		return finalize(emptyPending());
	}

	// 2. Pending after the selected run: this invocation's selected receipt is the only alternate.
	let judged = await judge(selectedReceipts, emptyPending());

	// 3. Subset mode executes the pending ids once, then re-judges with both receipts.
	if (mode === "subset" && judged.pending.length > 0) {
		const supplementReceipts = await runQualification(
			"supplement",
			judged.pending.map(entry => entry.id),
		);
		if (supplementReceipts.length === 1) {
			judged = await judge([...selectedReceipts, ...supplementReceipts], judged);
		}
	}

	// 4. The final judgement decides: any pending, failed, rejected or absent node fails the run.
	if (judged.pending.length > 0) problems.push(`pending: ${summarize(judged.pending.map(e => e.id).join(", "))}`);
	if (judged.failed.length > 0) problems.push(`failed: ${summarize(judged.failed.map(e => e.id).join(", "))}`);
	if (judged.rejected.length > 0) {
		problems.push(`rejected: ${summarize(judged.rejected.map(e => `${e.check}: ${e.reason}`).join(", "))}`);
	}
	if (judged.absent.length > 0) problems.push(`absent: ${summarize(judged.absent.join(", "))}`);
	if (problems.length > 0) {
		fail(1);
		console.error(`work-ledger-installed-accounting: ${problems.join("; ")}`);
	} else {
		console.log(
			`work-ledger-installed-accounting: selected mode=${mode} runs=${runs.map(r => r.name).join("+")} receipt=${selectedReceipts[0]}`,
		);
	}
	return finalize(judged);
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
