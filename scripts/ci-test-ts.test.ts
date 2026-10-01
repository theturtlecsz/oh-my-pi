import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ptree, TempDir } from "@oh-my-pi/pi-utils";
import {
	assertHostPackageJsonUnchanged,
	buildChildEnv,
	buildTimingRecords,
	describeChunkFailure,
	formatTimingTable,
	readHostPackageJson,
	selectShard,
	shouldPrintTimingTable,
	withTempAgentDir,
	writeTimingJson,
} from "./ci-test-ts.ts";

describe("test runner watchdog", () => {
	// Parent fake timers cannot drive the real watchdog inside the isolated runner process.
	test("kills a stalled chunk, reports failure, and continues the queue", async () => {
		using dir = TempDir.createSync("omp-test-runner-watchdog-");
		const started = dir.join("started");
		const completed = dir.join("completed");
		const continued = dir.join("continued");
		const stalledCommand = [
			process.execPath,
			"-e",
			`await Bun.write(${JSON.stringify(started)}, "started"); await Bun.sleep(60_000); await Bun.write(${JSON.stringify(completed)}, "completed");`,
		];
		const nextCommand = [process.execPath, "-e", `await Bun.write(${JSON.stringify(continued)}, "continued");`];
		const commands = [
			{ label: "stalled chunk", cwd: ".", command: stalledCommand },
			{ label: "following chunk", cwd: ".", command: nextCommand },
		];
		const result = await ptree.exec(
			[
				process.execPath,
				"-e",
				`import { runTestCommandsInParallel } from ${JSON.stringify(import.meta.resolve("./ci-test-ts.ts"))}; await runTestCommandsInParallel(${JSON.stringify(commands)}, 1);`,
			],
			{
				env: { ...Bun.env, OMP_TEST_CHUNK_TIMEOUT: "1", NO_COLOR: "1" },
				timeout: 10_000,
				detached: true,
				allowNonZero: true,
			},
		);

		expect(result.exitCode).toBe(1);
		expect(result.stdout).toContain("[watchdog]");
		expect(await Bun.file(started).exists()).toBe(true);
		expect(await Bun.file(completed).exists()).toBe(false);
		expect(await Bun.file(continued).text()).toBe("continued");
	}, 15_000);
});

describe("OMP_TEST_SHARD", () => {
	test("shards partition every chunk exactly once, balanced to within one", () => {
		const chunks = Array.from({ length: 79 }, (_, i) => i);
		const shards = [1, 2, 3].map(i => selectShard(chunks, `${i}/3`));
		expect(shards.flat().sort((a, b) => a - b)).toEqual(chunks);
		const sizes = shards.map(s => s.length);
		expect(Math.max(...sizes) - Math.min(...sizes)).toBeLessThanOrEqual(1);
		expect(selectShard(chunks, "1/1")).toEqual(chunks);
		expect(selectShard(chunks, undefined)).toEqual(chunks);
	});

	test("rejects malformed specs instead of running an empty or partial shard", () => {
		for (const spec of ["0/2", "3/2", "1/0", "2", "a/b", "1/2/3"]) {
			expect(() => selectShard([1, 2, 3], spec)).toThrow("Invalid OMP_TEST_SHARD");
		}
	});

	test("rejects a shard that selects no chunks", () => {
		expect(() => selectShard([1], "2/2")).toThrow("selects no chunks");
		expect(() => selectShard([], "1/1")).toThrow("selects no chunks");
	});
});

describe("buildChildEnv", () => {
	test("overrides parent PI_CODING_AGENT_DIR with provided agentDir", () => {
		const baseEnv = {
			PI_CODING_AGENT_DIR: "/parent/agent/dir",
			SOME_OTHER_VAR: "kept",
			AWS_SECRET_ACCESS_KEY: "scrubbed",
		};
		const env = buildChildEnv({ agentDir: "/child/temp/agent/dir", baseEnv });
		expect(env.PI_CODING_AGENT_DIR).toBe("/child/temp/agent/dir");
		expect(env.SOME_OTHER_VAR).toBe("kept");
		expect(env.AWS_SECRET_ACCESS_KEY).toBeUndefined();
		expect(env.PI_TEST_RUNTIME).toBe("1");
		expect(env.BUN_JSC_useConcurrentGC).toBe("0");
		expect(env.BUN_JSC_numberOfGCMarkers).toBe("1");
	});

	test("withTempAgentDir cleans up allocated directory after resolved callback", async () => {
		let allocatedDir = "";
		const result = await withTempAgentDir(async (agentDir, env) => {
			allocatedDir = agentDir;
			expect(env.PI_CODING_AGENT_DIR).toBe(agentDir);
			const stat = await fs.stat(agentDir);
			expect(stat.isDirectory()).toBe(true);
			return "success-result";
		});

		expect(result).toBe("success-result");
		expect(allocatedDir).not.toBe("");
		// Allocated directory removed in finally
		expect(await fs.stat(allocatedDir).catch(() => null)).toBeNull();
	});

	test("withTempAgentDir cleans up allocated directory after rejected callback", async () => {
		let allocatedDir = "";
		let errorThrown = false;
		try {
			await withTempAgentDir(async (agentDir, env) => {
				allocatedDir = agentDir;
				expect(env.PI_CODING_AGENT_DIR).toBe(agentDir);
				const stat = await fs.stat(agentDir);
				expect(stat.isDirectory()).toBe(true);
				throw new Error("simulated chunk failure");
			});
		} catch (error) {
			errorThrown = true;
			expect(error instanceof Error && error.message).toBe("simulated chunk failure");
		}

		expect(errorThrown).toBe(true);
		expect(allocatedDir).not.toBe("");
		// Allocated directory removed in finally on failure
		expect(await fs.stat(allocatedDir).catch(() => null)).toBeNull();
	});
});

