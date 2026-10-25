import { afterEach, beforeEach, describe, expect, it, spyOn } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { buildChildEnv, withTempAgentDir } from "./ci-test-ts.ts";

let fixture: string;

beforeEach(async () => {
	fixture = await fs.mkdtemp(path.join(os.tmpdir(), "omp-fx-"));
});

afterEach(async () => {
	await fs.rm(fixture, { recursive: true, force: true });
});

async function runChildProbe(
	env: Record<string, string | undefined>,
	probeFileName: string,
	probeContent: string,
): Promise<{ exitCode: number; childTmpdir: string; probeFile: string }> {
	const script = `
const os = require("node:os");
const fs = require("node:fs");
const path = require("node:path");
const tmp = os.tmpdir();
const probe = path.join(tmp, ${JSON.stringify(probeFileName)});
fs.writeFileSync(probe, ${JSON.stringify(probeContent)}, "utf8");
console.log(tmp);
`;
	const proc = Bun.spawn([process.execPath, "-e", script], {
		env: env as Record<string, string>,
		stdout: "pipe",
		stderr: "pipe",
	});
	const stdout = (await new Response(proc.stdout).text()).trim();
	const exitCode = await proc.exited;
	const probeFile = path.join(env.TMPDIR as string, probeFileName);
	return { exitCode, childTmpdir: stdout, probeFile };
}

describe("buildChildEnv", () => {
	it("forces TMPDIR === TMP === TEMP === tmpDir over baseEnv, preserves PI_CODING_AGENT_DIR, scrubs credentials, leaves inputs unmodified", () => {
		const baseEnv = {
			TMPDIR: "/base/tmpdir",
			TMP: "/base/tmp",
			TEMP: "/base/temp",
			PI_CODING_AGENT_DIR: "/base/agent",
			AWS_SECRET_ACCESS_KEY: "secret",
			GITHUB_TOKEN: "token",
			CUSTOM_VAR: "custom",
		};
		const baseEnvSnapshot = { ...baseEnv };
		const env = buildChildEnv({
			agentDir: "/isolated/agent",
			baseEnv,
			tmpDir: "/isolated/tmp",
		});

		expect(env.TMPDIR).toBe("/isolated/tmp");
		expect(env.TMP).toBe("/isolated/tmp");
		expect(env.TEMP).toBe("/isolated/tmp");
		expect(env.PI_CODING_AGENT_DIR).toBe("/isolated/agent");
		expect(env.AWS_SECRET_ACCESS_KEY).toBeUndefined();
		expect(env.GITHUB_TOKEN).toBeUndefined();
		expect(env.CUSTOM_VAR).toBe("custom");
		expect(env.GITHUB_ACTIONS).toBe("");
		expect(env.PI_TEST_RUNTIME).toBe("1");
		expect(env.BUN_JSC_useConcurrentGC).toBe("0");
		expect(env.BUN_JSC_numberOfGCMarkers).toBe("1");

		expect(baseEnv).toEqual(baseEnvSnapshot);
	});

	it("keeps inherited temp vars when tmpDir is omitted", () => {
		const baseEnv = {
			TMPDIR: "/base/tmpdir",
			TMP: "/base/tmp",
			TEMP: "/base/temp",
		};
		const env = buildChildEnv({ baseEnv });
		expect(env.TMPDIR).toBe("/base/tmpdir");
		expect(env.TMP).toBe("/base/tmp");
		expect(env.TEMP).toBe("/base/temp");
	});

	it("does not mutate Bun.env", () => {
		const sentinels = {
			TMPDIR: "sentinel-tmpdir",
			TMP: "sentinel-tmp",
			TEMP: "sentinel-temp",
			PI_CODING_AGENT_DIR: "sentinel-agent-dir",
			PI_TEST_RUNTIME: "sentinel-runtime",
			BUN_JSC_useConcurrentGC: "sentinel-concurrent-gc",
			BUN_JSC_numberOfGCMarkers: "sentinel-gc-markers",
			GITHUB_ACTIONS: "sentinel-actions",
			AWS_SECRET_ACCESS_KEY: "sentinel-aws-secret",
			GITHUB_TOKEN: "sentinel-github-token",
		};
		const originals = new Map(Object.keys(sentinels).map(key => [key, Bun.env[key]]));
		try {
			for (const [key, value] of Object.entries(sentinels)) {
				Bun.env[key] = value;
			}
			const before = { ...Bun.env };
			buildChildEnv({ agentDir: "/dir", tmpDir: "/tmp" });
			expect({ ...Bun.env }).toEqual(before);
			expect(Bun.env.TMPDIR).toBe(sentinels.TMPDIR);
			expect(Bun.env.TMP).toBe(sentinels.TMP);
			expect(Bun.env.TEMP).toBe(sentinels.TEMP);
			expect(Bun.env.PI_CODING_AGENT_DIR).toBe(sentinels.PI_CODING_AGENT_DIR);
			expect(Bun.env.PI_TEST_RUNTIME).toBe(sentinels.PI_TEST_RUNTIME);
			expect(Bun.env.BUN_JSC_useConcurrentGC).toBe(sentinels.BUN_JSC_useConcurrentGC);
			expect(Bun.env.BUN_JSC_numberOfGCMarkers).toBe(sentinels.BUN_JSC_numberOfGCMarkers);
			expect(Bun.env.GITHUB_ACTIONS).toBe(sentinels.GITHUB_ACTIONS);
			expect(Bun.env.AWS_SECRET_ACCESS_KEY).toBe(sentinels.AWS_SECRET_ACCESS_KEY);
			expect(Bun.env.GITHUB_TOKEN).toBe(sentinels.GITHUB_TOKEN);
		} finally {
			for (const [key, value] of originals) {
				if (value === undefined) {
					delete Bun.env[key];
				} else {
					Bun.env[key] = value;
				}
			}
		}
	});
});

