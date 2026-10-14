import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { CommandOptions, CommandResult, CommandRunner } from "../session-system/qualify-installed.ts";
import { spawnWithCoreDumpGuard } from "../session-system/qualify-installed.ts";
import { FULL_TARGETS, SUBSET_TARGETS, selectQualificationMode } from "./installed-qualification-mode.ts";
import {
	parseInstalledAccountingArgs,
	parsePending,
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
const projectDir = path.join(repoRoot, "python/omp-work");
const RECEIPT_SUFFIX = ".skip-receipt.json";

const ISOLATION_FILE = FULL_TARGETS[0]!;
const RECOVERY_FILE = FULL_TARGETS[1]!;
const LIFETIME_FILE = FULL_TARGETS[2]!;
const LIFETIME_NODE = `${LIFETIME_FILE}::test_sigkill_of_pytest_parent_terminates_installed_runtime_servers`;
const SUPPLEMENT_NODE = `${ISOLATION_FILE}::test_supplement_only`;

const tempDirs: string[] = [];

afterEach(async () => {
	await Promise.all(tempDirs.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

interface Fixture {
	root: string;
	out: string;
	release: string;
	unit: string;
	run: string;
	manifestSha: string;
}

type SupplementMode = "pass" | "skip" | "fail";

interface RecordedCall {
	command: readonly string[];
	options: CommandOptions;
}

interface SkipReceipt {
	check: string;
	run_id: string;
	selected: string[];
	collected: string[];
	complete: boolean;
}

function nodeName(target: string): string {
	return target.slice(target.indexOf("::") + 2);
}

function stub(name: string, body: string): string {
	return `\n@requires_installed\ndef ${name}():\n${body}\n`;
}

function isolationSource(): string {
	const names = SUBSET_TARGETS.filter(target => target.startsWith(`${ISOLATION_FILE}::`)).map(nodeName);
	const functions = names.map(name => stub(name, "    assert os.environ.get('OMP_INSTALLED_RELEASE')\n"));
	functions.push(
		[
			"",
			"@requires_installed",
			"def test_supplement_only():",
			"    mode = os.environ.get('FIXTURE_SUPPLEMENT', 'pass')",
			"    if mode == 'skip':",
			"        pytest.skip('supplement fixture skipped')",
			"    if mode == 'fail':",
			"        raise AssertionError('supplement fixture failed')",
			"    assert os.environ.get('OMP_INSTALLED_RELEASE')",
			"",
		].join("\n"),
	);
	return `${HEADER}${functions.join("")}`;
}

const HEADER = [
	"import os",
	"",
	"import pytest",
	"",
	"requires_installed = pytest.mark.skipif(",
	"    not os.environ.get('OMP_INSTALLED_RELEASE'),",
	"    reason='installed-process qualification requires OMP_INSTALLED_RELEASE',",
	")",
	"",
].join("\n");

function plainSource(file: string): string {
	const names = SUBSET_TARGETS.filter(target => target.startsWith(`${file}::`)).map(nodeName);
	if (file === LIFETIME_FILE) names.push(nodeName(LIFETIME_NODE));
	return `${HEADER}${names.map(name => stub(name, "    assert os.environ.get('OMP_INSTALLED_RELEASE')\n")).join("")}`;
}

function cleanEnv(overrides: Record<string, string | undefined>): Record<string, string | undefined> {
	const env: Record<string, string | undefined> = {};
	for (const [key, value] of Object.entries({ ...process.env, ...overrides })) {
		if (key.startsWith("PYTEST_")) continue;
		env[key] = value;
	}
	return env;
}

async function makeFixture(runId = "run-617"): Promise<Fixture> {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "omp-installed-accounting-"));
	tempDirs.push(root);
	const testsDir = path.join(root, "python/omp-work/tests");
	await Bun.write(path.join(root, "python/omp-work/pyproject.toml"), "[tool.pytest.ini_options]\n");
	await Bun.write(path.join(testsDir, path.basename(ISOLATION_FILE)), isolationSource());
	await Bun.write(path.join(testsDir, path.basename(RECOVERY_FILE)), plainSource(RECOVERY_FILE));
	await Bun.write(path.join(testsDir, path.basename(LIFETIME_FILE)), plainSource(LIFETIME_FILE));
	const out = path.join(root, "out");
	await Bun.write(path.join(out, "notes.txt"), "keep\n");
	const release = path.join(root, "release");
	await fs.mkdir(release, { recursive: true });
	const unit = path.join(root, "unit.json");
	await Bun.write(unit, `${JSON.stringify({ version: 1, run_id: runId })}\n`);
	return { root, out, release, unit, run: runId, manifestSha: "a".repeat(64) };
}

async function writeUnitReceipt(fix: Fixture): Promise<void> {
	const dir = path.join(fix.root, "unit-accounting");
	const result = await spawnWithCoreDumpGuard(
		[
			"uv",
			"run",
			"--project",
			projectDir,
			"--extra",
			"dev",
			"pytest",
			"-p",
			"omp_work.skip_accounting",
			`--basetemp=${path.join(fix.root, "unit-state")}`,
			...FULL_TARGETS,
		],
		{
			cwd: fix.root,
			env: cleanEnv({
				OMP_INSTALLED_RELEASE: undefined,
				OMP_ACCOUNTING_DIR: dir,
				OMP_ACCOUNTING_RUN: fix.run,
				OMP_ACCOUNTING_CHECK: "unit",
				OMP_ACCOUNTING_ROOT: fix.root,
			}),
		},
	);
	if (result.exitCode !== 0) throw new Error(`unit pytest failed: ${result.stderr}${result.stdout}`);
	const receipts = (await fs.readdir(dir)).filter(name => name.endsWith(RECEIPT_SUFFIX));
	expect(receipts).toHaveLength(1);
	await fs.copyFile(path.join(dir, receipts[0]!), fix.unit);
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

/** Real runner: spawns the real command at the repository project and pins the fixture supplement mode. */
function fixtureRunner(mode: SupplementMode = "pass"): { calls: RecordedCall[]; runner: CommandRunner } {
	const calls: RecordedCall[] = [];
	const runner: CommandRunner = async (command, options) => {
		calls.push({ command: [...command], options });
		const rewritten = command.map(part => (part === "python/omp-work" ? projectDir : part));
		return spawnWithCoreDumpGuard(rewritten, {
			cwd: options.cwd,
			env: cleanEnv({ ...options.env, FIXTURE_SUPPLEMENT: mode }),
		});
	};
	return { calls, runner };
}

async function readReceipt(file: string): Promise<SkipReceipt> {
	return (await Bun.file(file).json()) as SkipReceipt;
}

describe("installed accounting subset supplement", () => {
	test("selected receipt lacks test_supplement_only, the supplement executes it and the lifetime node", async () => {
		const fix = await makeFixture();
		await writeUnitReceipt(fix);
		const { calls, runner } = fixtureRunner("pass");

		const result = await runInstalledAccounting(input(fix, { mode: "subset", runner }));

		expect(result.exitCode).toBe(0);
		expect(result.runs.map(run => run.name)).toEqual(["selected", "supplement"]);
		expect(result.pending).toEqual({ pending: [], failed: [], rejected: [], absent: [] });
		// The selected run is the subset argv in DIR/selected with the installed accounting environment.
		expect([...calls[0]!.command]).toEqual([
			"uv",
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
			...SUBSET_TARGETS,
			`--basetemp=${path.join(fix.out, "selected-state")}`,
		]);
		expect(calls[0]!.options.cwd).toBe(fix.root);
		expect(calls[0]!.options.env.OMP_ACCOUNTING_DIR).toBe(path.join(fix.out, "selected"));
		expect(calls[0]!.options.env.OMP_ACCOUNTING_ROOT).toBe(fix.root);

		const selected = await readReceipt(result.runs[0]!.receipts[0]!);
		expect(selected.check).toBe("installed");
		expect(selected.selected).not.toContain(SUPPLEMENT_NODE);
		expect(selected.selected).toHaveLength(5);

		expect(result.runs[1]!.receipts).toHaveLength(1);
		const supplement = await readReceipt(result.runs[1]!.receipts[0]!);
		expect(supplement.selected).toContain(SUPPLEMENT_NODE);
		expect(supplement.selected).toContain(LIFETIME_NODE);
		// The supplement runs in DIR/supplement on the pending ids, not the original targets.
		const supplementCall = calls.find(
			call => call.options.env.OMP_ACCOUNTING_DIR === path.join(fix.out, "supplement"),
		);
		expect(supplementCall).toBeDefined();
		expect([...supplementCall!.command]).toContain(SUPPLEMENT_NODE);
		expect([...supplementCall!.command]).toContain(LIFETIME_NODE);
	});

	test("a supplement skip stays nonzero and leaves the id pending", async () => {
		const fix = await makeFixture();
		await writeUnitReceipt(fix);
		const { runner } = fixtureRunner("skip");

		const result = await runInstalledAccounting(input(fix, { mode: "subset", runner }));

		expect(result.exitCode).not.toBe(0);
		expect(result.pending.pending.map(entry => entry.id)).toEqual([SUPPLEMENT_NODE]);
		expect(result.pending.failed).toEqual([]);
	});

	test("a supplement failure stays nonzero and records the id as failed", async () => {
		const fix = await makeFixture();
		await writeUnitReceipt(fix);
		const { runner } = fixtureRunner("fail");

		const result = await runInstalledAccounting(input(fix, { mode: "subset", runner }));

		expect(result.exitCode).not.toBe(0);
		expect(result.pending.pending).toEqual([]);
		expect(result.pending.failed.map(entry => entry.id)).toEqual([SUPPLEMENT_NODE]);
	});
});

describe("installed accounting full run", () => {
	test("full mode executes every node in one run without a supplement", async () => {
		const fix = await makeFixture();
		await writeUnitReceipt(fix);
		const { calls, runner } = fixtureRunner("pass");

		const result = await runInstalledAccounting(input(fix, { mode: "full", runner }));

		expect(result.exitCode).toBe(0);
		expect(result.runs).toHaveLength(1);
		expect(result.runs[0]!.name).toBe("selected");
		expect(result.runs[0]!.mode).toBe("full");
		expect(result.pending).toEqual({ pending: [], failed: [], rejected: [], absent: [] });
		const selected = await readReceipt(result.runs[0]!.receipts[0]!);
		expect(selected.selected).toContain(SUPPLEMENT_NODE);
		expect(selected.selected).toContain(LIFETIME_NODE);
		expect(selected.selected).toHaveLength(7);
		expect(calls.some(call => call.options.env.OMP_ACCOUNTING_DIR === path.join(fix.out, "supplement"))).toBe(false);
	});
});

describe("pending output parsing", () => {
	test("accepts the four arrays and rejects a truncated or malformed document", () => {
		expect(parsePending('{"pending":[],"covered":[],"failed":[],"rejected":[],"absent":[]}')).toEqual({
			pending: [],
			failed: [],
			rejected: [],
			absent: [],
		});
		expect(parsePending("not json")).toEqual({ error: "pending output is not JSON" });
		expect(parsePending('{"pending":[]}')).toEqual({ error: "pending output has no failed array" });
		expect(parsePending('{"pending":[{"id":1}],"failed":[],"rejected":[],"absent":[]}')).toEqual({
			error: "pending output has a malformed pending entry",
		});
	});
});

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

const EMPTY_PENDING_JSON = JSON.stringify({ pending: [], covered: [], failed: [], rejected: [], absent: [] });

describe("installed accounting selected run", () => {
	test("full mode runs workflow_dispatch targets with the installed accounting environment", async () => {
		const fix = await makeFixture();
		await fs.writeFile(path.join(fix.out, "stray.skip-receipt.json"), "{}\n");
		const { calls, runner } = recordingRunner(async env => {
			if ((env.OMP_ACCOUNTING_DIR ?? "").endsWith("selected")) {
				await fs.mkdir(env.OMP_ACCOUNTING_DIR ?? "", { recursive: true });
				await fs.writeFile(path.join(env.OMP_ACCOUNTING_DIR ?? "", "notes.json"), "ignore\n");
				await writeReceipts(env.OMP_ACCOUNTING_DIR, 1);
			}
			return { exitCode: 0, stdout: EMPTY_PENDING_JSON, stderr: "" };
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
		expect(calls).toHaveLength(2);
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
	});

	test("subset mode runs the five pull_request targets and honors uv and root", async () => {
		const fix = await makeFixture();
		const uv = "/opt/host/uv";
		const { calls, runner } = recordingRunner(async env => {
			if ((env.OMP_ACCOUNTING_DIR ?? "").endsWith("selected")) await writeReceipts(env.OMP_ACCOUNTING_DIR, 1);
			return { exitCode: 0, stdout: EMPTY_PENDING_JSON, stderr: "" };
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
	});

	test("a nonzero pytest exit stays nonzero when one receipt was written", async () => {
		const fix = await makeFixture();
		const { calls, runner } = recordingRunner(async env => {
			if ((env.OMP_ACCOUNTING_DIR ?? "").endsWith("selected")) await writeReceipts(env.OMP_ACCOUNTING_DIR, 1);
			return { exitCode: 4, stdout: EMPTY_PENDING_JSON, stderr: "isolation failed" };
		});

		const result = await runInstalledAccounting(input(fix, { mode: "full", runner }));

		expect(calls[0]!.command).toContain("pytest");
		expect(result.exitCode).toBe(4);
		expect(result.runs).toHaveLength(1);
		expect(result.runs[0]?.receipts).toHaveLength(1);
	});

	test("zero receipts is nonzero", async () => {
		const fix = await makeFixture();
		const { calls, runner } = recordingRunner(async env => {
			await fs.mkdir(env.OMP_ACCOUNTING_DIR ?? "", { recursive: true });
			await fs.writeFile(path.join(env.OMP_ACCOUNTING_DIR ?? "", "notes.json"), "not a receipt\n");
			return { exitCode: 0, stdout: EMPTY_PENDING_JSON, stderr: "" };
		});

		const result = await runInstalledAccounting(input(fix, { runner }));

		expect(calls).toHaveLength(1);
		expect(result.exitCode).not.toBe(0);
		expect(result.runs[0]?.receipts).toEqual([]);
	});

	test("two receipts is nonzero", async () => {
		const fix = await makeFixture();
		const { calls, runner } = recordingRunner(async env => {
			await writeReceipts(env.OMP_ACCOUNTING_DIR, 2);
			return { exitCode: 0, stdout: EMPTY_PENDING_JSON, stderr: "" };
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
			const fix = await makeFixture();
			const { calls, runner } = recordingRunner();
			const result = await runInstalledAccounting(input(fix, { [field]: "   ", runner }));
			expect(result.exitCode).not.toBe(0);
			expect(result.runs).toEqual([]);
			expect(calls).toHaveLength(0);
		});
	}

	test("an unknown mode never calls the runner", async () => {
		const fix = await makeFixture();
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { mode: "nope", runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});

	test("an unreadable unit never calls the runner", async () => {
		const fix = await makeFixture();
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { unit: path.join(fix.root, "missing.json"), runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});

	test("a unit that is not JSON never calls the runner", async () => {
		const fix = await makeFixture();
		await fs.writeFile(fix.unit, "not-json");
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});

	test("a unit without run_id never calls the runner", async () => {
		const fix = await makeFixture();
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
		const fix = await makeFixture();
		const { calls, runner } = recordingRunner();
		const result = await runInstalledAccounting(input(fix, { run: "other-run", runner }));
		expect(result.exitCode).not.toBe(0);
		expect(result.runs).toEqual([]);
		expect(calls).toHaveLength(0);
	});

	test("an existing skip receipt never calls the runner", async () => {
		const fix = await makeFixture();
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
		const fix = await makeFixture();
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
