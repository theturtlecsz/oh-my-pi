import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { FULL_TARGETS } from "../../scripts/installed-qualification-mode";
import {
	countJunitTests,
	type CommandOptions,
	type CommandResult,
	type QualifyInstalledOptions,
	type QualificationTools,
	runQualifyInstalled,
} from "../qualify-installed";

const tempRoots: string[] = [];

afterEach(async () => {
	for (const root of tempRoots.splice(0)) await fs.rm(root, { recursive: true, force: true });
});

const MANIFEST_JSON = '{"schemaVersion":1,"status":"STAGED"}\n';
const TOOLS: QualificationTools = {
	bunPath: "/opt/host/bun",
	bunVersion: "1.3.14",
	uvPath: "/opt/host/uv",
	pythonPath: "/opt/host/python3.13",
};

interface RecordedCall {
	command: readonly string[];
	options: CommandOptions;
}

interface RunFixture {
	root: string;
	releaseRoot: string;
	calls: RecordedCall[];
	manifestDigest: string;
	options: QualifyInstalledOptions;
}

function argValue(command: readonly string[], flag: string): string | undefined {
	const index = command.indexOf(flag);
	return index >= 0 ? command[index + 1] : undefined;
}

function prefixedValue(command: readonly string[], prefix: string): string | undefined {
	const match = command.find(argument => argument.startsWith(prefix));
	return match?.slice(prefix.length);
}

function junitXml(cases: { skipped?: boolean }[]): string {
	const body = cases
		.map((testcase, index) =>
			testcase.skipped ? `<testcase name="case-${index}"><skipped/></testcase>` : `<testcase name="case-${index}"/>`,
		)
		.join("");
	return `<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">${body}</testsuite></testsuites>`;
}

/**
 * Build a fixed repository root and an injectable runner. The stage fake writes
 * a manifest to the destination it is handed; the pytest fake writes the JUnit
 * report each scenario specifies.
 */
async function fixture(config: {
	stageExit?: number;
	stageStderr?: string;
	pytestExit?: number;
	pytestStderr?: string;
	junit?: string;
	stageThrows?: string;
	pytestThrows?: string;
}): Promise<RunFixture> {
	const root = await fs.mkdtemp(path.join(import.meta.dir, "qualify-root-"));
	tempRoots.push(root);
	const digest = new Bun.CryptoHasher("sha256").update(MANIFEST_JSON).digest("hex");
	const calls: RecordedCall[] = [];
	const runner = async (command: readonly string[], options: CommandOptions): Promise<CommandResult> => {
		calls.push({ command, options });
		if (command.some(argument => argument.endsWith("stage.ts"))) {
			if (config.stageThrows) throw new Error(config.stageThrows);
			const destination = argValue(command, "--destination");
			if (destination) {
				await fs.mkdir(destination, { recursive: true });
				await Bun.write(path.join(destination, "manifest.json"), MANIFEST_JSON);
			}
			return { exitCode: config.stageExit ?? 0, stdout: "", stderr: config.stageStderr ?? "" };
		}
		if (config.pytestThrows) throw new Error(config.pytestThrows);
		const junitPath = prefixedValue(command, "--junitxml=");
		if (junitPath && config.junit !== undefined) await Bun.write(junitPath, config.junit);
		return { exitCode: config.pytestExit ?? 0, stdout: "", stderr: config.pytestStderr ?? "" };
	};
	const options: QualifyInstalledOptions = { root, runner, tools: TOOLS };
	return { root, releaseRoot: "", calls, manifestDigest: digest, options };
}

async function run(config: Parameters<typeof fixture>[0]) {
	const built = await fixture(config);
	const outcome = await runQualifyInstalled(built.options);
	tempRoots.push(outcome.tempRoot);
	built.releaseRoot = outcome.releaseRoot;
	return { ...built, outcome };
}

