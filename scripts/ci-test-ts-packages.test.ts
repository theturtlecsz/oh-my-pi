import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { $ } from "bun";

const repoRoot = path.resolve(import.meta.dir, "..");

describe("ci-test-ts package coverage", () => {
	test("native mode dry-run includes packages/stats chunk", async () => {
		const result = await $`bun scripts/ci-test-ts.ts native --dry-run`.cwd(repoRoot).quiet().nothrow();
		expect(result.exitCode).toBe(0);
		expect(result.text()).toContain("==> packages/stats");
	});

	test("local-ts mode dry-run includes packages/stats chunk", async () => {
		const result = await $`bun scripts/ci-test-ts.ts local-ts --dry-run`.cwd(repoRoot).quiet().nothrow();
		expect(result.exitCode).toBe(0);
		expect(result.text()).toContain("==> packages/stats");
	});
});
