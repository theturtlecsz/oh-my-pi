import { afterEach, expect, test } from "bun:test";
import { registerNativeTokioShutdown, shutdownNativeTokioRuntime } from "./loader-state.js";

const hook = Symbol.for("omp.natives.shutdownTokioRuntime");
const previous = Object.getOwnPropertyDescriptor(globalThis, hook);
afterEach(() => {
	if (previous) Object.defineProperty(globalThis, hook, previous);
	else Reflect.deleteProperty(globalThis, hook);
});

test("old add-ons report unavailable shutdown", () => {
	registerNativeTokioShutdown({});
	expect(shutdownNativeTokioRuntime()).toBe("unavailable");
});

test("loader exposes native shutdown to the process exit hook", () => {
	let calls = 0;
	registerNativeTokioShutdown({ __ompShutdownTokioRuntime: () => { calls++; } });
	expect(shutdownNativeTokioRuntime()).toBe("stopped");
	expect(calls).toBe(1);
	const shutdown = Reflect.get(globalThis, hook);
	expect(shutdown()).toBe("stopped");
	expect(calls).toBe(2);
});

test("loader propagates native shutdown failure with live tids", () => {
	registerNativeTokioShutdown({ __ompShutdownTokioRuntime: () => { throw new Error("tid 123 still alive"); } });
	expect(() => shutdownNativeTokioRuntime()).toThrow("tid 123 still alive");
});
