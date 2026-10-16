import type { SpawnOptions, Subprocess } from "bun";

interface OwnedPipe {
	finished: boolean;
	reader: ReadableStreamDefaultReader<Uint8Array>;
	text: Promise<string>;
}

interface OwnedChild {
	proc: Subprocess;
	stdout?: OwnedPipe;
	stderr?: OwnedPipe;
}

/** Explicit child ownership. Await reap() in afterEach before removing fixtures. */
export class OwnedChildren {
	readonly #children = new Map<number, OwnedChild>();

	/** Register immediately at spawn; this registry also owns all pipe readers. */
	register<T extends Subprocess>(proc: T): T {
		if (this.#children.has(proc.pid)) throw new Error(`Child ${proc.pid} already registered`);
		const child: OwnedChild = { proc };
		this.#children.set(proc.pid, child);
		child.stdout = ownPipe(proc.stdout);
		child.stderr = ownPipe(proc.stderr);
		return proc;
	}

	spawn<
		const In extends SpawnOptions.Writable = "ignore",
		const Out extends SpawnOptions.Readable = "pipe",
		const Err extends SpawnOptions.Readable = "inherit",
	>(cmd: string[], options?: SpawnOptions.SpawnOptions<In, Out, Err>): Subprocess<In, Out, Err> {
		return this.register(Bun.spawn(cmd, options));
	}

	pids(): number[] {
		return [...this.#children.keys()];
	}

	/** Read through the registered readers, never lock proc.stdout/stderr again. */
	async output(proc: Subprocess): Promise<{ stdout: string; stderr: string }> {
		const child = this.#children.get(proc.pid);
		if (!child) throw new Error(`Child ${proc.pid} is not registered`);
		const [stdout, stderr] = await Promise.all([child.stdout?.text ?? "", child.stderr?.text ?? ""]);
		return { stdout, stderr };
	}

	/** Signal only owned handles, escalate after grace, then finish every read. */
	async reap(gracefulMs = 200): Promise<void> {
		const results = await Promise.allSettled(
			[...this.#children.values()].map(async child => {
				const { proc } = child;
				if (proc.exitCode === null) proc.kill("SIGTERM");
				if (!(await exitedWithin(proc, gracefulMs))) {
					if (proc.exitCode === null) proc.kill("SIGKILL");
					if (!(await exitedWithin(proc, 1_000))) throw new Error(`Child ${proc.pid} did not exit after SIGKILL`);
				}
				await proc.exited;
				// Exit does not imply pipe EOF (another writer could hold the fd).
				// Cancel via our readers and await both cancellation and read loops.
				await Promise.all([finishPipe(child.stdout), finishPipe(child.stderr)]);
				this.#children.delete(proc.pid);
			}),
		);
		const errors = results.filter(result => result.status === "rejected").map(result => result.reason);
		if (errors.length) throw new AggregateError(errors, "Owned-child cleanup failed");
	}
}

function ownPipe(stream: ReadableStream<Uint8Array> | number | undefined): OwnedPipe | undefined {
	if (stream === undefined || typeof stream === "number") return undefined;
	const reader = stream.getReader();
	const pipe: OwnedPipe = { reader, finished: false, text: Promise.resolve("") };
	pipe.text = (async () => {
		const decoder = new TextDecoder();
		let text = "";
		try {
			while (true) {
				const { value, done } = await reader.read();
				if (done) return text + decoder.decode();
				text += decoder.decode(value, { stream: true });
			}
		} finally {
			pipe.finished = true;
			reader.releaseLock();
		}
	})();
	// A read can fail before the test awaits output/reap. Retain the rejection
	// for those consumers without reporting an unrelated unhandled rejection.
	void pipe.text.catch(() => {});
	return pipe;
}

async function finishPipe(pipe: OwnedPipe | undefined): Promise<void> {
	if (!pipe) return;
	const results = await Promise.allSettled([pipe.finished ? undefined : pipe.reader.cancel(), pipe.text]);
	const errors = results.filter(result => result.status === "rejected").map(result => result.reason);
	if (errors.length) throw new AggregateError(errors, "Owned pipe cleanup failed");
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
