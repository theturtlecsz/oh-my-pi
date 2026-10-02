import * as fs from "node:fs";

interface WatchdogConfig {
	sharedBuffer: SharedArrayBuffer;
	timeoutMs: number;
	pid?: number;
}

function startWatchdog(config: WatchdogConfig): void {
	const { sharedBuffer, timeoutMs: T, pid } = config;
	const state = new Int32Array(sharedBuffer);
	const BEAT_INDEX = 0;
	const TEST_SEQ_INDEX = 1;
	const IN_TEST_INDEX = 2;

	const G = 1000;
	const p = pid ?? process.pid;

	let lastBeat = performance.now();
	let lastBeatVal = Atomics.load(state, BEAT_INDEX);
	let testStart: number | undefined = undefined;
	let lastTestSeqVal = Atomics.load(state, TEST_SEQ_INDEX);
	let killed = false;

	postMessage("ready");

	setInterval(() => {
		postMessage("tick");

		const now = performance.now();
		const currentBeat = Atomics.load(state, BEAT_INDEX);
		const currentTestSeq = Atomics.load(state, TEST_SEQ_INDEX);
		const inTest = Atomics.load(state, IN_TEST_INDEX) === 1;

		if (currentBeat !== lastBeatVal) {
			lastBeat = now;
			lastBeatVal = currentBeat;
		}

		if (currentTestSeq !== lastTestSeqVal) {
			testStart = now;
			lastTestSeqVal = currentTestSeq;
			lastBeat = now;
		}

		const r1 = inTest && testStart !== undefined && now >= testStart + T + G && lastBeat < testStart + T;

		const r2 = now - lastBeat >= T + G;

		if ((r1 || r2) && !killed) {
			killed = true;
			const b = Math.round(now - lastBeat);
			const runningStr = inTest && testStart !== undefined ? String(Math.round(now - testStart)) : "-";
			fs.writeSync(
				2,
				`[stall-watchdog] pid ${p}: event loop blocked ${b} ms; test running ${runningStr} ms; per-test timeout ${T} ms; killing\n`,
			);
			try {
				process.kill(p, "SIGKILL");
			} catch {
				process.kill(process.pid, "SIGKILL");
			}
		}
	}, 100);
}

const onMessage = (event: unknown): void => {
	const payload = (
		typeof event === "object" && event !== null && "data" in event ? (event as { data: unknown }).data : event
	) as WatchdogConfig | undefined;
	if (payload?.sharedBuffer) {
		startWatchdog(payload);
	}
};

if (typeof addEventListener === "function") {
	addEventListener("message", onMessage);
} else {
	(globalThis as unknown as { onmessage?: (event: unknown) => void }).onmessage = onMessage;
}
