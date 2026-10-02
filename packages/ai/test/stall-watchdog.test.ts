import { afterAll, beforeAll, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

interface ChildRunResult {
	exitCode: number | null;
	signalCode: string | null;
	stdout: string;
	stderr: string;
	cleanStderr: string;
	durationMs: number;
	exitTimeOrigin: number;
}

interface WatchdogLogInfo {
	pid: number;
	blockedMs: number;
	runningStr: string;
	runningMs: number | null;
	timeoutMs: number;
}

function stripAnsi(text: string): string {
	return text.replace(/\u001b\[[0-9;]*[a-zA-Z]/g, "");
}

function parseWatchdogLine(stderr: string): WatchdogLogInfo | null {
	const match = stderr.match(
		/\[stall-watchdog\] pid (\d+): event loop blocked (\d+) ms; test running (\d+|-) ms; per-test timeout (\d+) ms; killing/,
	);
	if (!match) return null;
	const runningStr = match[3] ?? "-";
	return {
		pid: Number(match[1]),
		blockedMs: Number(match[2]),
		runningStr,
		runningMs: runningStr === "-" ? null : Number(runningStr),
		timeoutMs: Number(match[4]),
	};
}

async function runChildBun(args: string[]): Promise<ChildRunResult> {
	const t0 = performance.now();
	const proc = Bun.spawn(args, {
		stdout: "pipe",
		stderr: "pipe",
		env: {
			...process.env,
			NO_COLOR: "1",
		},
	});
	const pid = proc.pid;

	const killTimer = setTimeout(() => {
		try {
			process.kill(pid, "SIGKILL");
		} catch {}
	}, 15000);

	const [stdout, stderr] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text()]);
	await proc.exited;
	clearTimeout(killTimer);
	const durationMs = performance.now() - t0;
	const exitTimeOrigin = performance.timeOrigin + performance.now();

	return {
		exitCode: proc.exitCode,
		signalCode: proc.signalCode,
		stdout,
		stderr,
		cleanStderr: stripAnsi(stderr),
		durationMs,
		exitTimeOrigin,
	};
}

describe("stall-watchdog (OMP-512-s02)", () => {
	let tempDir: string;
	const preloadPath = path.resolve(import.meta.dir, "./helpers/stall-watchdog.ts");

	beforeAll(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "pi-ai-stall-watchdog-"));
	});

	afterAll(async () => {
		if (tempDir) {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});

	it("kills in bound: a block at test start", async () => {
		const fixtureFile = path.join(tempDir, "block-at-start.test.ts");
		const markFile = path.join(tempDir, "block-at-start.mark");

		await fs.writeFile(
			fixtureFile,
			`import { test } from "bun:test";
import * as fs from "node:fs";

test("block at test start", () => {
	fs.writeFileSync(${JSON.stringify(markFile)}, String(performance.timeOrigin + performance.now()));
	Bun.sleepSync(120_000);
});
`,
		);

		const result = await runChildBun(["bun", "test", `--preload=${preloadPath}`, "--timeout=2000", fixtureFile]);

		expect(result.exitCode !== 0 || result.signalCode !== null).toBe(true);
		const info = parseWatchdogLine(result.stderr);
		expect(info).not.toBeNull();
		expect(info!.runningMs).not.toBeNull();
		expect(info!.runningMs!).toBeGreaterThanOrEqual(3000);
		expect(info!.runningMs!).toBeLessThanOrEqual(3500);

		const markRaw = Number(await fs.readFile(markFile, "utf8"));
		const elapsedAfterMark = result.exitTimeOrigin - markRaw;
		expect(elapsedAfterMark).toBeLessThanOrEqual(5000);
	});

	it("kills in bound: a block after await Bun.sleep(1500)", async () => {
		const fixtureFile = path.join(tempDir, "block-after-async.test.ts");
		const markFile = path.join(tempDir, "block-after-async.mark");

		await fs.writeFile(
			fixtureFile,
			`import { test } from "bun:test";
import * as fs from "node:fs";

test("block after async sleep", async () => {
	await Bun.sleep(1500);
	fs.writeFileSync(${JSON.stringify(markFile)}, String(performance.timeOrigin + performance.now()));
	Bun.sleepSync(120_000);
});
`,
		);

		const result = await runChildBun(["bun", "test", `--preload=${preloadPath}`, "--timeout=2000", fixtureFile]);

		expect(result.exitCode !== 0 || result.signalCode !== null).toBe(true);
		const info = parseWatchdogLine(result.stderr);
		expect(info).not.toBeNull();
		expect(info!.runningMs).not.toBeNull();
		expect(info!.runningMs!).toBeGreaterThanOrEqual(3000);
		expect(info!.runningMs!).toBeLessThanOrEqual(3500);

		const markRaw = Number(await fs.readFile(markFile, "utf8"));
		const elapsedAfterMark = result.exitTimeOrigin - markRaw;
		expect(elapsedAfterMark).toBeLessThanOrEqual(5000);
	});

	it("kills in bound: a block after useFakeTimers() + setSystemTime(0)", async () => {
		const fixtureFile = path.join(tempDir, "block-after-fake-timers.test.ts");
		const markFile = path.join(tempDir, "block-after-fake-timers.mark");

		await fs.writeFile(
			fixtureFile,
			`import { test, jest, setSystemTime } from "bun:test";
import * as fs from "node:fs";

test("block after fake timers", () => {
	jest.useFakeTimers();
	setSystemTime(0);
	fs.writeFileSync(${JSON.stringify(markFile)}, String(performance.timeOrigin + performance.now()));
	Bun.sleepSync(120_000);
});
`,
		);

		const spawnTimeOrigin = performance.timeOrigin + performance.now();
		const result = await runChildBun(["bun", "test", `--preload=${preloadPath}`, "--timeout=2000", fixtureFile]);

		expect(result.exitCode !== 0 || result.signalCode !== null).toBe(true);
		const info = parseWatchdogLine(result.stderr);
		expect(info).not.toBeNull();
		expect(info!.runningMs).not.toBeNull();
		expect(info!.runningMs!).toBeGreaterThanOrEqual(3000);
		expect(info!.runningMs!).toBeLessThanOrEqual(3500);

		const markRaw = Number(await fs.readFile(markFile, "utf8"));
		const markTime = markRaw <= 100_000 ? spawnTimeOrigin : markRaw;
		const elapsedAfterMark = result.exitTimeOrigin - markTime;
		expect(elapsedAfterMark).toBeLessThanOrEqual(5000);
	});

	it("kills a top-level module block with '-' 3000-5000 ms after the mark", async () => {
		const fixtureFile = path.join(tempDir, "block-top-level.test.ts");
		const markFile = path.join(tempDir, "block-top-level.mark");

		await fs.writeFile(
			fixtureFile,
			`import * as fs from "node:fs";
fs.writeFileSync(${JSON.stringify(markFile)}, String(performance.timeOrigin + performance.now()));
Bun.sleepSync(120_000);
`,
		);

		const result = await runChildBun(["bun", "test", `--preload=${preloadPath}`, "--timeout=2000", fixtureFile]);

		expect(result.exitCode !== 0 || result.signalCode !== null).toBe(true);
		const info = parseWatchdogLine(result.stderr);
		expect(info).not.toBeNull();
		expect(info!.runningStr).toBe("-");

		const markRaw = Number(await fs.readFile(markFile, "utf8"));
		const elapsedAfterMark = result.exitTimeOrigin - markRaw;
		expect(elapsedAfterMark).toBeGreaterThanOrEqual(3000);
		expect(elapsedAfterMark).toBeLessThanOrEqual(5000);
	});
});