describe("host package.json gate check", () => {
	test("readHostPackageJson returns file content when present and null when absent", async () => {
		const tempHome = await fs.mkdtemp(path.join(os.tmpdir(), "omp-ci-test-home-"));
		try {
			expect(await readHostPackageJson(tempHome)).toBeNull();
			const pkgPath = path.join(tempHome, "package.json");
			await Bun.write(pkgPath, '{"name":"test"}\n');
			expect(await readHostPackageJson(tempHome)).toBe('{"name":"test"}\n');
		} finally {
			await fs.rm(tempHome, { recursive: true, force: true });
		}
	});

	test("assertHostPackageJsonUnchanged succeeds when content is unchanged", () => {
		expect(() => assertHostPackageJsonUnchanged(null, null, "/fake")).not.toThrow();
		expect(() => assertHostPackageJsonUnchanged('{"name":"a"}', '{"name":"a"}', "/fake")).not.toThrow();
	});

	test("assertHostPackageJsonUnchanged throws when host package.json was modified", () => {
		expect(() => assertHostPackageJsonUnchanged('{"name":"a"}', '{"name":"b"}', "/fake")).toThrow(
			/Host \/fake\/package\.json was modified during the test run/,
		);
		expect(() => assertHostPackageJsonUnchanged(null, '{"name":"new"}', "/fake")).toThrow(
			/Host \/fake\/package\.json was modified during the test run/,
		);
		expect(() => assertHostPackageJsonUnchanged('{"name":"old"}', null, "/fake")).toThrow(
			/Host \/fake\/package\.json was modified during the test run/,
		);
	});
});

describe("chunk timing summary and JSON output", () => {
	test("buildTimingRecords rounds seconds and derives ok status from exit codes", () => {
		const records = buildTimingRecords([
			{ label: "fast-passed", seconds: 1.234, exitCode: 0 },
			{ label: "slow-failed", seconds: 12.348, exitCode: 1 },
			{ label: "zero-sec", seconds: 0, exitCode: 0 },
			{ label: "timeout-killed", seconds: 60.101, exitCode: 137 },
		]);

		expect(records).toEqual([
			{ label: "timeout-killed", seconds: 60.1, ok: false },
			{ label: "slow-failed", seconds: 12.35, ok: false },
			{ label: "fast-passed", seconds: 1.23, ok: true },
			{ label: "zero-sec", seconds: 0, ok: true },
		]);
	});

	test("buildTimingRecords sorts slowest first with deterministic label tie-breaking", () => {
		const records = buildTimingRecords([
			{ label: "chunk-b", seconds: 5.5, ok: true },
			{ label: "chunk-a", seconds: 5.5, ok: true },
			{ label: "chunk-slowest", seconds: 10, ok: true },
			{ label: "chunk-fastest", seconds: 1, ok: true },
		]);

		expect(records.map(r => r.label)).toEqual(["chunk-slowest", "chunk-a", "chunk-b", "chunk-fastest"]);
	});

	test("formatTimingTable returns empty string when no records are given", () => {
		expect(formatTimingTable([])).toBe("");
	});

	test("formatTimingTable renders aligned table with headers, seconds, and pass/fail status", () => {
		const records = [
			{ label: "packages/natives", seconds: 5.2, ok: true },
			{ label: "packages/coding-agent (runtime)", seconds: 124.5, ok: true },
			{ label: "session-system/tests", seconds: 2.1, ok: false },
		];

		const table = formatTimingTable(records);
		const lines = table.trim().split("\n");

		// Header checks
		expect(lines[0]).toContain("━━━ Chunk Timing (slowest first) ━━━");
		expect(lines[1]).toBe("");
		expect(lines[2]).toMatch(/Chunk\s+Wall Time\s+Status/);
		expect(lines[3]).toMatch(/─+\s+─+\s+─+/);

		// Order check: slowest first
		expect(lines[4]).toContain("packages/coding-agent (runtime)");
		expect(lines[4]).toContain("124.50s");
		expect(lines[4]).toContain("pass");

		expect(lines[5]).toContain("packages/natives");
		expect(lines[5]).toContain("5.20s");
		expect(lines[5]).toContain("pass");

		expect(lines[6]).toContain("session-system/tests");
		expect(lines[6]).toContain("2.10s");
		expect(lines[6]).toContain("fail");
	});

	test("writeTimingJson writes schema [{label, seconds, ok}] sorted slowest first to target file", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-ci-timing-test-"));
		const jsonPath = path.join(tempDir, "nested", "dir", "timing.json");
		try {
			const records = [
				{ label: "chunk-b", seconds: 2.3, ok: true },
				{ label: "chunk-a", seconds: 45.67, ok: false },
			];
			await writeTimingJson(records, jsonPath);

			const content = await Bun.file(jsonPath).text();
			const parsed = JSON.parse(content);
			expect(parsed).toEqual([
				{ label: "chunk-a", seconds: 45.67, ok: false },
				{ label: "chunk-b", seconds: 2.3, ok: true },
			]);
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});

	test("shouldPrintTimingTable enables the timing table only for local and local-ts", () => {
		expect(shouldPrintTimingTable("local")).toBe(true);
		expect(shouldPrintTimingTable("local-ts")).toBe(true);
		expect(shouldPrintTimingTable("all")).toBe(false);
		expect(shouldPrintTimingTable("workspace")).toBe(false);
		expect(shouldPrintTimingTable("coding-agent-heavy")).toBe(false);
	});
});
