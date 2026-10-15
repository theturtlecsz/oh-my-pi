/**
 * Lifecycle test for scripts/session-system-test-preload.ts.
 *
 * The preload gives a `bun test` process a private temp root so prompt fixtures
 * never share `<os.tmpdir()>/omp-local`. This drives the real preload in bounded
 * child runs and asserts the observable contract a suite depends on:
 *
 * - each run records a writable root *under* the inherited temp dir, distinct
 *   from the inherited dir itself and from other runs,
 * - the root is removed when the run ends, for both a passing and a failing
 *   normal exit,
 * - the parent's own temp environment is untouched.
 *
 * `bun test` does not fire `process.on("exit")` on normal completion (verified
 * against Bun 1.4.2), so the preload removes the root from bun:test's `afterAll`
 * teardown; a failing test still ends the run normally and still cleans up.
 */

import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ptree } from "@oh-my-pi/pi-utils";

const repoRoot = path.join(import.meta.dir, "..");
const PRELOAD = path.join(repoRoot, "scripts/session-system-test-preload.ts");
const CHILD_TIMEOUT_MS = 60_000;

// Records its inherited temp dir and proves it is writable, then passes unless
// OMP_SSPRE_FAIL is set. The top-level write runs at module load, after the
// preload has installed the private root, so the recorded path survives a
// failing test.
const FIXTURE = `import { expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const tmp = os.tmpdir();
const probe = path.join(tmp, "writable-probe.txt");
fs.writeFileSync(probe, "probe");
fs.writeFileSync(process.env.OMP_SSPRE_OUT as string, tmp);

test("child probe", () => {
	expect(fs.readFileSync(probe, "utf8")).toBe("probe");
	expect(process.env.OMP_SSPRE_FAIL).toBeUndefined();
});
`;

interface RunOutcome {
	exitCode: number;
	recordedRoot: string;
}

/**
 * Run the preload (passed explicitly, never relied on from bunfig) over a
 * fixture child whose cwd is the harness dir outside the repo, so the root
 * bunfig preload cannot load a second time. The child inherits `base` as its
 * temp dir.
 */
async function runChildWithPreload(base: string, outPath: string, fail: boolean): Promise<RunOutcome> {
	const env: Record<string, string | undefined> = {
		...Bun.env,
		TMPDIR: base,
		TMP: base,
		TEMP: base,
		OMP_SSPRE_OUT: outPath,
		...(fail ? { OMP_SSPRE_FAIL: "1" } : {}),
	};
	const result = await ptree.exec([process.execPath, "test", "--preload", PRELOAD, path.join(base, "probe.test.ts")], {
		cwd: base,
		env,
		timeout: CHILD_TIMEOUT_MS,
		detached: true,
		allowNonZero: true,
	});
	return { exitCode: result.exitCode ?? 1, recordedRoot: (await fs.readFile(outPath, "utf8")).trim() };
}

describe.skipIf(process.platform === "win32")("session-system test preload temp-root lifecycle", () => {
	test("each run gets a distinct writable root under the inherited temp dir, removed on passing and failing exits", async () => {
		const harness = await fs.mkdtemp(path.join(os.tmpdir(), "omp-sspre-"));
		const parentTmpBefore = os.tmpdir();
		const parentEnvBefore = { ...Bun.env };
		try {
			await Bun.write(path.join(harness, "probe.test.ts"), FIXTURE);

			const first = await runChildWithPreload(harness, path.join(harness, "out-1.txt"), false);
			expect(first.exitCode).toBe(0);
			// The recorded root sits directly beneath the inherited temp dir, not
			// on it, so nothing else in the run shares `<tmp>/omp-local`.
			expect(path.dirname(first.recordedRoot)).toBe(harness);
			expect(first.recordedRoot).not.toBe(harness);
			// Removed when the passing run ends.
			expect(await fs.stat(first.recordedRoot).catch(() => null)).toBeNull();

			const second = await runChildWithPreload(harness, path.join(harness, "out-2.txt"), true);
			expect(second.exitCode).not.toBe(0);
			expect(path.dirname(second.recordedRoot)).toBe(harness);
			// A distinct root per run.
			expect(second.recordedRoot).not.toBe(first.recordedRoot);
			// Removed when the failing run ends too.
			expect(await fs.stat(second.recordedRoot).catch(() => null)).toBeNull();

			// Neither run left anything behind in the inherited temp dir.
			expect((await fs.readdir(harness)).sort()).toEqual(["out-1.txt", "out-2.txt", "probe.test.ts"]);
		} finally {
			await fs.rm(harness, { recursive: true, force: true });
		}

		// The parent's temp environment is not a side effect of child runs.
		expect(os.tmpdir()).toBe(parentTmpBefore);
		expect(Bun.env.TMPDIR).toBe(parentEnvBefore.TMPDIR);
		expect(Bun.env.TMP).toBe(parentEnvBefore.TMP);
		expect(Bun.env.TEMP).toBe(parentEnvBefore.TEMP);
	}, 180_000);
});
