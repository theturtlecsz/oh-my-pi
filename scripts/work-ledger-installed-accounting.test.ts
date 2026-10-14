import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { CommandOptions, CommandResult, CommandRunner } from "../session-system/qualify-installed.ts";
import { FULL_TARGETS, SUBSET_TARGETS, selectQualificationMode } from "./installed-qualification-mode.ts";
import {
	parseInstalledAccountingArgs,
	runInstalledAccounting,
	runInstalledAccountingCli,
	type InstalledAccountingOptions,
} from "./work-ledger-installed-accounting.ts";

const ZERO_SHA = "0000000000000000000000000000000000000000";

describe("installed qualification targets", () => {
	test("full run covers isolation, execution recovery and lifetime files", () => {
		expect([...FULL_TARGETS]).toEqual([
			"python/omp-work/tests/test_installed_runtime_isolation.py",
			"python/omp-work/tests/test_installed_execution_recovery.py",
			"python/omp-work/tests/test_installed_runtime_lifetime.py",
		]);
	});

	const fullCases: Array<[string, Parameters<typeof selectQualificationMode>[0]]> = [
		["isRelease", { isRelease: true, event: "pull_request" }],
		["release event", { event: "release" }],
		["schedule", { event: "schedule" }],
		["workflow_dispatch", { event: "workflow_dispatch" }],
		["all-zero push", { event: "push", before: ZERO_SHA }],
		[
			"protected path",
			{ event: "pull_request", changedFiles: ["python/omp-work/tests/test_installed_runtime_lifetime.py"] },
		],
	];

	for (const [name, options] of fullCases) {
		test(`${name} selects every full target`, () => {
			const result = selectQualificationMode(options);
			expect(result.mode).toBe("full");
			expect(result.targets).toEqual([...FULL_TARGETS]);
		});
	}

	test("non-protected pull_request selects exactly the five subset ids", () => {
		const result = selectQualificationMode({ event: "pull_request", changedFiles: ["README.md"] });
		expect(result.mode).toBe("subset");
		expect(result.targets).toEqual([...SUBSET_TARGETS]);
		expect(result.targets).toHaveLength(5);
	});
});

const repoRoot = path.resolve(import.meta.dir, "..");
const tempDirs: string[] = [];

afterEach(async () => {
	await Promise.all(tempDirs.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

interface Fixture {
	root: string;
	out: string;
	unit: string;
	release: string;
	run: string;
	manifestSha: string;
}

interface RecordedCall {
	command: readonly string[];
	options: CommandOptions;
}

async function fixture(runId = "run-617"): Promise<Fixture> {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "omp-installed-accounting-"));
	tempDirs.push(root);
	const out = path.join(root, "out");
	const release = path.join(root, "release");
	await fs.mkdir(out);
	await fs.mkdir(release);
	const unit = path.join(root, "unit.json");
	await fs.writeFile(unit, `${JSON.stringify({ version: 1, run_id: runId })}\n`);
	return { root, out, unit, release, run: runId, manifestSha: "a".repeat(64) };
}

function input(fix: Fixture, overrides: Partial<InstalledAccountingOptions> = {}): InstalledAccountingOptions {
	return {
		unit: fix.unit,
		out: fix.out,
		run: fix.run,
		mode: "full",
		release: fix.release,
		manifestSha: fix.manifestSha,
		root: fix.root,
		...overrides,
	};
}

function recordingRunner(
	behavior: (env: CommandOptions["env"]) => Promise<CommandResult> | CommandResult = () => ({
		exitCode: 0,
		stdout: "",
		stderr: "",
	}),
): { calls: RecordedCall[]; runner: CommandRunner } {
	const calls: RecordedCall[] = [];
	const runner: CommandRunner = async (command, options) => {
		calls.push({ command, options });
		return behavior(options.env);
	};
	return { calls, runner };
}

async function writeReceipts(dir: string | undefined, count: number): Promise<void> {
	if (!dir) throw new Error("OMP_ACCOUNTING_DIR was not set");
	await fs.mkdir(dir, { recursive: true });
	for (let index = 0; index < count; index++) {
		await fs.writeFile(path.join(dir, `installed-${index}.skip-receipt.json`), "{}\n");
	}
}

function expectedCommand(mode: "subset" | "full", out: string, uv = "uv"): string[] {
	const selected =
		mode === "full"
			? selectQualificationMode({ event: "workflow_dispatch" })
			: selectQualificationMode({ event: "pull_request", changedFiles: [] });
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
		...selected.targets,
		`--basetemp=${path.join(out, "selected-state")}`,
	];
}

