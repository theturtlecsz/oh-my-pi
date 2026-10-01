import { describe, expect, it } from "bun:test";
import * as os from "node:os";
import { defaultReadCmdline, resolveRunTimeoutMs } from "./helpers/stall-watchdog-timeout";

describe("stall-watchdog-timeout resolution (OMP-512-s01)", () => {
	it("resolves from argv using --timeout=N spelling", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test", "--timeout=2500"],
			readCmdline: () => undefined,
			ppid: 100,
		});
		expect(timeout).toBe(2500);
	});

	it("resolves from argv using --timeout N spelling", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test", "--timeout", "3500"],
			readCmdline: () => undefined,
			ppid: 100,
		});
		expect(timeout).toBe(3500);
	});

	it("prefers the first timeout flag when multiple flags appear in argv", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test", "--timeout=1200", "--timeout", "4500"],
			readCmdline: () => undefined,
			ppid: 100,
		});
		expect(timeout).toBe(1200);
	});

	it("resolves from argv even if cmdline readers return undefined", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test", "--timeout=2000"],
			readCmdline: () => undefined,
			ppid: 100,
		});
		expect(timeout).toBe(2000);
	});

	it("a self cmdline wins over the parent's cmdline", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test"],
			readCmdline: pid => (pid === "self" ? ["bun", "test", "--timeout=1000"] : ["bun", "test", "--timeout=3000"]),
			ppid: 200,
		});
		expect(timeout).toBe(1000);
	});

	it("a self cmdline with space spelling wins over the parent's cmdline", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test"],
			readCmdline: pid =>
				pid === "self" ? ["bun", "test", "--timeout", "1500"] : ["bun", "test", "--timeout=3000"],
			ppid: 200,
		});
		expect(timeout).toBe(1500);
	});

	it("a parent-only flag resolves when self cmdline has no timeout flag", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test"],
			readCmdline: pid =>
				pid === "self" ? ["bun", "test", "worker.ts"] : ["bun", "test", "--parallel=6", "--timeout=30000"],
			ppid: 200,
		});
		expect(timeout).toBe(30000);
	});

	it("a parent-only flag with space spelling resolves when self cmdline has no timeout flag", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test"],
			readCmdline: pid =>
				pid === "self" ? ["bun", "test", "worker.ts"] : ["bun", "test", "--parallel=6", "--timeout", "30000"],
			ppid: 200,
		});
		expect(timeout).toBe(30000);
	});

	it("readable cmdlines with no flag give Bun's default 5000 ms", () => {
		const timeoutBoth = resolveRunTimeoutMs({
			argv: ["bun", "test"],
			readCmdline: () => ["bun", "test"],
			ppid: 200,
		});
		expect(timeoutBoth).toBe(5000);

		const timeoutSelfOnly = resolveRunTimeoutMs({
			argv: ["bun", "test"],
			readCmdline: pid => (pid === "self" ? ["bun", "test"] : undefined),
			ppid: 200,
		});
		expect(timeoutSelfOnly).toBe(5000);

		const timeoutParentOnly = resolveRunTimeoutMs({
			argv: ["bun", "test"],
			readCmdline: pid => (pid === "self" ? undefined : ["bun", "test"]),
			ppid: 200,
		});
		expect(timeoutParentOnly).toBe(5000);
	});

	it("unreadable cmdlines give undefined when argv has no flag", () => {
		const timeout = resolveRunTimeoutMs({
			argv: ["bun", "test"],
			readCmdline: () => undefined,
			ppid: 200,
		});
		expect(timeout).toBeUndefined();
	});

	it("defaultReadCmdline reads self cmdline on Linux", () => {
		if (os.platform() !== "linux") return;
		const selfArgs = defaultReadCmdline("self");
		expect(selfArgs).toBeDefined();
		expect(Array.isArray(selfArgs)).toBe(true);
		expect(selfArgs!.length).toBeGreaterThan(0);
	});

	it("defaultReadCmdline returns undefined for non-positive or invalid PIDs", () => {
		expect(defaultReadCmdline(0)).toBeUndefined();
		expect(defaultReadCmdline(-1)).toBeUndefined();
		expect(defaultReadCmdline(Number.NaN)).toBeUndefined();
	});

	it("resolveRunTimeoutMs with default source resolves on Linux", () => {
		if (os.platform() !== "linux") return;
		const timeout = resolveRunTimeoutMs();
		expect(typeof timeout === "number" || timeout === undefined).toBe(true);
		if (typeof timeout === "number") {
			expect(timeout).toBeGreaterThan(0);
		}
	});
});
