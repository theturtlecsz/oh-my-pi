/**
 * OMP-640: process lifecycle around Bun's `killed N dangling process` line.
 *
 * Bun enforces a per-test deadline by killing that test's live children and
 * printing `killed N dangling process` at the instant the deadline fires — it
 * does NOT run a pending `try/finally` in the test body first, and its
 * dangling-process check has already run by the time an awaited `afterEach`
 * reaps the child. A test that returns normally is checked only *after*
 * `afterEach`, so an awaited `afterEach` suppression works there. Registering
 * every child at spawn and reaping it in an awaited `afterEach` is therefore
 * the only cleanup hook that runs on both paths; a `finally` alone is not
 * enough (verified experimentally — see docs/omp-640-process-lifecycle.md).
 *
 * This file's own run must end with no `killed N dangling process` line. The
 * deliberately failing paths (a stalled test past a short deadline, and an
 * assertion failure while holding a live child) run in nested `bun test`
 * processes so a deliberate failure cannot fail this file; each nested runner
 * is a child of this process and is reaped here.
 */
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test } from "bun:test";
import { OwnedChildren } from "./fixtures/owned-children";

const SCENARIO = process.env.OMP_640_LIFECYCLE_SCENARIO;
const REPO_ROOT = path.resolve(import.meta.dir, "../..");

interface RunnerResult {
	exitCode: number;
	output: string;
}

/** stdout/stderr of Bun.spawnSync is a Buffer even with encoding: "utf8". */
function decode(value: string | Buffer | Uint8Array): string {
	return typeof value === "string" ? value : new TextDecoder().decode(value);
}

async function runNestedScenario(scenario: string): Promise<RunnerResult> {
	const proc = Bun.spawn([process.execPath, "test", "--timeout", "30000", import.meta.path], {
		cwd: REPO_ROOT,
		env: { ...process.env, OMP_640_LIFECYCLE_SCENARIO: scenario },
		stdout: "pipe",
		stderr: "pipe",
	});
	const [stdout, stderr, exitCode] = await Promise.all([
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
		proc.exited,
	]);
	return { exitCode, output: `${stdout}${stderr}` };
}

if (SCENARIO === undefined) {
	describe("owned-child lifecycle (OMP-640)", () => {
		const owned = new OwnedChildren();
		const tempDirs: string[] = [];
		// Child exit observed by the afterEach of the test that owned it, read by
		// the next test. Proves the child is gone before the next case runs.
		const reapedBeforeNext: Array<{ pid: number; stillAlive: boolean }> = [];

		afterEach(async () => {
			const pids = owned.pids();
			await owned.reap();
			for (const pid of pids) {
				let stillAlive = true;
				try {
					process.kill(pid, 0);
				} catch {
					stillAlive = false;
				}
				reapedBeforeNext.push({ pid, stillAlive });
			}
			await Promise.all(tempDirs.splice(0).map(d => fs.rm(d, { recursive: true, force: true })));
		});

		test("a case returns with a registered child still alive", () => {
			const child = owned.spawn(["sleep", "30"], { stdout: "pipe", stderr: "ignore" });
			expect(child.pid).toBeGreaterThan(0);
			expect(child.exitCode).toBeNull();
			expect(owned.pids()).toEqual([child.pid]);
		});

		test("the next case runs git init and git config after the child exited", async () => {
			// The previous case's awaited afterEach reaped its child; this test only
			// starts once that hook returned.
			expect(reapedBeforeNext).toHaveLength(1);
			expect(reapedBeforeNext[0]!.stillAlive).toBe(false);

			const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-640-lifecycle-"));
			tempDirs.push(dir);
			const init = Bun.spawnSync(["git", "init", "-q"], { cwd: dir, encoding: "utf8" });
			expect(init.exitCode, decode(init.stderr)).toBe(0);
			const email = Bun.spawnSync(["git", "config", "user.email", "lifecycle@example.com"], {
				cwd: dir,
				encoding: "utf8",
			});
			expect(email.exitCode, decode(email.stderr)).toBe(0);
			const readBack = Bun.spawnSync(["git", "config", "--local", "user.email"], { cwd: dir, encoding: "utf8" });
			expect(readBack.exitCode, decode(readBack.stderr)).toBe(0);
			expect(decode(readBack.stdout)).toContain("lifecycle@example.com");
		});

		test.skipIf(process.platform !== "linux")(
			"a deliberate assertion failure while holding a child still finishes the file cleanly",
			async () => {
				const result = await runNestedScenario("assertion-failure");
				expect(result.exitCode).not.toBe(0); // the deliberate failure surfaced
				expect(result.output).toContain("1 pass"); // the later git case ran
				expect(result.output).not.toContain("killed 1 dangling process"); // afterEach reaped in time
			},
			30_000,
		);

		test.skipIf(process.platform !== "linux")(
			"a child-live test past its deadline prints the diagnostic but does not break the next git spawn",
			async () => {
				const result = await runNestedScenario("deadline");
				expect(result.exitCode).not.toBe(0); // the stalled test failed its deadline
				expect(result.output).toContain("killed 1 dangling process"); // bun killed the still-live child
				expect(result.output).toContain("timed out after 300ms"); // deadline below the enclosing timeout
				expect(result.output).toContain("1 pass"); // the later git case still ran and passed
				expect(result.output).not.toContain("could not"); // no spawn of the later case failed
			},
			30_000,
		);
	});
} else if (SCENARIO === "assertion-failure") {
	describe("nested: assertion failure with a live child", () => {
		const owned = new OwnedChildren();
		afterEach(async () => {
			await owned.reap();
		});

		test("fails an assertion while a registered child is alive", () => {
			owned.spawn(["sleep", "30"], { stdout: "pipe", stderr: "pipe" });
			expect(1).toBe(2);
		});

		test("the next case runs git after the afterEach reaped the child", () => {
			const r = Bun.spawnSync(["git", "--version"], { encoding: "utf8" });
			expect(r.exitCode, r.stderr as string).toBe(0);
		});
	});
} else if (SCENARIO === "deadline") {
	describe("nested: child-live test past its deadline", () => {
		const owned = new OwnedChildren();
		afterEach(async () => {
			await owned.reap(200);
		});

		test("overruns a short deadline while a registered child is alive", async () => {
			owned.spawn(["sleep", "30"], { stdout: "pipe", stderr: "ignore" });
			await Bun.sleep(1500);
		}, 300);

		test("the next case runs git after the deadline-enforced kill", () => {
			const r = Bun.spawnSync(["git", "--version"], { encoding: "utf8" });
			expect(r.exitCode, r.stderr as string).toBe(0);
		});
	});
} else {
	throw new Error(`unknown OMP_640_LIFECYCLE_SCENARIO: ${SCENARIO}`);
}
