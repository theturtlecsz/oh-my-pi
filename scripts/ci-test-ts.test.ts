import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { buildChildEnv, describeChunkFailure } from "./ci-test-ts.ts";

// The two ways a chunk reaches SIGKILL are indistinguishable by exit code, so
// these drive real subprocesses to produce a genuine 137 rather than asserting
// against a hand-written constant.
async function spawnExitCode(script: string): Promise<number> {
	const proc = Bun.spawn(["sh", "-c", script], { stdout: "ignore", stderr: "ignore" });
	return await proc.exited;
}

// Re-hosts the sequential runner's failure tail: spawn, watchdog, attribute.
// `runTestCommand` itself is not injectable (it builds argv from the repo
// layout), so the decision under test is driven directly.
async function runWithWatchdog(script: string, timeoutMs: number): Promise<string> {
	const proc = Bun.spawn(["sh", "-c", script], { stdout: "ignore", stderr: "ignore" });
	let timedOut = false;
	const killTimer = setTimeout(() => {
		timedOut = true;
		proc.kill("SIGKILL");
	}, timeoutMs);
	const exitCode = await proc.exited;
	clearTimeout(killTimer);
	return describeChunkFailure(exitCode, timedOut);
}

describe("describeChunkFailure", () => {
	test("a real SIGKILL that the watchdog did not cause is attributed to the OOM killer", async () => {
		const exitCode = await spawnExitCode("kill -9 $$");
		expect(exitCode).toBe(137);

		const message = describeChunkFailure(exitCode, false);
		expect(message).toContain("OOM killer");
		expect(message).toContain("chunkSize");
		// The old wording carried no cause at all; it must not come back.
		expect(message).not.toBe("failed with exit code 137");
	});

	test("a watchdog kill is attributed to the watchdog, not to memory", async () => {
		const message = await runWithWatchdog("sleep 30", 150);
		expect(message).toContain("chunk watchdog");
		expect(message).toContain("OMP_TEST_CHUNK_TIMEOUT");
		expect(message).not.toContain("OOM killer");
	});

	test("the two SIGKILL causes produce different messages from the same exit code", async () => {
		const oomKilled = describeChunkFailure(137, false);
		const watchdogKilled = describeChunkFailure(137, true);
		expect(oomKilled).not.toBe(watchdogKilled);
	});

	test("an ordinary test failure keeps the plain wording", async () => {
		const exitCode = await spawnExitCode("exit 1");
		expect(exitCode).toBe(1);
		expect(describeChunkFailure(exitCode, false)).toBe("failed with exit code 1");
	});

	test("a bun crash exit keeps the plain wording so the retry log still reads naturally", () => {
		expect(describeChunkFailure(134, false)).toBe("failed with exit code 134");
		expect(describeChunkFailure(139, false)).toBe("failed with exit code 139");
	});

	test("the watchdog message reports the configured timeout", () => {
		const previous = Bun.env.OMP_TEST_CHUNK_TIMEOUT;
		Bun.env.OMP_TEST_CHUNK_TIMEOUT = "42";
		try {
			expect(describeChunkFailure(137, true)).toContain("42s");
		} finally {
			if (previous === undefined) delete Bun.env.OMP_TEST_CHUNK_TIMEOUT;
			else Bun.env.OMP_TEST_CHUNK_TIMEOUT = previous;
		}
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

	test("cleans up temporary agent directory in try-finally lifecycle on success and failure", async () => {
		const runInTempAgentDir = async (shouldFail: boolean) => {
			const agentDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-ci-agent-test-"));
			let capturedDir = agentDir;
			try {
				const env = buildChildEnv({ agentDir });
				expect(env.PI_CODING_AGENT_DIR).toBe(agentDir);
				const statBefore = await fs.stat(agentDir);
				expect(statBefore.isDirectory()).toBe(true);
				if (shouldFail) {
					throw new Error("simulated test chunk failure");
				}
			} finally {
				await fs.rm(agentDir, { recursive: true, force: true }).catch(() => {});
			}
			return capturedDir;
		};

		const successDir = await runInTempAgentDir(false);
		expect(await fs.stat(successDir).catch(() => null)).toBeNull();

		let errorThrown = false;
		let failureDir = "";
		try {
			failureDir = await runInTempAgentDir(true);
		} catch {
			errorThrown = true;
		}
		expect(errorThrown).toBe(true);
		expect(await fs.stat(failureDir).catch(() => null)).toBeNull();
	});
});