describe("withTempAgentDir private tmp root", () => {
	it("gives concurrent callbacks distinct writable roots; child Bun spawned with env reports os.tmpdir() === env.TMPDIR and writes a probe file that parent reads back; fixture empty afterwards", async () => {
		const seenRoots = await Promise.all(
			[0, 1].map(i =>
				withTempAgentDir(
					async (agentDir, env) => {
						const tmp = env.TMPDIR as string;
						expect(path.dirname(tmp)).toBe(fixture);
						expect(agentDir.startsWith(`${tmp}${path.sep}`)).toBe(true);

						const probeFileName = `probe-${i}.txt`;
						const probeContent = `child-probe-content-${i}`;
						const { exitCode, childTmpdir, probeFile } = await runChildProbe(env, probeFileName, probeContent);

						expect(exitCode).toBe(0);
						expect(childTmpdir).toBe(env.TMPDIR);
						const readBack = await Bun.file(probeFile).text();
						expect(readBack).toBe(probeContent);

						return tmp;
					},
					{ TMPDIR: fixture },
				),
			),
		);

		expect(new Set(seenRoots).size).toBe(2);
		expect(await fs.readdir(fixture)).toEqual([]);
	});

	it("removes roots after successful callback", async () => {
		let allocatedTmpDir = "";
		let allocatedAgentDir = "";
		const result = await withTempAgentDir(
			async (agentDir, env) => {
				allocatedTmpDir = env.TMPDIR as string;
				allocatedAgentDir = agentDir;
				expect((await fs.stat(allocatedTmpDir)).isDirectory()).toBe(true);
				expect((await fs.stat(allocatedAgentDir)).isDirectory()).toBe(true);
				return "ok";
			},
			{ TMPDIR: fixture },
		);

		expect(result).toBe("ok");
		expect(allocatedTmpDir).not.toBe("");
		expect(await fs.stat(allocatedTmpDir).catch(() => null)).toBeNull();
		expect(await fs.stat(allocatedAgentDir).catch(() => null)).toBeNull();
		expect(await fs.readdir(fixture)).toEqual([]);
	});

	it("removes roots and rethrows original error on callback rejection", async () => {
		const boom = new Error("callback failure");
		let allocatedTmpDir = "";
		let allocatedAgentDir = "";
		await expect(
			withTempAgentDir(
				async (agentDir, env) => {
					allocatedTmpDir = env.TMPDIR as string;
					allocatedAgentDir = agentDir;
					expect((await fs.stat(allocatedTmpDir)).isDirectory()).toBe(true);
					expect((await fs.stat(allocatedAgentDir)).isDirectory()).toBe(true);
					throw boom;
				},
				{ TMPDIR: fixture },
			),
		).rejects.toBe(boom);

		expect(allocatedTmpDir).not.toBe("");
		expect(await fs.stat(allocatedTmpDir).catch(() => null)).toBeNull();
		expect(await fs.stat(allocatedAgentDir).catch(() => null)).toBeNull();
		expect(await fs.readdir(fixture)).toEqual([]);
	});

	it("removes the root when agentDir allocation rejects", async () => {
		const realMkdtemp = fs.mkdtemp;
		let createdTmpDir = "";
		const spy = spyOn(fs, "mkdtemp").mockImplementation(async (prefix: string) => {
			if (path.basename(prefix).startsWith("a-")) {
				createdTmpDir = path.dirname(prefix);
				throw new Error("agentDir alloc failed");
			}
			return realMkdtemp(prefix);
		});
		try {
			let called = false;
			await expect(
				withTempAgentDir(
					async () => {
						called = true;
					},
					{ TMPDIR: fixture },
				),
			).rejects.toThrow("agentDir alloc failed");

			expect(called).toBe(false);
			expect(createdTmpDir).not.toBe("");
			expect(await fs.stat(createdTmpDir).catch(() => null)).toBeNull();
			expect(await fs.readdir(fixture)).toEqual([]);
		} finally {
			spy.mockRestore();
		}
	});
});
