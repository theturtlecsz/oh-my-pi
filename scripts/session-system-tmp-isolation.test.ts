/**
 * Acceptance test for OMP-637 temp-root isolation of the session-system suite.
 *
 * The session-system tests that drive an AgentSession fall back to the shared
 * `<os.tmpdir()>/omp-local` root for `local://` scratch space. When the
 * inherited temp dir already holds a mode-000 `omp-local` (a hostile TMPDIR, or
 * a leftover from an earlier run), those tests fail on EACCES instead of
 * exercising their own fixtures. scripts/session-system-test-preload.ts —
 * loaded by the root bunfig.toml `[test].preload` — is the fix: it points
 * TMPDIR/TMP/TEMP at a fresh mkdtemp root for the process lifetime, so no child
 * ever reaches the hostile root.
 *
 * This drives the real acceptance command from the repo root with the root
 * bunfig's automatic preload discovery (no explicit --preload) under exactly
 * that hostile environment: a unique mkdtemp parent with an empty HOME and a
 * mode-000 `parent/omp-local`. The observable contract:
 *
 * - a child-path write into `parent/omp-local` must fail EACCES first, so the
 *   test fails loudly instead of passing vacuously on a root user,
 * - `bun test session-system/tests --parallel=1 --timeout=30000` exits 0 and the
 *   three AgentSession prompt suites each run at least their original tests,
 *   with zero failures and zero skips,
 * - `parent/omp-local` is still mode 000 when the run ends (nothing escaped the
 *   preload's private root), is empty once permissions are restored, and the
 *   parent holds no leftover preload root.
 *
 * Results are read from bun's JUnit reporter, written to its own file. The
 * suite spawns grandchildren that inherit the child's stdout, so their writes
 * interleave with the reporter's and even truncate header lines; the JUnit file
 * is the only interleaving-proof record of which tests ran and how they ended.
 *
 * The parent is removed in `finally` on every outcome, after permissions are
 * restored so a mode-000 directory cannot block the recursive delete.
 */

import { describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ptree } from "@oh-my-pi/pi-utils";

const repoRoot = path.join(import.meta.dir, "..");
// The whole session-system suite ran in ~110s on a quiet runner; leave headroom
// for a loaded CI lane.
const CHILD_TIMEOUT_MS = 240_000;
// scripts/session-system-test-preload.ts allocates its per-process root with
// this prefix under the inherited temp dir.
const PRELOAD_ROOT_PREFIX = "omp-session-test-";

// The AgentSession prompt suites the isolation fix protects, with the number of
// tests each must still execute. Floors, not equalities: a file that ran fewer
// tests than its original set means assertions were dropped or skipped.
const TARGET_FILES: Array<{ file: string; minTests: number }> = [
	{ file: "session-system/tests/agent-stop-prompt-refusal.test.ts", minTests: 3 },
	{ file: "session-system/tests/auditor-runner.test.ts", minTests: 80 },
	{ file: "session-system/tests/nsi6-model-runner.test.ts", minTests: 9 },
];

interface RunResult {
	exitCode: number;
	output: string;
}

interface SuiteResult {
	tests: number;
	failures: number;
	skipped: number;
}

/**
 * Run the acceptance command from the repo root, letting bun discover the root
 * bunfig preload. `env` carries the hostile TMPDIR directly; no wrapper replaces
 * it or injects an isolated agent dir. The JUnit reporter is additive — it does
 * not change which tests run or how they are isolated.
 */
async function runSessionSystemTests(env: Record<string, string | undefined>, junitPath: string): Promise<RunResult> {
	try {
		const result = await ptree.exec(
			[
				process.execPath,
				"test",
				"session-system/tests",
				"--parallel=1",
				"--timeout=30000",
				"--reporter=junit",
				`--reporter-outfile=${junitPath}`,
			],
			{
				cwd: repoRoot,
				env,
				timeout: CHILD_TIMEOUT_MS,
				detached: true,
				allowNonZero: true,
			},
		);
		return { exitCode: result.exitCode ?? 1, output: `${result.stdout}\n${result.stderr}` };
	} catch (err) {
		throw new Error(`session-system child failed or timed out: ${String(err)}`, { cause: err });
	}
}