function expectedEnv(fix: Fixture, accountingRoot: string): NodeJS.ProcessEnv {
	return {
		...process.env,
		OMP_INSTALLED_RELEASE: fix.release,
		OMP_INSTALLED_MANIFEST_SHA256: fix.manifestSha,
		OMP_ACCOUNTING_DIR: path.join(fix.out, "selected"),
		OMP_ACCOUNTING_RUN: fix.run,
		OMP_ACCOUNTING_CHECK: "installed",
		OMP_ACCOUNTING_ROOT: accountingRoot,
	};
}

describe("installed accounting selected run", () => {
	test("full mode runs workflow_dispatch targets with the installed accounting environment", async () => {
		const fix = await fixture();
		await fs.writeFile(path.join(fix.out, "stray.skip-receipt.json"), "{}\n");
		const { calls, runner } = recordingRunner(async env => {
			await fs.mkdir(env.OMP_ACCOUNTING_DIR ?? "", { recursive: true });
			await fs.writeFile(path.join(env.OMP_ACCOUNTING_DIR ?? "", "notes.json"), "ignore\n");
			await writeReceipts(env.OMP_ACCOUNTING_DIR, 1);
			return { exitCode: 0, stdout: "", stderr: "" };
		});
		const envBefore = { ...process.env };

		const result = await runInstalledAccounting(input(fix, { mode: "full", root: undefined, runner }));

		expect(result.exitCode).toBe(0);
		expect(result.runs).toEqual([
			{
				name: "selected",
				mode: "full",
				exitCode: 0,
				receipts: [path.join(fix.out, "selected", "installed-0.skip-receipt.json")],
			},
		]);
		expect(calls).toHaveLength(1);
		expect([...calls[0]!.command]).toEqual(expectedCommand("full", fix.out));
		expect(calls[0]!.options.cwd).toBe(repoRoot);
		expect(calls[0]!.options.env).toEqual({
			...envBefore,
			OMP_INSTALLED_RELEASE: fix.release,
			OMP_INSTALLED_MANIFEST_SHA256: fix.manifestSha,
			OMP_ACCOUNTING_DIR: path.join(fix.out, "selected"),
			OMP_ACCOUNTING_RUN: fix.run,
			OMP_ACCOUNTING_CHECK: "installed",
			OMP_ACCOUNTING_ROOT: repoRoot,
		});
		expect(selectQualificationMode({ event: "workflow_dispatch" }).mode).toBe("full");
		expect(selectQualificationMode({ event: "workflow_dispatch" }).targets).toEqual([...FULL_TARGETS]);
	});

	test("subset mode runs the empty pull_request targets and honors uv and root", async () => {
		const fix = await fixture();
		const uv = "/opt/host/uv";
		const { calls, runner } = recordingRunner(async env => {
			await writeReceipts(env.OMP_ACCOUNTING_DIR, 1);
			return { exitCode: 0, stdout: "", stderr: "" };
		});
		const envBefore = { ...process.env };

		const result = await runInstalledAccounting(input(fix, { mode: "subset", uv, runner }));

		expect(result.exitCode).toBe(0);
		expect(result.runs).toEqual([
			{
				name: "selected",
				mode: "subset",
				exitCode: 0,
				receipts: [path.join(fix.out, "selected", "installed-0.skip-receipt.json")],
			},
		]);
		expect([...calls[0]!.command]).toEqual(expectedCommand("subset", fix.out, uv));
		expect(calls[0]!.options.cwd).toBe(fix.root);
		expect(calls[0]!.options.env).toEqual(expectedEnv(fix, fix.root));
		expect(calls[0]!.options.env).toEqual({
			...envBefore,
			OMP_INSTALLED_RELEASE: fix.release,
			OMP_INSTALLED_MANIFEST_SHA256: fix.manifestSha,
			OMP_ACCOUNTING_DIR: path.join(fix.out, "selected"),
			OMP_ACCOUNTING_RUN: fix.run,
			OMP_ACCOUNTING_CHECK: "installed",
			OMP_ACCOUNTING_ROOT: fix.root,
		});
		const selected = selectQualificationMode({ event: "pull_request", changedFiles: [] });
		expect(selected.mode).toBe("subset");
		expect(selected.targets).toEqual([...SUBSET_TARGETS]);
	});

	test("a nonzero pytest exit stays nonzero when one receipt was written", async () => {
		const fix = await fixture();
		const { calls, runner } = recordingRunner(async env => {
			await writeReceipts(env.OMP_ACCOUNTING_DIR, 1);
			return { exitCode: 4, stdout: "", stderr: "isolation failed" };
		});

		const result = await runInstalledAccounting(input(fix, { runner }));

		expect(calls).toHaveLength(1);
		expect(result.exitCode).toBe(4);
		expect(result.runs).toHaveLength(1);
		expect(result.runs[0]?.receipts).toHaveLength(1);
	});

	test("zero receipts is nonzero", async () => {
		const fix = await fixture();
		const { calls, runner } = recordingRunner(async env => {
			await fs.mkdir(env.OMP_ACCOUNTING_DIR ?? "", { recursive: true });
			await fs.writeFile(path.join(env.OMP_ACCOUNTING_DIR ?? "", "notes.json"), "not a receipt\n");
			return { exitCode: 0, stdout: "", stderr: "" };
		});

		const result = await runInstalledAccounting(input(fix, { runner }));

		expect(calls).toHaveLength(1);
		expect(result.exitCode).not.toBe(0);
		expect(result.runs[0]?.receipts).toEqual([]);
	});

	test("two receipts is nonzero", async () => {
		const fix = await fixture();
		const { calls, runner } = recordingRunner(async env => {
			await writeReceipts(env.OMP_ACCOUNTING_DIR, 2);
			return { exitCode: 0, stdout: "", stderr: "" };
		});

		const result = await runInstalledAccounting(input(fix, { runner }));

		expect(calls).toHaveLength(1);
		expect(result.exitCode).not.toBe(0);
		expect(result.runs[0]?.receipts).toHaveLength(2);
	});
});

