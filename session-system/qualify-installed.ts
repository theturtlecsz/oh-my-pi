#!/usr/bin/env bun
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $which } from "@oh-my-pi/pi-utils/which";
import { isContained } from "../packages/coding-agent/src/discovery/contained-path";
import { FULL_TARGETS } from "../scripts/installed-qualification-mode";
import { releaseFileSha256 } from "./runtime/manifest";

/** Repository root: the CLI always qualifies the checkout it lives in. */
export const repositoryRoot = path.resolve(import.meta.dir, "..");

const STAGE_SCRIPT = "session-system/runtime/stage.ts";
const STAGE_DESTINATION = "runtime";
const NATIVE_ADDONS = [
	"packages/natives/native/pi_natives.linux-x64-baseline.node",
	"packages/natives/native/pi_natives.linux-x64-modern.node",
] as const;
// CI runs the qualification pytest under `ulimit -c 0`: controller fault
// fixtures crash children on purpose, and core dumps would fill the runner.
const CORE_DUMP_GUARD = 'ulimit -c 0 2>/dev/null; exec "$@"';

export interface CommandResult {
	exitCode: number;
	stdout: string;
	stderr: string;
}

export interface CommandOptions {
	cwd: string;
	env: Record<string, string | undefined>;
}

/** Injectable seam: the real runner spawns a process, tests substitute a fake. */
export type CommandRunner = (command: readonly string[], options: CommandOptions) => Promise<CommandResult>;

export interface QualificationTools {
	bunPath: string;
	bunVersion: string;
	uvPath: string;
	pythonPath: string;
}

export interface QualifyInstalledOptions {
	root?: string;
	runner?: CommandRunner;
	tools?: QualificationTools;
}

export interface QualificationOutcome {
	ok: boolean;
	reason: string;
	tempRoot: string;
	releaseRoot: string;
	manifestSha256: string | null;
	tests: number;
	skipped: number;
}

function summarize(text: string, max = 400): string {
	const collapsed = text.replace(/\s+/g, " ").trim();
	return collapsed.length <= max ? collapsed : collapsed.slice(collapsed.length - max);
}

/** Real command runner: every command observes the CI core-dump guard. */
async function spawnWithCoreDumpGuard(command: readonly string[], options: CommandOptions): Promise<CommandResult> {
	const proc = Bun.spawn(["bash", "-c", CORE_DUMP_GUARD, "omp-qualify-installed", ...command], {
		cwd: options.cwd,
		env: options.env,
		stdout: "pipe",
		stderr: "pipe",
	});
	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	return { exitCode, stdout, stderr };
}

/** Resolve the host toolchain staging records: pinned Bun plus the uv-managed 3.13 interpreter. */
export async function resolveQualificationTools(root: string, runner: CommandRunner): Promise<QualificationTools> {
	const uvPath = $which("uv");
	if (!uvPath) throw new Error("uv not found on PATH — install uv before qualifying an installed runtime");
	const result = await runner([uvPath, "python", "find", "3.13"], { cwd: root, env: { ...process.env } });
	const pythonPath = result.stdout.trim();
	if (result.exitCode !== 0 || !pythonPath) {
		throw new Error(`uv python find 3.13 failed: ${summarize(result.stderr) || `exit ${result.exitCode}`}`);
	}
	return { bunPath: process.execPath, bunVersion: Bun.version, uvPath, pythonPath };
}

function stageCommand(root: string, releaseRoot: string, tools: QualificationTools): string[] {
	const native = NATIVE_ADDONS.map(addon => path.join(root, addon));
	return [
		tools.bunPath,
		STAGE_SCRIPT,
		"--source",
		root,
		"--destination",
		releaseRoot,
		"--bun",
		tools.bunPath,
		"--bun-version",
		tools.bunVersion,
		"--uv",
		tools.uvPath,
		"--python",
		tools.pythonPath,
		"--native",
		native[0],
		"--native",
		native[1],
	];
}

function pytestCommand(uvPath: string, stateRoot: string, junitPath: string): string[] {
	return [
		uvPath,
		"run",
		"--project",
		"python/omp-work",
		"--extra",
		"dev",
		"pytest",
		"-n",
		"3",
		...FULL_TARGETS,
		`--basetemp=${stateRoot}`,
		`--junitxml=${junitPath}`,
	];
}