function attribute(tag: string, name: string): string | undefined {
	return new RegExp(`\\b${name}="([^"]*)"`).exec(tag)?.[1];
}

/** Totals for one file from its outer `<testsuite name="<path>" file="<path>">` element, or null. */
function suiteFor(junit: string, file: string): SuiteResult | null {
	for (const match of junit.matchAll(/<testsuite\b[^>]*>/g)) {
		const tag = match[0];
		if (attribute(tag, "name") !== file || attribute(tag, "file") !== file) continue;
		const number = (name: string): number => Number(attribute(tag, name) ?? Number.NaN);
		return { tests: number("tests"), failures: number("failures"), skipped: number("skipped") };
	}
	return null;
}

describe.skipIf(process.platform === "win32")(
	"session-system tests run on a private temp root under a hostile TMPDIR",
	() => {
		it("passes the full session-system suite with a mode-000 parent/omp-local, leaving it untouched and the parent clean", async () => {
			const parent = await fs.mkdtemp(path.join(os.tmpdir(), "ss-tmp-isolation-"));
			const home = path.join(parent, "home");
			const ompLocal = path.join(parent, "omp-local");
			const junitPath = path.join(parent, "junit.xml");
			try {
				await fs.mkdir(home);
				await fs.mkdir(ompLocal);
				await fs.chmod(ompLocal, 0o000);

				// Prerequisite, not a defect path: prove the hostile root is really
				// unwritable. A root user would make every assertion below vacuous, so
				// fail loudly instead of running the child against a writable root.
				let writable = false;
				try {
					await fs.mkdir(path.join(ompLocal, "probe"));
					writable = true;
				} catch (err) {
					if ((err as NodeJS.ErrnoException).code !== "EACCES") throw err;
				}
				if (writable) {
					throw new Error(
						`Prerequisite failed: mode-000 ${ompLocal} is writable (running as root?). Run as non-root.`,
					);
				}

				const env: Record<string, string | undefined> = {
					...Bun.env,
					TMPDIR: parent,
					TMP: parent,
					TEMP: parent,
					HOME: home,
				};
				const result = await runSessionSystemTests(env, junitPath);
				if (result.exitCode !== 0) {
					throw new Error(`session-system suite failed (exit ${result.exitCode}):\n${result.output}`);
				}

				let junit: string;
				try {
					junit = await fs.readFile(junitPath, "utf8");
				} catch (err) {
					throw new Error(`No JUnit report at ${junitPath} (exit ${result.exitCode}):\n${result.output}`, {
						cause: err,
					});
				}

				const totals = /<testsuites\b[^>]*>/.exec(junit)?.[0];
				expect(totals && Number(attribute(totals, "failures"))).toBe(0);

				for (const { file, minTests } of TARGET_FILES) {
					const suite = suiteFor(junit, file);
					if (suite === null) {
						throw new Error(`${file} did not run:\n${result.output}`);
					}
					expect({ file, failures: suite.failures }).toEqual({ file, failures: 0 });
					expect({ file, skipped: suite.skipped }).toEqual({ file, skipped: 0 });
					expect(suite.tests).toBeGreaterThanOrEqual(minTests);
				}

				// The child never reached the hostile root: it is still mode 000 and
				// therefore had nothing written into it.
				expect((await fs.stat(ompLocal)).mode & 0o777).toBe(0);

				await fs.chmod(ompLocal, 0o700);
				expect(await fs.readdir(ompLocal)).toEqual([]);
				const leftoverRoots = (await fs.readdir(parent)).filter(name => name.startsWith(PRELOAD_ROOT_PREFIX));
				expect(leftoverRoots).toEqual([]);
			} finally {
				await fs.chmod(ompLocal, 0o700).catch(() => {});
				await fs.rm(parent, { recursive: true, force: true });
			}
		}, 300_000);
	},
);
