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

async function runChildBun(args: string[], cwd?: string): Promise<ChildRunResult> {
	const t0 = performance.now();
	const proc = Bun.spawn(args, {
		// Default to the package root so the package `bunfig.toml` preload is
		// inherited by the child — that preload is what activates the watchdog.
		cwd: cwd ?? path.resolve(import.meta.dir, ".."),
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

		const result = await runChildBun(["bun", "test", "--timeout=2000", fixtureFile]);

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

		const result = await runChildBun(["bun", "test", "--timeout=2000", fixtureFile]);

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
		const result = await runChildBun(["bun", "test", "--timeout=2000", fixtureFile]);

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

		const result = await runChildBun(["bun", "test", "--timeout=2000", fixtureFile]);

		expect(result.exitCode !== 0 || result.signalCode !== null).toBe(true);
		const info = parseWatchdogLine(result.stderr);
		expect(info).not.toBeNull();
		expect(info!.runningStr).toBe("-");

		const markRaw = Number(await fs.readFile(markFile, "utf8"));
		const elapsedAfterMark = result.exitTimeOrigin - markRaw;
		expect(elapsedAfterMark).toBeGreaterThanOrEqual(3000);
		expect(elapsedAfterMark).toBeLessThanOrEqual(5000);
	});

	it("passes a 1000 ms Bun.sleepSync inside an async test under --timeout=2000", async () => {
		const fixtureFile = path.join(tempDir, "sleep-sync-async.test.ts");

		await fs.writeFile(
			fixtureFile,
			`import { it } from "bun:test";

it("sleepSync inside async test", async () => {
	Bun.sleepSync(1000);
});
`,
		);

		const result = await runChildBun(["bun", "test", "--timeout=2000", fixtureFile]);
		const combined = `${result.stdout}${result.stderr}`;

		expect(combined).not.toContain("[stall-watchdog]");
		expect(result.exitCode).toBe(0);
		expect(result.signalCode).toBeNull();
		expect(combined).toContain("1 pass");
		expect(combined).toContain("0 fail");
	});

	it("passes await Bun.sleep(3000) in it(..., 6000) under --timeout=2000", async () => {
		const fixtureFile = path.join(tempDir, "async-sleep-own-timeout.test.ts");

		await fs.writeFile(
			fixtureFile,
			`import { it } from "bun:test";

it("async sleep within its own timeout", async () => {
	await Bun.sleep(3000);
}, 6000);
`,
		);

		const result = await runChildBun(["bun", "test", "--timeout=2000", fixtureFile]);
		const combined = `${result.stdout}${result.stderr}`;

		expect(combined).not.toContain("[stall-watchdog]");
		expect(result.exitCode).toBe(0);
		expect(result.signalCode).toBeNull();
		expect(combined).toContain("1 pass");
		expect(combined).toContain("0 fail");
	});

	it("reports Bun's timeout for a never-settling it(..., 1500) under --timeout=20000", async () => {
		const fixtureFile = path.join(tempDir, "never-settles.test.ts");

		await fs.writeFile(
			fixtureFile,
			`import { it } from "bun:test";

it("never settles", async () => {
	await new Promise(() => {});
}, 1500);
`,
		);

		const result = await runChildBun(["bun", "test", "--timeout=20000", fixtureFile]);
		const combined = `${result.stdout}${result.stderr}`;

		expect(combined).not.toContain("[stall-watchdog]");
		expect(result.exitCode).toBeGreaterThan(0);
		expect(result.signalCode).toBeNull();
		expect(combined).toContain("this test timed out after 1500ms");
		expect(result.durationMs).toBeGreaterThanOrEqual(1500);
		expect(result.durationMs).toBeLessThanOrEqual(6000);
	});

	it("kills a blocked worker under --parallel=2 and still passes ok.test.ts", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "pi-ai-stall-parallel-"));
		try {
			await fs.writeFile(
				path.join(dir, "stuck.test.ts"),
				`import { test } from "bun:test";

test("stuck", () => {
	Bun.sleepSync(120_000);
});
`,
			);
			await fs.writeFile(
				path.join(dir, "ok.test.ts"),
				`import { test } from "bun:test";

test("ok", () => {});
`,
			);

			const preload = path.resolve(import.meta.dir, "helpers/stall-watchdog.ts");
			const result = await runChildBun(["bun", "test", "--parallel=2", "--preload", preload, "--timeout=2000"], dir);
			const combined = stripAnsi(`${result.stdout}${result.stderr}`);

			expect(result.exitCode).toBe(1);
			expect(result.signalCode).toBeNull();
			expect(result.durationMs).toBeLessThan(15000);
			expect(combined).toContain("1 pass");
			expect(combined).not.toContain("✗ ok.test.ts");
			const info = parseWatchdogLine(result.stderr);
			expect(info).not.toBeNull();
			expect(info!.timeoutMs).toBe(2000);
			expect(combined).toContain("worker crashed: SIGKILL");
		} finally {
			await fs.rm(dir, { recursive: true, force: true });
		}
	}, 30_000);
});
