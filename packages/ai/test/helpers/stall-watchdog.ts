import { afterEach, beforeEach } from "bun:test";
import { resolveRunTimeoutMs } from "./stall-watchdog-timeout";

const timeoutMs = resolveRunTimeoutMs();

if (timeoutMs !== undefined) {
	const sharedBuffer = new SharedArrayBuffer(3 * Int32Array.BYTES_PER_ELEMENT);
	const state = new Int32Array(sharedBuffer);
	const BEAT_INDEX = 0;
	const TEST_SEQ_INDEX = 1;
	const IN_TEST_INDEX = 2;

	const workerUrl = new URL("./stall-watchdog-thread.ts", import.meta.url).href;
	const worker = new Worker(workerUrl, { type: "module" });
	worker.unref();

	const { promise: readyPromise, resolve: resolveReady } = Promise.withResolvers<void>();

	worker.onmessage = event => {
		const data = event.data;
		if (data === "ready") {
			resolveReady();
			return;
		}
		if (data === "tick") {
			Atomics.add(state, BEAT_INDEX, 1);
			return;
		}
	};

	worker.postMessage({
		sharedBuffer,
		timeoutMs,
		pid: process.pid,
	});

	await readyPromise;

	beforeEach(() => {
		Atomics.add(state, TEST_SEQ_INDEX, 1);
		Atomics.add(state, BEAT_INDEX, 1);
		Atomics.store(state, IN_TEST_INDEX, 1);
	});

	afterEach(() => {
		Atomics.store(state, IN_TEST_INDEX, 0);
	});
}
