/**
 * Regression for https://github.com/can1357/oh-my-pi/issues/4324
 *
 * The Kokoro TTS worker crash-loops with `exit code 7`, but every worker
 * subprocess was spawned with `stderr: "ignore"` — so the native crash message
 * (ONNX Runtime traceback, glibc assertion, segfault details) was discarded
 * and the parent only ever logged `tts subprocess exited with code 7`, with no
 * way to diagnose what actually blew up.
 *
 * The fix pipes stderr without starting a live read while the worker is idle;
 * after `onExit`, it drains the pipe, keeps the last 16 KiB in a bounded ring,
 * and appends that tail to the `Error` surfaced to `onError` handlers. These
 * tests pin that contract so the exit-code-7 crash (and the next one) actually
 * shows up in `~/.omp/logs/omp.log` without regressing idle-worker shutdown.
 */
import { describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { createWorkerSubprocess, type SpawnedSubprocess } from "@oh-my-pi/pi-coding-agent/subprocess/worker-client";

interface FakeWorkerOutbound {
	type: "pong";
	id: string;
}

/** Build a spawn command that emits `stderr` verbatim then exits with `exitCode`. */
function stderrExitCommand(stderr: string, exitCode: number): { cmd: string[] } {
	const script = `process.stderr.write(${JSON.stringify(stderr)}); process.exit(${exitCode});`;
	return { cmd: [process.execPath, "-e", script] };
}

/**
 * Resolve the first worker-error handler fires with. The subprocess wires the
 * error surface off `stderrDrained` so this always resolves after the stderr
 * tail has fully drained — no wall-clock races.
 */
function firstWorkerError(sub: SpawnedSubprocess<FakeWorkerOutbound>): Promise<Error> {
	const { promise, resolve } = Promise.withResolvers<Error>();
	sub.errors.add(resolve);
	return promise;
}

describe("issue #4324 — worker subprocess stderr survives to the exit error", () => {
	it("surfaces stderr in the onExit error when the worker exits non-zero", async () => {
		const stderr =
			"onnxruntime[Native]: Non-zero status code returned while executing Add node.\n" +
			"terminate called after throwing an instance of 'std::runtime_error'\n" +
			"  what():  cudaMemcpy failed\n";
		const sub = createWorkerSubprocess<FakeWorkerOutbound>({
			spawnCommand: stderrExitCommand(stderr, 7),
			env: {},
			exitLabel: "tts subprocess",
		});
		const err = await firstWorkerError(sub);
		// The exit-code prefix is preserved so existing log parsers keep working.
		expect(err.message).toStartWith("tts subprocess exited with code 7");
		// The actual native crash reason must now be part of the error.
		expect(err.message).toContain("onnxruntime[Native]");
		expect(err.message).toContain("cudaMemcpy failed");
	}, 15_000);

	it("truncates a large stderr to the last ~16 KiB so a chatty runtime can't blow the parent up", async () => {
		// Write well past the 16 KiB tail limit. A recognisable trailer must
		// still land at the end so the diagnostic tail is what survives.
		const filler = "A".repeat(64 * 1024);
		const trailer = "FATAL: onnxruntime session run failed\n";
		const sub = createWorkerSubprocess<FakeWorkerOutbound>({
			spawnCommand: stderrExitCommand(filler + trailer, 7),
			env: {},
			exitLabel: "tts subprocess",
		});
		const err = await firstWorkerError(sub);
		// Trailer wins.
		expect(err.message).toContain("FATAL: onnxruntime session run failed");
		// Truncation happened — we did not append the whole 64 KiB.
		expect(err.message.length).toBeLessThan(20_000);
	}, 15_000);

	it("does not surface intentional terminate() SIGKILLs as worker errors", async () => {
		// Regression guard: piping stderr must not change the semantics of an
		// intentional teardown. The wrapper's `terminate()` flips
		// `intentionalExit` then SIGKILLs — the error channel must stay quiet.
		const sub = createWorkerSubprocess<FakeWorkerOutbound>({
			// A sleeping child so we get to SIGKILL it before it exits on its own.
			spawnCommand: {
				cmd: [process.execPath, "-e", "Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 60000);"],
			},
			env: {},
			exitLabel: "tts subprocess",
		});
		let errored = false;
		sub.errors.add(() => {
			errored = true;
		});
		sub.intentionalExit.value = true;
		sub.proc.kill("SIGKILL");
		// Wait for both process reap AND stderr drain so any latent error
		// path has had its shot at firing before we assert silence.
		await sub.proc.exited;
		await sub.stderrDrained;
		expect(errored).toBe(false);
	}, 15_000);

	it("leaves no capture dir behind when its owning process is hard-killed", async () => {
		// OMP-389: the parent owns the capture for its whole lifetime. On POSIX the
		// log is unlinked while its fd is open, so no directory entry is ever
		// created — a `SIGKILL` to the parent (which never runs the `exit` handler)
		// therefore cannot leave an `omp-worker-stderr-*` dir in the temp root.
		const repoRoot = path.resolve(import.meta.dir, "..");
		// The child watches the wrapper and exits once that owner is gone, so this
		// test never leaves an orphaned worker process behind.
		const workerScript =
			"const ownerPid = Number(process.argv.at(-1)); const lock = new Int32Array(new SharedArrayBuffer(4)); while (true) { try { process.kill(ownerPid, 0); } catch { break; } Atomics.wait(lock, 0, 0, 100); }";
		const wrapperScript = `
			const { createWorkerSubprocess } = await import("@oh-my-pi/pi-coding-agent/subprocess/worker-client");
			const sub = createWorkerSubprocess({
				spawnCommand: { cmd: [process.execPath, "-e", ${JSON.stringify(workerScript)}, String(process.pid)] },
				env: {},
				exitLabel: "killed subprocess",
			});
			process.stdout.write("READY:" + String(sub.proc.pid));
			await new Promise(() => {});
		`;
		const captureNames = (baseline: Set<string>): string[] =>
			(fs.readdirSync(os.tmpdir()) as string[]).filter(
				name => name.startsWith("omp-worker-stderr-") && !baseline.has(name),
			);
		const baseline = new Set(fs.readdirSync(os.tmpdir()) as string[]);

		const wrapper = Bun.spawn([process.execPath, "-e", wrapperScript], {
			cwd: repoRoot,
			stdout: "pipe",
			stderr: "pipe",
			env: { ...process.env, BUN_ENV: "development", NODE_ENV: "development", PI_TEST_RUNTIME: "0" },
		});
		const reader = (wrapper.stdout as ReadableStream<Uint8Array>).getReader();
		const decoder = new TextDecoder();
		let stdout = "";
		try {
			const deadline = Date.now() + 10_000;
			while (!stdout.includes("READY:") && Date.now() < deadline) {
				const { value, done } = await reader.read();
				if (done) break;
				stdout += decoder.decode(value, { stream: true });
			}
			expect(stdout).toContain("READY:");
			const workerPid = Number(stdout.slice(stdout.indexOf("READY:") + "READY:".length).trim());
			expect(Number.isSafeInteger(workerPid)).toBe(true);

			// While the owner is still alive: POSIX keeps the capture only in the
			// unlinked fd, never as a temp dir (Windows retains it until exit).
			if (process.platform !== "win32") expect(captureNames(baseline)).toEqual([]);

			wrapper.kill("SIGKILL");
			await wrapper.exited;

			if (process.platform === "win32") return; // The owner-marker reap covers Windows on the next spawn.
			// The hard-killed owner ran no exit handler, yet left nothing behind.
			expect(captureNames(baseline)).toEqual([]);
		} finally {
			wrapper.kill("SIGKILL");
			await reader.cancel().catch(() => {});
		}
	}, 20_000);
});
