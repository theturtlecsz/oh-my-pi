/** Bounded process ownership regressions; see docs/omp-640-process-lifecycle.md. */
import * as fsSync from "node:fs";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { isEnoent } from "@oh-my-pi/pi-utils/fs-error";
import type { Subprocess } from "bun";
import { afterEach, describe, expect, test } from "bun:test";
import { OwnedChildren } from "./fixtures/owned-children";

const SCENARIO = process.env.OMP_640_LIFECYCLE_SCENARIO;
const REPO_ROOT = path.resolve(import.meta.dir, "../..");
const SESSION_PRELOAD = path.join(REPO_ROOT, "scripts/session-system-test-preload.ts");
const REAL_LIFECYCLE_FILES = [
	"session-system/tests/omp-262-plan-identity.test.ts",
	"session-system/tests/update.test.ts",
] as const;
const owned = new OwnedChildren();
const tempDirs: string[] = [];
let previousChild: Subprocess<"ignore", "pipe", "pipe"> | undefined;
let bodyFinally = false;
// Timed-out bodies whose continuations still use fixtures (see the identity test).
const pendingRuns: Promise<unknown>[] = [];

// Hooks run even after assertion/deadline failures. Always finish process and
// pipe cleanup before deleting paths that unfinished work might still use.
afterEach(async () => {
	await Promise.allSettled(pendingRuns.splice(0));
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

interface ProcStat {
	state: string;
	ppid: number;
}

/** /proc entries vanish mid-read; a missing entry is an exited process. */
function readProcStat(pid: number): ProcStat | undefined {
	try {
		const text = fsSync.readFileSync(`/proc/${pid}/stat`, "utf8");
		const end = text.lastIndexOf(")");
		if (end < 0) return undefined;
		const rest = text.slice(end + 2).split(" ");
		const ppid = Number(rest[1]);
		if (!rest[0] || !Number.isInteger(ppid)) return undefined;
		return { state: rest[0], ppid };
	} catch {
		return undefined;
	}
}

function descendantPids(rootPid: number): number[] {
	let names: string[];
	try {
		names = fsSync.readdirSync("/proc");
	} catch {
		return [];
	}
	const children = new Map<number, number[]>();
	for (const name of names) {
		if (!/^[0-9]+$/.test(name)) continue;
		const pid = Number(name);
		const stat = readProcStat(pid);
		if (!stat) continue;
		const list = children.get(stat.ppid);
		if (list) list.push(pid);
		else children.set(stat.ppid, [pid]);
	}
	const out: number[] = [];
	const stack = [...(children.get(rootPid) ?? [])];
	const seen = new Set<number>();
	while (stack.length > 0) {
		const pid = stack.pop();
		if (pid === undefined || seen.has(pid)) continue;
		seen.add(pid);
		out.push(pid);
		stack.push(...(children.get(pid) ?? []));
	}
	return out;
}

function sessionRoots(scratch: string): string[] {
	try {
		return fsSync
			.readdirSync(scratch)
			.filter(name => name.startsWith("omp-session-test-"))
			.map(name => path.join(scratch, name));
	} catch {
		return [];
	}
}

function underScratch(scratch: string, target: string): boolean {
	return target === scratch || target.startsWith(`${scratch}${path.sep}`);
}

interface FixtureChild {
	kind: "git" | "python";
	paths: string[];
}

/** Git and mapper children whose paths live in the preload's private root. */
function fixtureChild(pid: number, scratch: string): FixtureChild | undefined {
	const stat = readProcStat(pid);
	if (!stat || stat.state === "Z") return undefined;
	let args: string[];
	try {
		args = fsSync
			.readFileSync(`/proc/${pid}/cmdline`)
			.toString("utf8")
			.split("\0")
			.filter(arg => arg.length > 0);
	} catch {
		return undefined;
	}
	const command = args[0];
	if (!command) return undefined;
	const base = path.basename(command);
	if (base === "python3" || base === "python") {
		const paths = args.slice(1).filter(arg => underScratch(scratch, arg));
		if (paths.length === 0) return undefined;
		return { kind: "python", paths };
	}
	if (base !== "git" && !command.endsWith("/git")) return undefined;
	const paths: string[] = [];
	for (let index = 0; index < args.length; index++) {
		const next = args[index + 1];
		if (args[index] === "-C" && next && underScratch(scratch, next)) paths.push(next);
	}
	try {
		const cwd = fsSync.readlinkSync(`/proc/${pid}/cwd`);
		if (underScratch(scratch, cwd)) paths.push(cwd);
	} catch {
		// The process can exit between cmdline and cwd.
	}
	if (paths.length === 0) return undefined;
	return { kind: "git", paths };
}

interface Lifetime {
	git: number;
	python: number;
	sawRoot: boolean;
	violations: string[];
	seenPids: Set<number>;
}

function visibleOutput(text: string): string {
	return text.replace(/\u001b\[[0-9;]*m/g, "");
}

function pathExists(target: string): boolean {
	try {
		fsSync.accessSync(target);
		return true;
	} catch {
		return false;
	}
}

function sampleLifetime(runnerPid: number, scratch: string, lifetime: Lifetime): void {
	const roots = sessionRoots(scratch);
	if (roots.length > 0) lifetime.sawRoot = true;
	for (const pid of descendantPids(runnerPid)) {
		const child = fixtureChild(pid, scratch);
		if (!child) continue;
		const missing = child.paths.filter(target => !pathExists(target));
		const still = readProcStat(pid);
		if (!still || still.state === "Z") continue;
		lifetime.seenPids.add(pid);
		if (missing.length > 0 || roots.length === 0) {
			if (lifetime.violations.length < 8) {
				const gone = missing.length > 0 ? missing.join(", ") : "preload root";
				lifetime.violations.push(`${child.kind} pid ${pid} alive while ${gone} is gone`);
			}
			continue;
		}
		if (child.kind === "git") lifetime.git++;
		else lifetime.python++;
	}
}

async function observeUntilExit(runnerPid: number, scratch: string, exited: Promise<unknown>): Promise<Lifetime> {
	const lifetime: Lifetime = { git: 0, python: 0, sawRoot: false, violations: [], seenPids: new Set() };
	let done = false;
	void exited.then(
		() => {
			done = true;
		},
		() => {
			done = true;
		},
	);
	while (!done) {
		sampleLifetime(runnerPid, scratch, lifetime);
		await Bun.sleep(20);
	}
	sampleLifetime(runnerPid, scratch, lifetime);
	return lifetime;
}

if (SCENARIO === undefined) {
	describe("owned-child lifecycle (OMP-640)", () => {
		test("returns with registered child alive; awaited hook owns its exit", () => {
			previousChild = spawnChild();
			expect(previousChild.exitCode).toBeNull();
		});

		test("next case initializes and configures git after child and pipe cleanup", gitAfterExit, 5_000);

		for (const scenario of [
			"assertion-failure",
			"operation-deadline",
			"test-deadline",
			"sync-deadline",
			"continuation-deadline",
		]) {
			test(`awaited hook reaps child after ${scenario} before next git`, async () => {
				const result = await runNestedScenario(scenario);
				expect(result.exit).not.toBe(0);
				expect(result.signal).toBeNull();
				expect(result.output).toContain("1 pass");
				expect(result.output).toContain("1 fail");
				expect(result.output).toContain("hook:reaped");
				expect(result.output).toContain("next:git:0");
				expect(result.output.indexOf("hook:reaped")).toBeLessThan(result.output.indexOf("next:git:0"));
				if (scenario === "test-deadline" || scenario === "sync-deadline" || scenario === "continuation-deadline") {
					expect(result.output).toContain("timed out after 100ms");
					expect(result.output).toContain(
						scenario === "test-deadline" ? "killed 1 dangling process" : "killed 2 dangling processes",
					);
					if (scenario === "continuation-deadline") {
						// The continuation read its fixture; teardown waited for it.
						expect(result.output).toContain("plan:read:plan body");
						expect(result.output).not.toContain("ENOENT");
						expect(result.output.indexOf("plan:read")).toBeLessThan(result.output.indexOf("hook:reaped"));
					}
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

		// Reduced scenarios above use a synthetic body. This runs the repaired
		// files under the real session preload and watches the private root those
		// files actually use. The runner is an owned child: afterEach reaps it,
		// then removes this scratch dir.
		test.skipIf(process.platform !== "linux")(
			"real files pass under the session preload while fixtures outlive their children",
			async () => {
				const scratch = await fs.mkdtemp(path.join(os.tmpdir(), "omp-640-real-"));
				tempDirs.push(scratch);
				const proc = owned.spawn(
					[process.execPath, "test", "--preload", SESSION_PRELOAD, "--timeout", "30000", ...REAL_LIFECYCLE_FILES],
					{
						cwd: REPO_ROOT,
						env: { ...process.env, TMPDIR: scratch, TMP: scratch, TEMP: scratch },
						stdout: "pipe",
						stderr: "pipe",
						timeout: 120_000,
						killSignal: "SIGKILL",
					},
				);
				const lifetimePromise = observeUntilExit(proc.pid, scratch, proc.exited);
				const [exitCode, pipes] = await Promise.all([proc.exited, owned.output(proc)]);
				const lifetime = await lifetimePromise;
				const outputText = visibleOutput(pipes.stdout + pipes.stderr);
				expect(proc.signalCode).toBeNull();
				expect(exitCode, outputText).toBe(0);
				expect(outputText).toContain("omp-262-plan-identity.test.ts");
				expect(outputText).toContain("update.test.ts");
				expect(Number(/^\s*(\d+) pass$/m.exec(outputText)?.[1] ?? "0")).toBeGreaterThan(0);
				expect(/^\s*(\d+) fail$/m.exec(outputText)?.[1]).toBe("0");
				expect(outputText).not.toMatch(/killed \d+ dangling process/);
				expect(lifetime.sawRoot).toBe(true);
				expect(lifetime.git).toBeGreaterThan(0);
				expect(lifetime.python).toBeGreaterThan(0);
				expect(lifetime.violations, lifetime.violations.join("\n")).toEqual([]);
				const lingering = [...lifetime.seenPids].filter(pid => readProcStat(pid) !== undefined);
				expect(lingering).toEqual([]);
				expect(sessionRoots(scratch)).toEqual([]);
			},
			150_000,
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
			if (SCENARIO === "continuation-deadline") {
				const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-640-plan-"));
				tempDirs.push(dir);
				const plan = path.join(dir, "plan.md");
				await Bun.write(plan, "plan body");
				const run = (async () => {
					// Like createWorkflowHost's git probe: the deadline interrupts this
					// synchronous call and teardown starts before the body resumes.
					Bun.spawnSync([process.execPath, "-e", "Bun.sleepSync(500)"], { timeout: 1_500 });
					console.log(`plan:read:${await Bun.file(plan).text()}`);
				})();
				pendingRuns.push(run);
				await run;
				return;
			}
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
		SCENARIO === "test-deadline" || SCENARIO === "sync-deadline" || SCENARIO === "continuation-deadline"
			? 100
			: 2_000,
	);

	test("next case observes exit before git init/config", gitAfterExit, 2_000);
}
