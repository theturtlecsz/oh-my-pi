import { describe, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ptree } from "@oh-my-pi/pi-utils";
import { withTempAgentDir } from "./ci-test-ts.ts";

const repoRoot = path.join(import.meta.dir, "..");
const CHILD_TIMEOUT_MS = 60_000;

// Old fixed-name roots under os.tmpdir() that earlier tests created directly.
const BLOCKED_NAMES = [
	"pi-utils-python-gateway",
	"pi-utils-profiles",
	"pi-utils-editorconfig-formatting",
	"test-config-overlay",
	"test-config-overlay-missing",
	"test-config-overlay-bad",
	"test-reload-cwd",
	"pi-coding-agent-format-options",
	"omp-local",
];

const UTILS_FILES = ["test/dirs-python-gateway.test.ts", "test/profiles.test.ts", "test/spacing.test.ts"];
const CODING_AGENT_S04_FILES = ["test/settings-reload-cwd.test.ts", "test/lsp-format-options.test.ts"];
const SETUP_BAIL_FILE = "test/agent-session-plan-reference-setup-bail.test.ts";
const ISOLATED_FILES = [
	"test/image-paste-source-path.test.ts",
	"test/agent-session-skill-image.test.ts",
	"test/agent-session-plan-mode-convergence.test.ts",
	"test/agent-session-plan-reference-compaction.test.ts",
	SETUP_BAIL_FILE,
];

interface RunResult {
	exitCode: number;
	output: string;
}

async function runBunTest(
	pkg: "utils" | "coding-agent",
	files: string[],
	env: Record<string, string | undefined>,
): Promise<RunResult> {
	try {
		const result = await ptree.exec([process.execPath, "test", ...files], {
			cwd: path.join(repoRoot, "packages", pkg),
			env,
			timeout: CHILD_TIMEOUT_MS,
			detached: true,
			allowNonZero: true,
		});
		return { exitCode: result.exitCode ?? 1, output: `${result.stdout}\n${result.stderr}` };
	} catch (err) {
		throw new Error(`Child failed or timed out (${pkg}: ${files.join(", ")}): ${String(err)}`, { cause: err });
	}
}

function expectPass(label: string, result: RunResult): void {
	if (result.exitCode !== 0) {
		throw new Error(`${label} failed (exit ${result.exitCode}):\n${result.output}`);
	}
}

async function withBlockedTmp<T>(
	fn: (root: string, env: Record<string, string | undefined>) => Promise<T>,
): Promise<T> {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "tti-"));
	try {
		await fs.chmod(root, 0o700);
		for (const name of BLOCKED_NAMES) {
			const dir = path.join(root, name);
			await fs.mkdir(dir);
			await fs.chmod(dir, 0o000);
		}
		try {
			await fs.mkdir(path.join(root, "omp-local", "probe"));
		} catch (err) {
			if ((err as NodeJS.ErrnoException).code !== "EACCES") throw err;
			const env = { ...Bun.env, TMPDIR: root, TMP: root, TEMP: root };
			return await fn(root, env);
		}
		throw new Error("Prerequisite failed: chmod 000 directories are writable (running as root?). Run as non-root.");
	} finally {
		for (const name of BLOCKED_NAMES) {
			await fs.chmod(path.join(root, name), 0o700).catch(() => {});
		}
		await fs.rm(root, { recursive: true, force: true });
	}
}

describe.skipIf(process.platform === "win32")("test temp-root isolation under unwritable fixed-name dirs", () => {
	it("passes hardened suites with unwritable legacy roots, and the setup-bail control still hits EACCES", async () => {
		await withBlockedTmp(async (root, env) => {
			expectPass("utils files", await runBunTest("utils", UTILS_FILES, env));
			expectPass("coding-agent s04 files", await runBunTest("coding-agent", CODING_AGENT_S04_FILES, env));

			await withTempAgentDir(async (_agentDir, isolatedEnv) => {
				expectPass("coding-agent isolated files", await runBunTest("coding-agent", ISOLATED_FILES, isolatedEnv));
			}, env);

			const control = await runBunTest("coding-agent", [SETUP_BAIL_FILE], env);
			const blocked = path.join(root, "omp-local");
			const permissionFailure = control.output
				.split("\n")
				.some(line => line.includes(blocked) && /EACCES|permission denied/i.test(line));
			if (control.exitCode === 0 || !permissionFailure) {
				throw new Error(`Expected setup-bail EACCES at ${blocked} (exit ${control.exitCode}):\n${control.output}`);
			}
		});
		// root is removed on every outcome
	}, 300_000);
});