/** Count JUnit testcases and skips; a run that skips must never read as qualified. */
export function countJunitTests(xml: string): { total: number; skipped: number } {
	return {
		total: (xml.match(/<testcase(?=[\s/>])/g) ?? []).length,
		skipped: (xml.match(/<skipped(?=[\s/>])/g) ?? []).length,
	};
}

/**
 * Stage a clean isolated runtime outside the checkout, then run the installed
 * isolation and controller-recovery pytest targets against it. The temporary
 * root is removed only when the run qualifies; failures preserve it for
 * inspection and report the path.
 */
export async function runQualifyInstalled(options: QualifyInstalledOptions = {}): Promise<QualificationOutcome> {
	const root = path.resolve(options.root ?? repositoryRoot);
	const runner = options.runner ?? spawnWithCoreDumpGuard;
	const tools = options.tools ?? (await resolveQualificationTools(root, runner));
	const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "omp-installed-qualification-"));
	const releaseRoot = path.join(tempRoot, STAGE_DESTINATION);
	const stateRoot = path.join(tempRoot, "state");
	const junitPath = path.join(tempRoot, "tests.xml");
	const failure = (
		reason: string,
		manifestSha256: string | null = null,
		tests = 0,
		skipped = 0,
	): QualificationOutcome => ({ ok: false, reason, tempRoot, releaseRoot, manifestSha256, tests, skipped });

	if (isContained(root, tempRoot)) {
		return failure("temporary runtime landed inside the checkout");
	}

	// A stage failure must abort before pytest: an unqualified runtime is never executed.
	const stage = await runner(stageCommand(root, releaseRoot, tools), { cwd: root, env: { ...process.env } });
	if (stage.exitCode !== 0) {
		return failure(`stage failed with exit ${stage.exitCode}: ${summarize(stage.stderr) || "no stderr"}`);
	}

	const manifestPath = path.join(releaseRoot, "manifest.json");
	let manifestSha256: string;
	try {
		manifestSha256 = await releaseFileSha256(manifestPath);
	} catch (err) {
		return failure(`staged manifest unreadable: ${err instanceof Error ? err.message : String(err)}`);
	}

	const env = {
		...process.env,
		OMP_INSTALLED_RELEASE: releaseRoot,
		OMP_INSTALLED_MANIFEST_SHA256: manifestSha256,
	};
	const pytest = await runner(pytestCommand(tools.uvPath, stateRoot, junitPath), { cwd: root, env });
	if (pytest.exitCode !== 0) {
		return failure(
			`pytest failed with exit ${pytest.exitCode}: ${summarize(pytest.stderr) || "no stderr"}`,
			manifestSha256,
		);
	}

	let xml: string;
	try {
		xml = await Bun.file(junitPath).text();
	} catch (err) {
		return failure(`junit report unreadable: ${err instanceof Error ? err.message : String(err)}`, manifestSha256);
	}
	const { total, skipped } = countJunitTests(xml);
	if (total === 0) return failure("junit reported no tests", manifestSha256, 0, skipped);
	if (skipped > 0) return failure(`junit reported ${skipped} skipped`, manifestSha256, total, skipped);

	await fs.rm(tempRoot, { recursive: true, force: true });
	return {
		ok: true,
		reason: "installed qualification passed",
		tempRoot,
		releaseRoot,
		manifestSha256,
		tests: total,
		skipped: 0,
	};
}

async function main(): Promise<void> {
	const outcome = await runQualifyInstalled().catch((err: unknown) => {
		console.error(`qualify-installed: ${err instanceof Error ? err.message : String(err)}`);
		return null;
	});
	if (outcome === null || !outcome.ok) {
		if (outcome !== null) {
			console.error(`qualify-installed: ${outcome.reason}`);
			console.error(`qualify-installed: evidence preserved at ${outcome.tempRoot}`);
		}
		process.exit(1);
	}
	console.log(
		`qualify-installed: PASS tests=${outcome.tests} skipped=${outcome.skipped} manifest=${outcome.manifestSha256} release=${outcome.releaseRoot}`,
	);
}

if (import.meta.main) {
	await main();
}
