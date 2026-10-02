import { describe, expect, it } from "bun:test";
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
}

interface DumpInfo {
	state: string;
	wchan: string;
	utime_ms: string;
	stime_ms: string;
	majflt: string;
	late_ms: string;
	rss_kb: string;
	hwm_kb: string;
	swap_kb: string;
	load: string;
	psi_cpu: string;
	psi_mem: string;
	psi_io: string;
}

interface WatchdogLogInfo {
	blockedMs: number;
	timeoutMs: number;
}

function stripAnsi(text: string): string {
	return text.replace(/\u001b\[[0-9;]*[a-zA-Z]/g, "");
}

function parseDumpLine(stderr: string): DumpInfo | null {
	const match = stderr.match(/\[stall-watchdog\] dump (.*)/);
	if (!match) return null;
	const line = match[1].trim();
	const pairs = line.split(/\s+/);
	const record: Record<string, string> = {};
	for (const pair of pairs) {
		const eq = pair.indexOf("=");
		if (eq !== -1) {
			record[pair.slice(0, eq)] = pair.slice(eq + 1);
		}
	}
	return record as unknown as DumpInfo;
}

function parseWatchdogLine(stderr: string): WatchdogLogInfo | null {
	const match = stderr.match(
		/\[stall-watchdog\] pid \d+: event loop blocked (\d+) ms; test running (?:\d+|-) ms; per-test timeout (\d+) ms; killing/,
	);
	if (!match) return null;
	return {
		blockedMs: Number(match[1]),
		timeoutMs: Number(match[2]),
	};
}

async function runChildBun(args: string[], cwd: string): Promise<ChildRunResult> {
	const t0 = performance.now();
	const proc = Bun.spawn(args, {
		cwd,
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

	return {
		exitCode: proc.exitCode,
		signalCode: proc.signalCode,
		stdout,
		stderr,
		cleanStderr: stripAnsi(stderr),
		durationMs,
	};
}

describe.skipIf(process.platform !== "linux")("stall-watchdog dump (OMP-512-s04)", () => {
	it("prints diagnostic dump line before kill for a blocked test process", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "pi-ai-stall-dump-"));
		try {
			const fixtureFile = path.join(tempDir, "sleep-block.test.ts");
			await fs.writeFile(
				fixtureFile,
				`import { test } from "bun:test";
test("blocks event loop", () => {
	Bun.sleepSync(120_000);
});
`,
			);

			const preload = path.resolve(import.meta.dir, "helpers/stall-watchdog.ts");
			const result = await runChildBun(
				["bun", "test", "--preload", preload, "--timeout=2000", fixtureFile],
				tempDir,
			);

			expect(result.exitCode !== 0 || result.signalCode !== null).toBe(true);

			const dumpIndex = result.cleanStderr.indexOf("[stall-watchdog] dump ");
			const killIndex = result.cleanStderr.indexOf("[stall-watchdog] pid ");
			expect(dumpIndex).toBeGreaterThanOrEqual(0);
			expect(killIndex).toBeGreaterThan(dumpIndex);

			const dump = parseDumpLine(result.cleanStderr);
			expect(dump).not.toBeNull();

			// state is S or D (not R)
			expect(["S", "D"]).toContain(dump!.state);

			// utime_ms is under 20% of the reported blocked ms
			const watchdogInfo = parseWatchdogLine(result.cleanStderr);
			expect(watchdogInfo).not.toBeNull();
			const utimeMs = Number(dump!.utime_ms);
			expect(Number.isNaN(utimeMs)).toBe(false);
			expect(utimeMs).toBeLessThan(0.2 * watchdogInfo!.blockedMs);

			// rss_kb, hwm_kb and majflt are integers
			expect(dump!.rss_kb).toMatch(/^\d+$/);
			expect(dump!.hwm_kb).toMatch(/^\d+$/);
			expect(dump!.majflt).toMatch(/^\d+$/);

			// late_ms is from 0 to 500
			const lateMs = Number(dump!.late_ms);
			expect(Number.isNaN(lateMs)).toBe(false);
			expect(lateMs).toBeGreaterThanOrEqual(0);
			expect(lateMs).toBeLessThanOrEqual(500);

			// other fields
			expect(dump!.wchan).toBeTruthy();
			expect(dump!.stime_ms).toMatch(/^\d+$/);
			expect(dump!.swap_kb).toMatch(/^\d+$/);
			expect(dump!.load).toMatch(/^([0-9.]+(,[0-9.]+){2}|na)$/);
			expect(dump!.psi_cpu).toMatch(/^(\d+\.\d+|na)$/);
			expect(dump!.psi_mem).toMatch(/^(\d+\.\d+|na)$/);
			expect(dump!.psi_io).toMatch(/^(\d+\.\d+|na)$/);
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	}, 15000);
});
