import { afterEach, describe, expect, spyOn, test, vi } from "bun:test";
import { commandsForGate } from "./ci-test-ts.ts";
import * as gateScope from "./gate-scope.ts";

afterEach(() => {
	vi.restoreAllMocks();
});

describe("task-scoped test command plan", () => {
	test("Python-only task omits workspace TS suites but retains session-system and Rust checks", async () => {
		spyOn(gateScope, "changedFiles").mockResolvedValue([
			"python/omp-work/src/omp_work/research/capacity.py",
			"python/omp-work/tests/test_research_capacity.py",
			"docs/upstream-fork-inventory.tsv",
		]);
		const commands = await commandsForGate("local", "test", true);
		expect(commands.map(command => command.label)).toEqual([
			"session-system/tests",
			"rust (cargo nextest; skipped if no Rust changes)",
		]);
		expect(commands[0].command).toContain("session-system/tests");
		expect(commands[1].command).toEqual(["bun", "scripts/run-rs-task.ts", "test:rs"]);
	});

	test("schema, web, mixed-language and unknown-path changes retain workspace regression suites", async () => {
		const fullPlan = await commandsForGate("local", "gate");
		const diff = spyOn(gateScope, "changedFiles");
		for (const files of [
			["python/omp-work/src/omp_work/contracts/v1/schema.json"],
			["python/robomp/web/src/App.vue"],
			["python/omp-work/src/omp_work/models.py", "packages/work-client/src/client.ts"],
			["session-system/extensions/workflow/agent-stop.ts"],
			["infra/automation-user/parallel-test.sh"],
		]) {
			diff.mockResolvedValue(files);
			expect(await commandsForGate("local", "test", true)).toEqual(fullPlan);
		}
	});

	test("missing or failed diff retains full plan instead of silently skipping regression suites", async () => {
		const fullPlan = await commandsForGate("local", "gate");
		const diff = spyOn(gateScope, "changedFiles").mockResolvedValue([]);
		expect(await commandsForGate("local", "test", true)).toEqual(fullPlan);
		diff.mockRejectedValue(new Error("unknown revision main"));
		expect(await commandsForGate("local", "test", true)).toEqual(fullPlan);
	});

	test("pre-batch and manual local plans keep every TS suite even for Python-only changes", async () => {
		spyOn(gateScope, "changedFiles").mockResolvedValue(["python/omp-work/src/omp_work/models.py"]);
		const taskPlan = await commandsForGate("local-ts", "test", true);
		const gatePlan = await commandsForGate("local-ts", "gate", true);
		expect(taskPlan.map(command => command.cwd)).toEqual(["."]);
		expect(gatePlan.some(command => command.cwd === "packages/stats")).toBe(true);
		expect(gatePlan.some(command => command.cwd === "packages/coding-agent")).toBe(true);
		expect(await commandsForGate("local-ts", undefined, true)).toEqual(gatePlan);
		expect(await commandsForGate("local-ts", "test")).toEqual(gatePlan);
	});

	test("CI all bucket preserves complete package and coding-agent coverage regardless of task phase", async () => {
		spyOn(gateScope, "changedFiles").mockResolvedValue(["python/omp-work/src/omp_work/models.py"]);
		const ciPlan = await commandsForGate("all", undefined);
		expect(ciPlan.some(command => command.cwd === "packages/stats")).toBe(true);
		expect(ciPlan.some(command => command.cwd === "packages/coding-agent")).toBe(true);
		expect(await commandsForGate("all", "test", true)).toEqual(ciPlan);
	});
});
