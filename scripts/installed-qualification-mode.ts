#!/usr/bin/env bun
import * as fs from "node:fs/promises";

export const SUBSET_TARGETS: readonly string[] = [
	"python/omp-work/tests/test_installed_runtime_isolation.py::test_candidate_edits_and_inherited_config_do_not_change_installed_processes",
	"python/omp-work/tests/test_installed_runtime_isolation.py::test_corrupted_disposable_release_is_refused_before_runtime_launch",
	"python/omp-work/tests/test_installed_execution_recovery.py::test_killed_persisted_turn_performs_one_real_criteria_seal",
	"python/omp-work/tests/test_installed_execution_recovery.py::test_repeated_child_kill_preserves_original_task_preparation",
	"python/omp-work/tests/test_installed_execution_recovery.py::test_committed_resume_post_response_loss_reconciles_same_operation",
];

export const FULL_TARGETS: readonly string[] = [
	"python/omp-work/tests/test_installed_runtime_isolation.py",
	"python/omp-work/tests/test_installed_execution_recovery.py",
];

export const PROTECTED_PATHS: readonly string[] = [
	"session-system/runtime/**",
	"session-system/extensions/workflow/**",
	"session-system/tests/fixtures/installed-recovery-setup.ts",
	"python/omp-work/tests/test_installed_*.py",
	"python/omp-work/tests/installed_runtime_support.py",
	"python/omp-work/tests/pg_native.py",
	"python/omp-work/tests/pg_supervisor.py",
	"packages/coding-agent/src/task/**",
	"packages/coding-agent/src/session/**",
	"python/omp-work/src/omp_work/v1/**",
	".github/workflows/ci.yml",
];

export const KNOWN_EVENTS: ReadonlySet<string> = new Set([
	"pull_request",
	"pull_request_target",
	"push",
	"schedule",
	"workflow_dispatch",
	"release",
]);

const compiledGlobs = PROTECTED_PATHS.map(pattern => new Bun.Glob(pattern));

export function matchesProtectedPath(filePath: string): boolean {
	const normalized = filePath.trim().replace(/\\/g, "/").replace(/^\.\//, "");
	return compiledGlobs.some(glob => glob.match(normalized));
}

export interface SelectQualificationModeOptions {
	event: string;
	isRelease?: boolean | string;
	changedFiles?: string[];
	before?: string;
}

export interface QualificationModeResult {
	mode: "subset" | "full";
	reason: string;
	targets: string[];
}

export function selectQualificationMode(options: SelectQualificationModeOptions): QualificationModeResult {
	const isRelease = options.isRelease === true || options.isRelease === "true" || options.isRelease === "1";
	if (isRelease) {
		return {
			mode: "full",
			reason: "release",
			targets: [...FULL_TARGETS],
		};
	}

	const rawEvent = options.event;
	const event = (rawEvent ?? "").trim().toLowerCase();
	if (!event || !KNOWN_EVENTS.has(event)) {
		throw new Error(`Unknown event: ${rawEvent}`);
	}

	if (event === "release") {
		return {
			mode: "full",
			reason: "release",
			targets: [...FULL_TARGETS],
		};
	}

	if (event === "schedule" || event === "workflow_dispatch") {
		return {
			mode: "full",
			reason: event,
			targets: [...FULL_TARGETS],
		};
	}

	if (event === "push") {
		const before = options.before?.trim();
		if (before && /^0+$/.test(before)) {
			return {
				mode: "full",
				reason: "push with all-zero before",
				targets: [...FULL_TARGETS],
			};
		}
	}

	const changedFiles = options.changedFiles ?? [];
	for (const file of changedFiles) {
		if (matchesProtectedPath(file)) {
			const normalized = file.trim().replace(/\\/g, "/").replace(/^\.\//, "");
			return {
				mode: "full",
				reason: `protected path: ${normalized}`,
				targets: [...FULL_TARGETS],
			};
		}
	}

	return {
		mode: "subset",
		reason: "no protected paths modified",
		targets: [...SUBSET_TARGETS],
	};
}

export async function runCli(): Promise<void> {
	const event = process.env.EVENT_NAME ?? Bun.env.EVENT_NAME ?? "";
	const isReleaseEnv = process.env.IS_RELEASE ?? Bun.env.IS_RELEASE;
	const isRelease = isReleaseEnv === "true" || isReleaseEnv === "1";
	const changedFilesFile = process.env.CHANGED_FILES_FILE ?? Bun.env.CHANGED_FILES_FILE;
	const before = process.env.BEFORE ?? Bun.env.BEFORE;
	const githubOutput = process.env.GITHUB_OUTPUT ?? Bun.env.GITHUB_OUTPUT;

	let changedFiles: string[] = [];
	if (changedFilesFile) {
		try {
			const text = await Bun.file(changedFilesFile).text();
			changedFiles = text
				.split("\n")
				.map(line => line.trim())
				.filter(line => line.length > 0 && !line.startsWith("#"));
		} catch (err) {
			console.error(`Failed to read CHANGED_FILES_FILE (${changedFilesFile}): ${(err as Error).message}`);
			process.exit(1);
		}
	}

	let result: QualificationModeResult;
	try {
		result = selectQualificationMode({
			event,
			isRelease,
			changedFiles,
			before,
		});
	} catch (err) {
		console.error((err as Error).message);
		process.exit(1);
	}

	console.log(`installed qualification mode: ${result.mode} (${result.reason})`);

	if (githubOutput) {
		try {
			const lines = `mode=${result.mode}\ntargets=${result.targets.join(" ")}\n`;
			await fs.appendFile(githubOutput, lines, "utf8");
		} catch (err) {
			console.error(`Failed to write to GITHUB_OUTPUT (${githubOutput}): ${(err as Error).message}`);
			process.exit(1);
		}
	}
}

if (import.meta.main) {
	await runCli();
}
