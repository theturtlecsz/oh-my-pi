import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import {
	FULL_TARGETS,
	matchesProtectedPath,
	PROTECTED_PATHS,
	SUBSET_TARGETS,
	selectQualificationMode,
} from "./installed-qualification-mode.ts";

const repoRoot = path.join(import.meta.dir, "..");
const tempDirs: string[] = [];

afterEach(async () => {
	await Promise.all(tempDirs.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

async function makeTempDir(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-installed-mode-test-"));
	tempDirs.push(dir);
	return dir;
}

async function runCliSubprocess(env: Record<string, string>): Promise<{
	exitCode: number;
	stdout: string;
	stderr: string;
}> {
	const proc = Bun.spawn(["bun", "scripts/installed-qualification-mode.ts"], {
		cwd: repoRoot,
		env: {
			...process.env,
			...env,
		},
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

describe("installed qualification mode selection", () => {
	test("release -> full: isRelease boolean or string flag forces full mode and targets", () => {
		const resultBool = selectQualificationMode({
			event: "pull_request",
			isRelease: true,
			changedFiles: ["packages/tui/src/a.ts"],
		});
		expect(resultBool.mode).toBe("full");
		expect(resultBool.reason).toBe("release");
		expect(resultBool.targets).toEqual([...FULL_TARGETS]);

		const resultStr = selectQualificationMode({
			event: "push",
			isRelease: "true",
		});
		expect(resultStr.mode).toBe("full");
		expect(resultStr.reason).toBe("release");
		expect(resultStr.targets).toEqual([...FULL_TARGETS]);

		const resultEvent = selectQualificationMode({
			event: "release",
		});
		expect(resultEvent.mode).toBe("full");
		expect(resultEvent.reason).toBe("release");
		expect(resultEvent.targets).toEqual([...FULL_TARGETS]);
	});

	test("schedule/dispatch -> full: schedule and workflow_dispatch triggers run full qualification", () => {
		const scheduleResult = selectQualificationMode({
			event: "schedule",
		});
		expect(scheduleResult.mode).toBe("full");
		expect(scheduleResult.reason).toBe("schedule");
		expect(scheduleResult.targets).toEqual([...FULL_TARGETS]);

		const dispatchResult = selectQualificationMode({
			event: "workflow_dispatch",
		});
		expect(dispatchResult.mode).toBe("full");
		expect(dispatchResult.reason).toBe("workflow_dispatch");
		expect(dispatchResult.targets).toEqual([...FULL_TARGETS]);
	});

	test("PR with session-system/runtime/x.ts -> full: protected path is detected and named in reason", () => {
		const result = selectQualificationMode({
			event: "pull_request",
			changedFiles: ["session-system/runtime/x.ts"],
		});
		expect(result.mode).toBe("full");
		expect(result.reason).toContain("session-system/runtime/x.ts");
		expect(result.targets).toEqual([...FULL_TARGETS]);
	});

	test("PR with only packages/tui/src/a.ts -> subset: selects exactly the 5 node id targets", () => {
		const result = selectQualificationMode({
			event: "pull_request",
			changedFiles: ["packages/tui/src/a.ts"],
		});
		expect(result.mode).toBe("subset");
		expect(result.targets).toHaveLength(5);
		expect(result.targets).toEqual([...SUBSET_TARGETS]);
	});

	test("near misses session-system/runtimex/a and python/omp-work/tests/test_other.py -> subset", () => {
		const result = selectQualificationMode({
			event: "pull_request",
			changedFiles: ["session-system/runtimex/a", "python/omp-work/tests/test_other.py"],
		});
		expect(result.mode).toBe("subset");
		expect(result.targets).toEqual([...SUBSET_TARGETS]);
	});

	test("push with all-zero before -> full: fresh branch or tag without prior commit runs full", () => {
		const result = selectQualificationMode({
			event: "push",
			before: "0000000000000000000000000000000000000000",
			changedFiles: ["packages/tui/src/a.ts"],
		});
		expect(result.mode).toBe("full");
		expect(result.reason).toContain("all-zero before");
		expect(result.targets).toEqual([...FULL_TARGETS]);
	});

	test("reason names the first matching protected file when multiple match", () => {
		const result = selectQualificationMode({
			event: "pull_request",
			changedFiles: [
				"packages/tui/src/a.ts",
				"packages/coding-agent/src/task/runner.ts",
				"python/omp-work/src/omp_work/v1/service.py",
			],
		});
		expect(result.mode).toBe("full");
		expect(result.reason).toContain("packages/coding-agent/src/task/runner.ts");
		expect(result.reason).not.toContain("python/omp-work/src/omp_work/v1/service.py");
	});

	test("protected path matcher validates all 11 required patterns", () => {
		expect(PROTECTED_PATHS).toHaveLength(11);
		const testCases = [
			{ path: "session-system/runtime/worker.ts", expected: true },
			{ path: "session-system/extensions/workflow/engine.ts", expected: true },
			{ path: "session-system/tests/fixtures/installed-recovery-setup.ts", expected: true },
			{ path: "python/omp-work/tests/test_installed_custom.py", expected: true },
			{ path: "python/omp-work/tests/installed_runtime_support.py", expected: true },
			{ path: "python/omp-work/tests/pg_native.py", expected: true },
			{ path: "python/omp-work/tests/pg_supervisor.py", expected: true },
			{ path: "packages/coding-agent/src/task/worker.ts", expected: true },
			{ path: "packages/coding-agent/src/session/manager.ts", expected: true },
			{ path: "python/omp-work/src/omp_work/v1/models.py", expected: true },
			{ path: ".github/workflows/ci.yml", expected: true },
			{ path: "packages/coding-agent/src/utils/math.ts", expected: false },
			{ path: "python/omp-work/tests/test_unrelated.py", expected: false },
		];

		for (const { path: filePath, expected } of testCases) {
			expect(matchesProtectedPath(filePath)).toBe(expected);
		}
	});

	test("unknown event throws an Error in selectQualificationMode", () => {
		expect(() => selectQualificationMode({ event: "unknown_event_xyz" })).toThrow(/Unknown event/);
	});
});

describe("installed qualification mode CLI", () => {
	test("CLI writes mode/targets to a temp GITHUB_OUTPUT and stdout", async () => {
		const tempDir = await makeTempDir();
		const changedFile = path.join(tempDir, "changed.txt");
		await Bun.write(changedFile, "packages/tui/src/a.ts\n");

		const outputFile = path.join(tempDir, "github_output");
		await Bun.write(outputFile, "PRE_EXISTING=1\n");

		const { exitCode, stdout, stderr } = await runCliSubprocess({
			EVENT_NAME: "pull_request",
			CHANGED_FILES_FILE: changedFile,
			GITHUB_OUTPUT: outputFile,
		});

		expect(exitCode).toBe(0);
		expect(stderr).toBe("");
		expect(stdout).toContain("installed qualification mode: subset (no protected paths modified)");

		const outputContent = await Bun.file(outputFile).text();
		expect(outputContent).toContain("PRE_EXISTING=1\n");
		expect(outputContent).toContain("mode=subset\n");
		expect(outputContent).toContain(`targets=${SUBSET_TARGETS.join(" ")}\n`);
	});

	test("CLI full mode writes full targets to GITHUB_OUTPUT on protected path change", async () => {
		const tempDir = await makeTempDir();
		const changedFile = path.join(tempDir, "changed.txt");
		await Bun.write(changedFile, "session-system/runtime/manifest.ts\n");

		const outputFile = path.join(tempDir, "github_output");

		const { exitCode, stdout, stderr } = await runCliSubprocess({
			EVENT_NAME: "pull_request",
			CHANGED_FILES_FILE: changedFile,
			GITHUB_OUTPUT: outputFile,
		});

		expect(exitCode).toBe(0);
		expect(stderr).toBe("");
		expect(stdout).toContain(
			"installed qualification mode: full (protected path: session-system/runtime/manifest.ts)",
		);

		const outputContent = await Bun.file(outputFile).text();
		expect(outputContent).toContain("mode=full\n");
		expect(outputContent).toContain(`targets=${FULL_TARGETS.join(" ")}\n`);
	});

	test("unknown event exits non-zero", async () => {
		const { exitCode, stderr } = await runCliSubprocess({
			EVENT_NAME: "unrecognized_event",
		});

		expect(exitCode).not.toBe(0);
		expect(stderr).toContain("Unknown event: unrecognized_event");
	});
});
