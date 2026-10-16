import type { Subprocess } from "bun";

/**
 * Registry of child processes a test file explicitly owns.
 *
 * Bun reaps any child still live when a test ends and prints
 * `killed N dangling process`; a test-body `finally` never runs when Bun
 * enforces the test deadline, so cleanup must live in an awaited `afterEach`.
 * Register every spawned child here at spawn time, then reap in `afterEach`.
 */
export class OwnedChildren {
	readonly #live = new Map<number, Subprocess>();

	/** Track an already-spawned child. Returns it unchanged. */
	register<T extends Subprocess>(proc: T): T {
		this.#live.set(proc.pid, proc);
		return proc;
	}

	/** Spawn and register in one step, so no window exists where it is untracked. */
	spawn(...args: Parameters<typeof Bun.spawn>): Subprocess {
		return this.register(Bun.spawn(...args));
	}

	/** PIDs still tracked (exited handles are removed by reap). */
	pids(): number[] {
		return [...this.#live.keys()];
	}

	/**
	 * Terminate every tracked child and wait for it to exit.
	 *
	 * SIGTERM first; after `gracefulMs`, SIGKILL. Only individually owned PIDs
	 * are signalled — no process-group or name-based sweep. Pipe streams are
	 * drained/cancelled so file descriptors are released before a caller
	 * removes the fixture directory.
	 */
	async reap(gracefulMs = 1_000): Promise<void> {
		const procs = [...this.#live.values()];
		this.#live.clear();
		for (const proc of procs) {
			if (proc.exitCode === null) {
				try {
					proc.kill("SIGTERM");
				} catch {
					// already gone
				}
			}
		}
		await Promise.all(
			procs.map(async proc => {
				if (await exitedWithin(proc, gracefulMs)) return;
				if (proc.exitCode === null) {
					try {
						proc.kill("SIGKILL");
					} catch {
						// already gone
					}
				}
				await proc.exited;
			}),
		);
		for (const proc of procs) {
			cancelPipe(proc.stdout);
			cancelPipe(proc.stderr);
		}
	}
}

function exitedWithin(proc: Subprocess, ms: number): Promise<boolean> {
	if (proc.exitCode !== null) return Promise.resolve(true);
	const { promise, resolve } = Promise.withResolvers<boolean>();
	const timer = setTimeout(() => resolve(false), ms);
	void proc.exited.then(() => {
		clearTimeout(timer);
		resolve(true);
	});
	return promise;
}

function cancelPipe(stream: ReadableStream<Uint8Array> | undefined): void {
	if (!stream) return;
	try {
		void stream.cancel().catch(() => undefined);
	} catch {
		// stream already closed
	}
}
