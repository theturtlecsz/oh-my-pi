/** Bounded process ownership regressions; see docs/omp-640-process-lifecycle.md. */
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { isEnoent } from "@oh-my-pi/pi-utils/fs-error";
import type { Subprocess } from "bun";
import { afterEach, describe, expect, test } from "bun:test";
import { OwnedChildren } from "./fixtures/owned-children";

const SCENARIO = process.env.OMP_640_LIFECYCLE_SCENARIO;
const owned = new OwnedChildren();
const tempDirs: string[] = [];
let previousChild: Subprocess<"ignore", "pipe", "pipe"> | undefined;
let bodyFinally = false;

// Hooks run even after assertion/deadline failures. Always finish process and
// pipe cleanup before deleting paths that unfinished work might still use.
afterEach(async () => {
	await owned.reap();
	await Promise.all(tempDirs.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
	if (SCENARIO) console.log("hook:reaped");
});

function spawnChild(): Subprocess<"ignore", "pipe", "pipe"> {
	// Child has its own safety deadline, below both runner and parent deadlines.
	return owned.spawn([process.execPath, "-e", "await Bun.sleep(1500)"], {
		stdout: "pipe",
		stderr: "pipe",
		timeout: 1_800,
		killSignal: "SIGKILL",
	});
}

async function gitAfterExit(): Promise<void> {
	expect(previousChild).toBeDefined();
	expect(previousChild!.exitCode !== null || previousChild!.signalCode !== null).toBe(true);
	expect(previousChild!.stdout?.locked).toBe(false);
	expect(previousChild!.stderr?.locked).toBe(false);
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-640-git-"));
	tempDirs.push(dir);
	for (const args of [
		["init", "-q"],
		["config", "user.email", "lifecycle@example.com"],
		["config", "--local", "user.email"],
	]) {
		// Match createWorkflowHost's synchronous spawn path. This API returns
		// only after exit; no live handle or pending pipe reader escapes it.
		const result = Bun.spawnSync(["git", ...args], { cwd: dir, timeout: 1_000 });
		expect(result.exitCode, result.stderr.toString()).toBe(0);
		if (args[0] === "config" && args[1] === "--local")
			expect(result.stdout.toString().trim()).toBe("lifecycle@example.com");
	}
	if (SCENARIO === "test-deadline") {
		expect(bodyFinally).toBe(false);
		await Bun.sleep(550);
		expect(bodyFinally).toBe(true);
	}
	if (SCENARIO) console.log("next:git:0");
}

async function runNestedScenario(
	scenario: string,
	timeout = 4_000,
): Promise<{ exit: number; output: string; signal: string | null }> {
	const proc = owned.spawn([process.execPath, "test", "--timeout", "2000", import.meta.path], {
		cwd: path.resolve(import.meta.dir, "../.."),
		env: { ...process.env, OMP_640_LIFECYCLE_SCENARIO: scenario },
		stdout: "pipe",
		stderr: "pipe",
		timeout,
		killSignal: "SIGKILL",
	});
	const [exit, output] = await Promise.all([proc.exited, owned.output(proc)]);
	return { exit, output: output.stdout + output.stderr, signal: proc.signalCode };
}

if (SCENARIO === undefined) {
	describe("owned-child lifecycle (OMP-640)", () => {
		test("returns with registered child alive; awaited hook owns its exit", () => {
			previousChild = spawnChild();
			expect(previousChild.exitCode).toBeNull();
		});

		test("next case initializes and configures git after child and pipe cleanup", gitAfterExit, 5_000);

		for (const scenario of ["assertion-failure", "operation-deadline", "test-deadline", "sync-deadline"]) {
			test(`awaited hook reaps child after ${scenario} before next git`, async () => {
				const result = await runNestedScenario(scenario);
				expect(result.exit).not.toBe(0);
				expect(result.signal).toBeNull();
				expect(result.output).toContain("1 pass");
				expect(result.output).toContain("1 fail");
				expect(result.output).toContain("hook:reaped");
				expect(result.output).toContain("next:git:0");
				expect(result.output.indexOf("hook:reaped")).toBeLessThan(result.output.indexOf("next:git:0"));
				if (scenario === "test-deadline" || scenario === "sync-deadline") {
					expect(result.output).toContain("timed out after 100ms");
					expect(result.output).toContain(
						scenario === "sync-deadline" ? "killed 2 dangling processes" : "killed 1 dangling process",
					);
					if (scenario === "test-deadline")
						expect(result.output.indexOf("hook:reaped")).toBeLessThan(result.output.indexOf("body:finally"));
				} else {
					expect(result.output).not.toMatch(/killed \d+ dangling process/);
					expect(result.output).toContain(
						scenario === "assertion-failure" ? "deliberate assertion" : "operation deadline",
					);
				}
			}, 10_000);
		}

		test("parent kills and reaps a stalled nested runner below enclosing timeout", async () => {
			const result = await runNestedScenario("runner-stall", 150);
			expect(result.signal).toBe("SIGKILL");
			expect(result.exit).not.toBe(0);
		}, 10_000);

		test.skipIf(process.platform === "win32")(
			"escalates SIGTERM and settles owned reads before fixture removal",
			async () => {
				const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-640-pipes-"));
				tempDirs.push(dir);
				const ready = path.join(dir, "ready");
				const readyFile = Bun.file(ready);
				const child = owned.spawn(
					[
						process.execPath,
						"-e",
						'process.on("SIGTERM", () => {}); console.log("pipe payload"); await Bun.write(process.argv[1], "ready"); await Bun.sleep(1500); process.exit(42)',
						ready,
					],
					{ stdout: "pipe", stderr: "pipe", timeout: 2_000, killSignal: "SIGKILL" },
				);
				const output = owned.output(child);
				const deadline = Date.now() + 1_000;
				while (true) {
					try {
						await fs.access(ready);
						break;
					} catch (error) {
						if (!isEnoent(error)) throw error;
					}
					if (Date.now() >= deadline) throw new Error("Child did not become ready");
					await Bun.sleep(5);
				}
				await Promise.race([
					owned.reap(20),
					Bun.sleep(1_000).then(() => {
						throw new Error("Cleanup exceeded operation deadline");
					}),
				]);
				expect(child.signalCode).toBe("SIGKILL");
				expect(child.stdout?.locked).toBe(false);
				expect(child.stderr?.locked).toBe(false);
				expect((await output).stdout).toContain("pipe payload");
				expect(await readyFile.text()).toBe("ready");
			},
			5_000,
		);
	});
} else if (SCENARIO === "runner-stall") {
	// No grandchildren: the parent tests its independent runner safety deadline.
	await Bun.sleep(30_000);
} else {
	test(
		"leaves a registered child through a failing path",
		async () => {
			previousChild = spawnChild();
			if (SCENARIO === "sync-deadline") {
				// The test deadline interrupts this synchronous wait, as in the
				// identity path. Its bounded child is reaped by spawnSync itself.
				Bun.spawnSync([process.execPath, "-e", "Bun.sleepSync(500)"], { timeout: 1_500 });
				return;
			}
			if (SCENARIO === "assertion-failure") expect(1, "deliberate assertion failure").toBe(2);
			if (SCENARIO === "operation-deadline") {
				// Operation deadline precedes Bun's test deadline, allowing hook cleanup
				// without Bun's emergency dangling-process kill.
				await Promise.race([
					previousChild.exited,
					Bun.sleep(30).then(() => {
						throw new Error("operation deadline");
					}),
				]);
			}
			try {
				await Bun.sleep(500);
			} finally {
				bodyFinally = true;
				console.log("body:finally");
			}
		},
		SCENARIO === "test-deadline" || SCENARIO === "sync-deadline" ? 100 : 2_000,
	);

	test("next case observes exit before git init/config", gitAfterExit, 2_000);
}
