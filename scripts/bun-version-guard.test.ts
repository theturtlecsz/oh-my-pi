import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { bunVersionMismatch } from "./bun-version-guard.ts";

describe("bunVersionMismatch", () => {
	test("returns message containing both versions on mismatch against fixed version (1.3.14 vs bun@1.4.0)", () => {
		const result = bunVersionMismatch("1.3.14", "bun@1.4.0");
		expect(result).not.toBeNull();
		expect(result).toContain("1.3.14");
		expect(result).toContain("bun@1.4.0");
		expect(result).toBe(
			"bun 1.3.14 is running but package.json packageManager is bun@1.4.0 (major.minor 1.3 != 1.4)",
		);
	});

	test("returns message containing both versions on mismatch against range (1.3.14 vs bun@>=1.4)", () => {
		const result = bunVersionMismatch("1.3.14", "bun@>=1.4");
		expect(result).not.toBeNull();
		expect(result).toContain("1.3.14");
		expect(result).toContain("bun@>=1.4");
		expect(result).toBe(
			"bun 1.3.14 is running but package.json packageManager is bun@>=1.4 (major.minor 1.3 != 1.4)",
		);
	});

	test("returns message containing both versions on mismatch when running is newer major.minor (1.5.0 vs bun@>=1.4)", () => {
		const result = bunVersionMismatch("1.5.0", "bun@>=1.4");
		expect(result).not.toBeNull();
		expect(result).toContain("1.5.0");
		expect(result).toContain("bun@>=1.4");
		expect(result).toBe("bun 1.5.0 is running but package.json packageManager is bun@>=1.4 (major.minor 1.5 != 1.4)");
	});

	test("returns null when major.minor match (1.4.2 vs bun@>=1.4)", () => {
		expect(bunVersionMismatch("1.4.2", "bun@>=1.4")).toBeNull();
	});

	test("returns null when major.minor match (1.4.0 vs bun@1.4.0)", () => {
		expect(bunVersionMismatch("1.4.0", "bun@1.4.0")).toBeNull();
	});

	test("returns null for various matching comparator patterns", () => {
		expect(bunVersionMismatch("1.4.1", "bun@>=1.4.0")).toBeNull();
		expect(bunVersionMismatch("1.4.5", "bun@^1.4.0")).toBeNull();
		expect(bunVersionMismatch("1.4.3", "bun@~1.4.0")).toBeNull();
		expect(bunVersionMismatch("1.4.2", "bun@1.4")).toBeNull();
		expect(bunVersionMismatch("v1.4.2", "bun@>=1.4")).toBeNull();
	});

	test("returns message for non-bun spec", () => {
		const npmResult = bunVersionMismatch("1.4.2", "npm@10.0.0");
		expect(npmResult).not.toBeNull();
		expect(npmResult).toContain("npm@10.0.0");

		const pnpmResult = bunVersionMismatch("1.4.2", "pnpm@9.0.0");
		expect(pnpmResult).not.toBeNull();
		expect(pnpmResult).toContain("pnpm@9.0.0");

		const yarnResult = bunVersionMismatch("1.4.2", "yarn@4.0.0");
		expect(yarnResult).not.toBeNull();
		expect(yarnResult).toContain("yarn@4.0.0");

		const bareBunResult = bunVersionMismatch("1.4.2", "bun");
		expect(bareBunResult).not.toBeNull();
		expect(bareBunResult).toContain("bun");

		const emptyResult = bunVersionMismatch("1.4.2", "");
		expect(emptyResult).not.toBeNull();
	});

	test("returns message when bun spec has no major.minor", () => {
		const latestResult = bunVersionMismatch("1.4.2", "bun@latest");
		expect(latestResult).not.toBeNull();
		expect(latestResult).toContain("bun@latest");

		const emptySpecResult = bunVersionMismatch("1.4.2", "bun@");
		expect(emptySpecResult).not.toBeNull();
		expect(emptySpecResult).toContain("bun@");

		const majorOnlyResult = bunVersionMismatch("1.4.2", "bun@1");
		expect(majorOnlyResult).not.toBeNull();
		expect(majorOnlyResult).toContain("bun@1");
	});

	test("ci-test-ts skips guard under --dry-run", async () => {
		const proc = Bun.spawn(["bun", "scripts/ci-test-ts.ts", "local", "--dry-run"], {
			cwd: path.join(import.meta.dir, ".."),
			stdout: "pipe",
			stderr: "pipe",
		});
		const exitCode = await proc.exited;
		expect(exitCode).toBe(0);
	});
});
