import { describe, expect, it } from "bun:test";
import * as os from "node:os";
import * as path from "node:path";
import { Tokenizer } from "@oh-my-pi/pi-agent-core";
import { buildSystemPrompt } from "@oh-my-pi/pi-coding-agent/system-prompt";
import { TempDir } from "@oh-my-pi/pi-utils";
import {
	BASELINE_PATH,
	type Baseline,
	type BaselineBudgets,
	type BaselineDocument,
	checkAgainstBudget,
} from "../../../scripts/cpk0-baseline";

const repoRoot = path.resolve(import.meta.dir, "../../..");

function tempPrefix(name: string): string {
	return path.join(os.tmpdir(), `cpk0-test-${name}-`);
}

describe("CPK-0 baseline budgets (OMP-204-s04)", () => {
	it("committed baseline.json parses and every budget is greater than or equal to observed value", async () => {
		const doc = (await Bun.file(BASELINE_PATH).json()) as BaselineDocument;

		expect(doc.schema_version).toBe("cpk0-baseline/v1");
		expect(typeof doc.method.startup).toBe("string");
		expect(typeof doc.method.memory).toBe("string");
		expect(typeof doc.method.tokens).toBe("string");
		expect(doc.method.startup.length).toBeGreaterThan(0);
		expect(doc.method.memory.length).toBeGreaterThan(0);
		expect(doc.method.tokens.length).toBeGreaterThan(0);

		expect(doc.observed.startup_latency_ms).toBeGreaterThan(0);
		expect(doc.observed.memory_overhead_mb).toBeGreaterThan(0);
		expect(doc.observed.system_prompt_tokens).toBeGreaterThan(0);
		expect(doc.observed.runs).toBeGreaterThanOrEqual(1);

		expect(doc.budgets.startup_latency_ms).toBeGreaterThanOrEqual(doc.observed.startup_latency_ms);
		expect(doc.budgets.memory_overhead_mb).toBeGreaterThanOrEqual(doc.observed.memory_overhead_mb);
		expect(doc.budgets.system_prompt_tokens).toBeGreaterThanOrEqual(doc.observed.system_prompt_tokens);

		expect(typeof doc.platform).toBe("string");
		expect(doc.platform.length).toBeGreaterThan(0);
	});

	it("fresh system prompt token count is within budgeted system prompt tokens", async () => {
		const doc = (await Bun.file(BASELINE_PATH).json()) as BaselineDocument;
		const cwd = await TempDir.create(tempPrefix("prompt"));
		try {
			const { systemPrompt } = await buildSystemPrompt({ cwd: cwd.path(), contextFiles: [], skills: [], rules: [] });
			const freshTokens = new Tokenizer(null).countTokens(systemPrompt, "strict");
			expect(freshTokens).toBeLessThanOrEqual(doc.budgets.system_prompt_tokens);
		} finally {
			await cwd.remove();
		}
	});

	it("checkAgainstBudget returns [] for observed==budget and ['startup_latency_ms'] when only latency exceeds", () => {
		const sampleBaseline: Baseline = {
			startup_latency_ms: 500,
			memory_overhead_mb: 200,
			system_prompt_tokens: 2000,
			runs: 5,
		};

		const matchingBudget: BaselineBudgets = {
			startup_latency_ms: 500,
			memory_overhead_mb: 200,
			system_prompt_tokens: 2000,
		};
		expect(checkAgainstBudget(sampleBaseline, matchingBudget)).toEqual([]);

		const latencyExceededBudget: BaselineBudgets = {
			startup_latency_ms: 499,
			memory_overhead_mb: 200,
			system_prompt_tokens: 2000,
		};
		expect(checkAgainstBudget(sampleBaseline, latencyExceededBudget)).toEqual(["startup_latency_ms"]);

		const memoryExceededBudget: BaselineBudgets = {
			startup_latency_ms: 500,
			memory_overhead_mb: 199,
			system_prompt_tokens: 2000,
		};
		expect(checkAgainstBudget(sampleBaseline, memoryExceededBudget)).toEqual(["memory_overhead_mb"]);

		const allExceededBudget: BaselineBudgets = {
			startup_latency_ms: 400,
			memory_overhead_mb: 150,
			system_prompt_tokens: 1500,
		};
		expect(checkAgainstBudget(sampleBaseline, allExceededBudget)).toEqual([
			"startup_latency_ms",
			"memory_overhead_mb",
			"system_prompt_tokens",
		]);
	});

	it("CLI --check --budget with all budgets 1 exits 1 and stdout contains OVER", async () => {
		const tmp = await TempDir.create(tempPrefix("budget"));
		const budgetPath = path.join(tmp.path(), "exceeded-budget.json");
		try {
			await Bun.write(
				budgetPath,
				JSON.stringify({
					budgets: {
						startup_latency_ms: 1,
						memory_overhead_mb: 1,
						system_prompt_tokens: 1,
					},
				}),
			);

			const proc = Bun.spawn(
				["bun", path.join(repoRoot, "scripts/cpk0-baseline.ts"), "--check", "--budget", budgetPath],
				{
					cwd: repoRoot,
					stdin: "ignore",
					stdout: "pipe",
					stderr: "pipe",
				},
			);

			const [exitCode, stdout] = await Promise.all([
				proc.exited,
				new Response(proc.stdout as ReadableStream<Uint8Array>).text(),
			]);

			expect(exitCode).toBe(1);
			expect(stdout).toContain("OVER");
		} finally {
			await tmp.remove();
		}
	}, 120000);
});
