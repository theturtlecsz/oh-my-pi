import * as fs from "node:fs";

interface WatchdogConfig {
	sharedBuffer: SharedArrayBuffer;
	timeoutMs: number;
	pid?: number;
}

interface StatSample {
	state: string;
	utime: number;
	stime: number;
	majflt: number;
}

function readStat(pid: number): StatSample | null {
	try {
		const raw = fs.readFileSync(`/proc/self/task/${pid}/stat`, "utf8");
		const rParen = raw.lastIndexOf(")");
		if (rParen === -1) return null;
		const fields = raw
			.slice(rParen + 2)
			.trim()
			.split(/\s+/);
		const state = fields[0];
		const majflt = Number.parseInt(fields[9], 10);
		const utime = Number.parseInt(fields[11], 10);
		const stime = Number.parseInt(fields[12], 10);
		if (Number.isNaN(majflt) || Number.isNaN(utime) || Number.isNaN(stime)) {
			return null;
		}
		return { state, utime, stime, majflt };
	} catch {
		return null;
	}
}

function readWchan(pid: number): string {
	try {
		const raw = fs.readFileSync(`/proc/self/task/${pid}/wchan`, "utf8").trim();
		return raw.length > 0 ? raw : "na";
	} catch {
		return "na";
	}
}

function readStatusMemory(): { rss_kb: string; hwm_kb: string; swap_kb: string } {
	let rss_kb = "na";
	let hwm_kb = "na";
	let swap_kb = "na";
	try {
		const status = fs.readFileSync("/proc/self/status", "utf8");
		for (const line of status.split("\n")) {
			if (line.startsWith("VmRSS:")) {
				const match = line.match(/^VmRSS:\s*(\d+)\s*kB/);
				if (match) rss_kb = match[1];
			} else if (line.startsWith("VmHWM:")) {
				const match = line.match(/^VmHWM:\s*(\d+)\s*kB/);
				if (match) hwm_kb = match[1];
			} else if (line.startsWith("VmSwap:")) {
				const match = line.match(/^VmSwap:\s*(\d+)\s*kB/);
				if (match) swap_kb = match[1];
			}
		}
	} catch {}
	return { rss_kb, hwm_kb, swap_kb };
}

function readLoad(): string {
	try {
		const raw = fs.readFileSync("/proc/loadavg", "utf8").trim().split(/\s+/);
		if (raw.length >= 3) {
			return `${raw[0]},${raw[1]},${raw[2]}`;
		}
		return "na";
	} catch {
		return "na";
	}
}

function readPressureAvg10(type: "cpu" | "memory" | "io"): string {
	try {
		const content = fs.readFileSync(`/proc/pressure/${type}`, "utf8");
		for (const line of content.split("\n")) {
			if (line.startsWith("some ")) {
				const match = line.match(/\bavg10=([0-9.]+)/);
				if (match) return match[1];
			}
		}
		return "na";
	} catch {
		return "na";
	}
}

function startWatchdog(config: WatchdogConfig): void {
	const { sharedBuffer, timeoutMs: T, pid } = config;
	const state = new Int32Array(sharedBuffer);
	const BEAT_INDEX = 0;
	const TEST_SEQ_INDEX = 1;
	const IN_TEST_INDEX = 2;

	const G = 1000;
	const p = pid ?? process.pid;
	const isLinux = process.platform === "linux";

	let lastBeat = performance.now();
	let lastBeatVal = Atomics.load(state, BEAT_INDEX);
	let testStart: number | undefined = undefined;
	let lastTestSeqVal = Atomics.load(state, TEST_SEQ_INDEX);
	let killed = false;
	let lastSample: StatSample | null = isLinux ? readStat(p) : null;

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
			if (isLinux) {
				const sample = readStat(p);
				if (sample) {
					lastSample = sample;
				}
			}
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

			if (isLinux) {
				try {
					const deadline =
						r1 && r2
							? Math.min(testStart! + T + G, lastBeat + T + G)
							: r1
								? testStart! + T + G
								: lastBeat + T + G;
					const late_ms = Math.max(0, Math.round(now - deadline));

					const currentStat = readStat(p);
					const stateVal = currentStat?.state ?? "na";
					const utime_ms =
						currentStat && lastSample
							? String(Math.max(0, Math.round((currentStat.utime - lastSample.utime) * 10)))
							: "na";
					const stime_ms =
						currentStat && lastSample
							? String(Math.max(0, Math.round((currentStat.stime - lastSample.stime) * 10)))
							: "na";
					const majflt =
						currentStat && lastSample ? String(Math.max(0, currentStat.majflt - lastSample.majflt)) : "na";

					const wchan = readWchan(p);
					const { rss_kb, hwm_kb, swap_kb } = readStatusMemory();
					const load = readLoad();
					const psi_cpu = readPressureAvg10("cpu");
					const psi_mem = readPressureAvg10("memory");
					const psi_io = readPressureAvg10("io");

					fs.writeSync(
						2,
						`[stall-watchdog] dump state=${stateVal} wchan=${wchan} utime_ms=${utime_ms} stime_ms=${stime_ms} majflt=${majflt} late_ms=${late_ms} rss_kb=${rss_kb} hwm_kb=${hwm_kb} swap_kb=${swap_kb} load=${load} psi_cpu=${psi_cpu} psi_mem=${psi_mem} psi_io=${psi_io}\n`,
					);
				} catch {}
			}

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
