import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { $ } from "bun";

const repoRoot = path.resolve(import.meta.dir, "..");

describe("ci-test-ts --skip-session-system", () => {
	test("local-ts mode with --skip-session-system prints no session-system/tests and still prints packages/stats", async () => {
		const result = await $`bun scripts/ci-test-ts.ts local-ts --dry-run --skip-session-system`
			.cwd(repoRoot)
			.quiet()
			.nothrow();
		expect(result.exitCode).toBe(0);
		expect(result.text()).not.toContain("==> session-system/tests");
		expect(result.text()).toContain("==> packages/stats");
	});

	test("local mode with --skip-session-system prints no session-system/tests", async () => {
		const result = await $`bun scripts/ci-test-ts.ts local --dry-run --skip-session-system`
			.cwd(repoRoot)
			.quiet()
			.nothrow();
		expect(result.exitCode).toBe(0);
		expect(result.text()).not.toContain("==> session-system/tests");
	});

	test("local mode dry-run without --skip-session-system prints session-system/tests", async () => {
		const result = await $`bun scripts/ci-test-ts.ts local --dry-run`.cwd(repoRoot).quiet().nothrow();
		expect(result.exitCode).toBe(0);
		expect(result.text()).toContain("==> session-system/tests");
	});

	test("local-ts mode dry-run without --skip-session-system prints session-system/tests", async () => {
		const result = await $`bun scripts/ci-test-ts.ts local-ts --dry-run`.cwd(repoRoot).quiet().nothrow();
		expect(result.exitCode).toBe(0);
		expect(result.text()).toContain("==> session-system/tests");
	});

	test("native mode with --skip-session-system still prints session-system/tests", async () => {
		const result = await $`bun scripts/ci-test-ts.ts native --dry-run --skip-session-system`
			.cwd(repoRoot)
			.quiet()
			.nothrow();
		expect(result.exitCode).toBe(0);
		expect(result.text()).toContain("==> session-system/tests");
	});
});