describe("installed accounting refusals", () => {
	const fields = ["unit", "out", "run", "mode", "release", "manifestSha"] as const;

	for (const field of fields) {
		test(`an empty ${field} never calls the runner`, async () => {
			const fix = await fixture();
			const { calls, runner } = recordingRunner();
			const result = await runInstalledAccounting(input(fix, { [field]: "   ", runner }));
			expect(result.exitCode).not.toBe(0);
			expect(result.runs).toEqual([]);
			expect(calls).toHaveLength(0);
		});
	}

	test("an unknown mode never calls the runner", async () => {
		const fix = await fixture();
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { mode: "nope", runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});

	test("an unreadable unit never calls the runner", async () => {
		const fix = await fixture();
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { unit: path.join(fix.root, "missing.json"), runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});

	test("a unit that is not JSON never calls the runner", async () => {
		const fix = await fixture();
		await fs.writeFile(fix.unit, "not-json");
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});

	test("a unit without run_id never calls the runner", async () => {
		const fix = await fixture();
		const { calls, runner } = recordingRunner();
		for (const body of ["[]", "{}", '{"run_id":1}', '{"run_id":""}']) {
			await fs.writeFile(fix.unit, body);
			const result = await runInstalledAccounting(input(fix, { runner }));
			expect(result.exitCode).not.toBe(0);
			expect(result.runs).toEqual([]);
		}
		expect(calls).toHaveLength(0);
	});

	test("a run id that differs from the unit never calls the runner", async () => {
		const fix = await fixture();
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { run: "other-run", runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});

	test("an existing skip receipt never calls the runner", async () => {
		const fix = await fixture();
		const selected = path.join(fix.out, "selected");
		await fs.mkdir(selected);
		await fs.writeFile(path.join(selected, "earlier.skip-receipt.json"), "{}\n");
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});
});

describe("installed accounting cli", () => {
	test("requires every flag and does not fill omitted flags", () => {
		expect(parseInstalledAccountingArgs([])).toEqual({
			unit: "",
			out: "",
			run: "",
			mode: "",
			release: "",
			manifestSha: "",
		});
		expect(parseInstalledAccountingArgs(["--mode", "subset"])).toEqual({
			unit: "",
			out: "",
			run: "",
			mode: "subset",
			release: "",
			manifestSha: "",
		});
	});

	test("maps the six flags", async () => {
		const fix = await fixture();
		expect(
			parseInstalledAccountingArgs([
				"--unit",
				fix.unit,
				"--out",
				fix.out,
				"--run",
				fix.run,
				"--mode",
				"full",
				"--release",
				fix.release,
				"--manifest-sha",
				fix.manifestSha,
			]),
		).toEqual({
			unit: fix.unit,
			out: fix.out,
			run: fix.run,
			mode: "full",
			release: fix.release,
			manifestSha: fix.manifestSha,
		});
	});

	test("an unknown argument or a missing flag is a nonzero refusal", async () => {
		const unknown = await runInstalledAccountingCli(["--bogus"]);
		expect(unknown.exitCode).not.toBe(0);
		expect(unknown.runs).toEqual([]);

		const missing = await runInstalledAccountingCli(["--mode", "full", "--run"]);
		expect(missing.exitCode).not.toBe(0);
		expect(missing.runs).toEqual([]);

		const incomplete = await runInstalledAccountingCli(["--mode", "full"]);
		expect(incomplete.exitCode).not.toBe(0);
		expect(incomplete.runs).toEqual([]);
	});
});
