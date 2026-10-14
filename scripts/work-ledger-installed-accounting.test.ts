import { describe, expect, test } from "bun:test";
import { FULL_TARGETS, SUBSET_TARGETS, selectQualificationMode } from "./installed-qualification-mode.ts";

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
