import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { pathToFileURL } from "node:url";

const loggerModuleUrl = pathToFileURL(path.join(import.meta.dir, "../src/logger.ts")).href;
const roots: string[] = [];

afterEach(async () => {
	await Promise.all(roots.splice(0).map(root => fs.rm(root, { recursive: true, force: true })));
});

async function makeProbe(logsDir: string): Promise<string> {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "omp-logger-probe-"));
	roots.push(root);
	const releasePath = path.join(logsDir, ".release");
	const probePath = path.join(root, "probe.ts");
	await Bun.write(
		probePath,
		`import * as fs from "node:fs";\n` +
			`import { info, setTransports } from ${JSON.stringify(loggerModuleUrl)};\n` +
			`setTransports({ file: ${JSON.stringify(logsDir)} });\n` +
			`info("multiprocess probe");\n` +
			`fs.writeSync(1, "ready\\n");\n` +
			`await new Promise<void>(resolve => {\n` +
			`\tconst watcher = fs.watch(${JSON.stringify(logsDir)}, (_event, name) => {\n` +
			`\t\tif (name !== ".release") return;\n` +
			`\t\twatcher.close();\n` +
			`\t\tresolve();\n` +
			`\t});\n` +
			`\tif (fs.existsSync(${JSON.stringify(releasePath)})) {\n` +
			`\t\twatcher.close();\n` +
			`\t\tresolve();\n` +
			`\t}\n` +
			`});\n` +
			`setTransports({ file: false });\n`,
	);
	return probePath;
}

describe("multiprocess file logging", () => {
	it("prunes completed PID namespaces across short-lived invocations and caps at 20 logs per day", async () => {
		const logsDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-logger-retention-"));
		roots.push(logsDir);
		const deadPids = Array.from({ length: 25 }, (_, i) => 9_000_001 + i);

		await Bun.write(path.join(logsDir, ".release"), "");
		const probePath = await makeProbe(logsDir);
		const seed = Bun.spawn([process.execPath, probePath], {
			stdin: "pipe",
			stdout: "ignore",
			stderr: "pipe",
		});
		seed.stdin.end();
		expect(await seed.exited).toBe(0);
		const seedLog = (await fs.readdir(logsDir)).find(name => name.endsWith(`.${seed.pid}.log`));
		const seedDate = seedLog?.match(/^omp\.(\d{4}-\d{2}-\d{2})\./)?.[1];
		if (!seedDate) throw new Error("probe did not create a dated log");
		const baseDate = new Date(`${seedDate}T12:00:00`);
		const localDate = (daysAgo: number): string => {
			const date = new Date(baseDate);
			date.setDate(date.getDate() - daysAgo);
			return (
				`${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-` +
				String(date.getDate()).padStart(2, "0")
			);
		};

		// 1. Multi-day retention across 5 days (days 2..4 survive, -1 and 5 expire)
		const multiDayPids = [9_000_101, 9_000_102];
		const multiDayRetained: string[] = [];
		const multiDayExpired: string[] = [];
		for (const pid of multiDayPids) {
			for (const daysAgo of [-1, 2, 3, 4, 5]) {
				const name = `omp.${localDate(daysAgo)}.${pid}.log`;
				await Bun.write(path.join(logsDir, name), name);
				await fs.utimes(path.join(logsDir, name), 2, 2);
				(daysAgo > 0 && daysAgo < 5 ? multiDayRetained : multiDayExpired).push(name);
			}
			await Bun.write(path.join(logsDir, `.omp.${pid}-audit.json`), "{}");
		}

		// 2. Day-level cap: on localDate(1), seed 25 dead PID logs with increasing mtimes
		const capDay = localDate(1);

		const dayCapRetained: string[] = [];
		const dayCapExpired: string[] = [];
		for (let i = 0; i < deadPids.length; i++) {
			const pid = deadPids[i];
			const name = `omp.${capDay}.${pid}.log`;
			await Bun.write(path.join(logsDir, name), name);
			const mtimeSec = 1000 + i * 10;
			await fs.utimes(path.join(logsDir, name), mtimeSec, mtimeSec);
			if (i >= deadPids.length - 20) {
				dayCapRetained.push(name);
			} else {
				dayCapExpired.push(name);
			}
			await Bun.write(path.join(logsDir, `.omp.${pid}-audit.json`), "{}");
		}
		let currentPid = 0;
		for (let restart = 0; restart < 2; restart++) {
			const current = Bun.spawn([process.execPath, probePath], {
				stdin: "pipe",
				stdout: "ignore",
				stderr: "pipe",
			});
			current.stdin.end();
			expect(await current.exited).toBe(0);
			currentPid = current.pid;
		}

		const entries = await fs.readdir(logsDir);
		for (const expected of multiDayRetained) expect(entries).toContain(expected);
		for (const expired of multiDayExpired) expect(entries).not.toContain(expired);
		for (const expected of dayCapRetained) expect(entries).toContain(expected);
		for (const expired of dayCapExpired) expect(entries).not.toContain(expired);
		const deadOnCapDay = entries.filter(name => name.startsWith(`omp.${capDay}.90000`));
		expect(deadOnCapDay).toHaveLength(20);
		expect(entries.filter(name => name.endsWith("-audit.json"))).toEqual([`.omp.${currentPid}-audit.json`]);
	});
});