describe("qualify-installed staging", () => {
	test("stage failure is fatal and pytest is never invoked", async () => {
		const { calls, outcome } = await run({ stageExit: 7, stageStderr: "dirty checkout" });

		expect(outcome.ok).toBe(false);
		expect(outcome.reason).toContain("stage failed with exit 7");
		expect(outcome.manifestSha256).toBeNull();
		expect(calls).toHaveLength(1);
		expect(calls.some(call => call.command.includes("pytest"))).toBe(false);
	});

	test("stage command mirrors the CI invocation with both native addons", async () => {
		const { root, releaseRoot, calls } = await run({ junit: junitXml([{}]) });

		expect(calls[0].command).toEqual([
			TOOLS.bunPath,
			"session-system/runtime/stage.ts",
			"--source",
			root,
			"--destination",
			releaseRoot,
			"--bun",
			TOOLS.bunPath,
			"--bun-version",
			TOOLS.bunVersion,
			"--uv",
			TOOLS.uvPath,
			"--python",
			TOOLS.pythonPath,
			"--native",
			path.join(root, "packages/natives/native/pi_natives.linux-x64-baseline.node"),
			"--native",
			path.join(root, "packages/natives/native/pi_natives.linux-x64-modern.node"),
		]);
		expect(calls[0].options.cwd).toBe(root);
	});

	test("a stage launch failure is fatal, preserves its evidence path, and never runs pytest", async () => {
		const { calls, outcome } = await run({ stageThrows: "spawn failed: EACCES" });

		expect(outcome.ok).toBe(false);
		expect(outcome.reason).toContain("stage failed: spawn failed: EACCES");
		expect((await fs.stat(outcome.tempRoot)).isDirectory()).toBe(true);
		expect(calls).toHaveLength(1);
		expect(calls.some(call => call.command.includes("pytest"))).toBe(false);
	});
});

describe("qualify-installed pytest contract", () => {
	test("pytest runs exactly FULL_TARGETS with the release and manifest digest in its environment", async () => {
		const { releaseRoot, calls, manifestDigest, outcome } = await run({ junit: junitXml([{}, {}]) });

		const pytest = calls.find(call => call.command.includes("pytest"));
		expect(pytest).toBeDefined();
		for (const target of FULL_TARGETS) expect(pytest?.command).toContain(target);
		expect(pytest?.command).toContain("-n");
		expect(pytest?.command.some(argument => argument.startsWith("--basetemp="))).toBe(true);
		expect(pytest?.command.some(argument => argument.startsWith("--junitxml="))).toBe(true);

		expect(pytest?.options.env.OMP_INSTALLED_RELEASE).toBe(releaseRoot);
		expect(pytest?.options.env.OMP_INSTALLED_MANIFEST_SHA256).toBe(manifestDigest);
		expect(outcome.manifestSha256).toBe(manifestDigest);
		expect(outcome.tests).toBe(2);
	});
});

describe("qualify-installed qualification verdicts", () => {
	test("pytest failure preserves evidence and reports a failure", async () => {
		const { outcome } = await run({ pytestExit: 3, pytestStderr: "isolation breached" });
		expect(outcome.ok).toBe(false);
		expect(outcome.reason).toContain("pytest failed with exit 3");
		expect(outcome.manifestSha256).not.toBeNull();
		expect((await fs.stat(outcome.tempRoot)).isDirectory()).toBe(true);
	});

	test("a pytest launch failure preserves the staged release and its evidence path", async () => {
		const { outcome } = await run({ pytestThrows: "spawn failed: EACCES" });

		expect(outcome.ok).toBe(false);
		expect(outcome.reason).toContain("pytest failed with exit 1");
		expect(outcome.manifestSha256).not.toBeNull();
		expect((await fs.stat(outcome.tempRoot)).isDirectory()).toBe(true);
		expect((await fs.stat(outcome.releaseRoot)).isDirectory()).toBe(true);
	});

	test("a run that skips is refused even when pytest exits zero", async () => {
		const { outcome } = await run({ junit: junitXml([{}, { skipped: true }]) });
		expect(outcome.ok).toBe(false);
		expect(outcome.reason).toContain("1 skipped");
		expect(outcome.skipped).toBe(1);
	});

	test("a run that reports zero tests is refused", async () => {
		const { outcome } = await run({ junit: junitXml([]) });
		expect(outcome.ok).toBe(false);
		expect(outcome.reason).toContain("no tests");
		expect(outcome.tests).toBe(0);
	});

	test("verified run succeeds and removes its temporary root", async () => {
		const { outcome } = await run({ junit: junitXml([{}, {}, {}]) });
		expect(outcome.ok).toBe(true);
		expect(outcome.tests).toBe(3);
		expect(outcome.skipped).toBe(0);
		await expect(fs.stat(outcome.tempRoot)).rejects.toHaveProperty("code", "ENOENT");
	});
});

describe("countJunitTests", () => {
	test("counts self-closing testcases and nested skips", () => {
		expect(countJunitTests(junitXml([{}, { skipped: true }, {}]))).toEqual({ total: 3, skipped: 1 });
	});
});
