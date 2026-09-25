import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { measureBaseline, probeStartup } from "../../../scripts/cpk0-baseline";

const repoRoot = path.resolve(import.meta.dir, "../../..");

describe("CPK-0 baseline probe (OMP-204-s03)", () => {
	it("measureBaseline(1) reports positive latency, memory overhead, and system prompt tokens", async () => {
		const baseline = await measureBaseline(1);
		expect(baseline.runs).toBe(1);
		expect(baseline.startup_latency_ms).toBeGreaterThan(0);
		expect(baseline.memory_overhead_mb).toBeGreaterThan(0);
		expect(baseline.system_prompt_tokens).toBeGreaterThan(0);
	}, 120000);

	it("system prompt token count is deterministic across independent measurements", async () => {
		const first = await measureBaseline(1);
		const second = await measureBaseline(1);
		expect(first.system_prompt_tokens).toBe(second.system_prompt_tokens);
	}, 120000);

	it("probeStartup rejects when the CLI path does not exist", async () => {
		await expect(probeStartup(path.join(repoRoot, "packages/coding-agent/src/does-not-exist.ts"))).rejects.toThrow();
	}, 120000);
});
