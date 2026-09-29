// OMP-405: the session stop gate polls WorkClient.stopStatus and holds work while engaged.
import { describe, expect, test } from "bun:test";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import type { StopStatusView } from "@oh-my-pi/pi-work-client";
import { installAgentStopGate } from "../extensions/workflow/agent-stop";

const WORKSPACE = "00000000-0000-0000-0000-000000000001";

function status(stopped: boolean, reason: string | null = stopped ? "runaway loop" : null): StopStatusView {
	return {
		workspace_id: WORKSPACE,
		stopped,
		reason,
		changed_at: null,
		changed_by_actor_kind: null,
	};
}

interface Deferred<T> {
	promise: Promise<T>;
	resolve: (value: T) => void;
	reject: (error: unknown) => void;
}

function defer<T>(): Deferred<T> {
	let resolve!: (value: T) => void;
	let reject!: (error: unknown) => void;
	const promise = new Promise<T>((res, rej) => {
		resolve = res;
		reject = rej;
	});
	return { promise, resolve, reject };
}

type Handler = (event: unknown, ctx: FakeCtx) => unknown;

interface IntervalArm {
	callback: () => Promise<void>;
	ms: number;
	handle: object;
}

interface FakeCtx {
	idle: boolean;
	aborts: number;
	notifies: Array<{ message: string; level?: string }>;
	intervals: IntervalArm[];
	cleared: object[];
	isIdle: () => boolean;
	abort: () => void;
	setInterval: (callback: () => Promise<void>, ms?: number) => object;
	clearTimer: (timer: object) => void;
	ui: { notify: (message: string, level?: "info" | "warning" | "error") => void };
}

function fakeCtx(): FakeCtx {
	const ctx: FakeCtx = {
		idle: true,
		aborts: 0,
		notifies: [],
		intervals: [],
		cleared: [],
		isIdle: () => ctx.idle,
		abort: () => {
			ctx.aborts += 1;
		},
		setInterval: (callback, ms) => {
			const handle = {};
			ctx.intervals.push({ callback, ms: ms ?? 0, handle });
			return handle;
		},
		clearTimer: timer => {
			ctx.cleared.push(timer);
		},
		ui: {
			notify: (message, level) => {
				ctx.notifies.push({ message, level });
			},
		},
	};
	return ctx;
}

function install(options?: { pollMs?: number }) {
	const handlers = new Map<string, Handler>();
	const pi = {
		on(event: string, handler: Handler) {
			handlers.set(event, handler);
		},
	};
	const polls: Array<Deferred<StopStatusView>> = [];
	let override: (() => Promise<StopStatusView>) | undefined;
	const client = {
		stopStatus: () => {
			if (override) {
				const run = override;
				override = undefined;
				return run();
			}
			const pending = defer<StopStatusView>();
			polls.push(pending);
			return pending.promise;
		},
	};
	installAgentStopGate(pi as unknown as ExtensionAPI, client, options);
	const ctx = fakeCtx();

	function tick(): Promise<void> {
		const arm = ctx.intervals.at(-1);
		if (!arm) throw new Error("session_start did not arm an interval");
		return arm.callback();
	}

	return {
		handlers,
		ctx,
		polls,
		sessionStart: () => handlers.get("session_start")?.({ type: "session_start" }, ctx),
		tick,
		throwNext(error: unknown = new Error("stop status down")) {
			override = () => {
				throw error;
			};
		},
		rejectNext(error: unknown = new Error("stop status down")) {
			override = () => Promise.reject(error);
		},
		input(text = "do the work") {
			return handlers.get("input")?.({ type: "input", text, source: "interactive" }, ctx);
		},
		tool() {
			return handlers.get("tool_call")?.(
				{ type: "tool_call", toolName: "bash", toolCallId: "call-1", input: { command: "true" } },
				ctx,
			);
		},
	};
}

type Harness = ReturnType<typeof install>;

/** Drive the session_start poll (single-flight with the armed interval callback) to a stopped view. */
async function engage(h: Harness, idle: boolean): Promise<void> {
	h.ctx.idle = idle;
	h.sessionStart();
	const done = h.tick();
	expect(h.polls).toHaveLength(1);
	h.polls[0]?.resolve(status(true, "runaway loop"));
	await done;
}

function expectStopCopy(message: string): void {
	expect(message).toContain("runaway loop");
	expect(message).toContain("Only the owner can release it via omp-work stop release.");
}

describe("agent stop gate (OMP-405)", () => {
	test("does not call stopStatus before session_start", () => {
		const h = install();
		expect(h.polls).toHaveLength(0);
		expect(h.handlers.has("session_start")).toBe(true);
		expect(h.handlers.has("input")).toBe(true);
		expect(h.handlers.has("tool_call")).toBe(true);
	});

	test("session_start resolves while stopStatus never settles", async () => {
		const h = install();
		let done = false;
		void Promise.resolve(h.sessionStart()).then(() => {
			done = true;
		});
		await Promise.resolve();
		expect(done).toBe(true);
		expect(h.polls).toHaveLength(1);
		expect(h.ctx.intervals).toHaveLength(1);
		expect(h.ctx.intervals[0]?.ms).toBe(5000);
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(0);
		const state = await Promise.race([
			h.polls[0]?.promise.then(() => "settled"),
			Promise.resolve("pending"),
		]);
		expect(state).toBe("pending");
		// Unknown until the first poll settles: work is allowed.
		expect(await h.input()).toBeUndefined();
		expect(await h.tool()).toBeUndefined();
		expect(h.ctx.notifies).toHaveLength(0);
	});

	test("pollMs override is the interval delay", () => {
		const h = install({ pollMs: 250 });
		h.sessionStart();
		expect(h.ctx.intervals[0]?.ms).toBe(250);
	});

	test("a later session_start clears the previous interval", () => {
		const h = install();
		h.sessionStart();
		const first = h.ctx.intervals[0]?.handle;
		h.sessionStart();
		expect(h.ctx.cleared).toEqual([first]);
		expect(h.ctx.intervals).toHaveLength(2);
		expect(h.ctx.intervals[1]?.ms).toBe(5000);
		// The first poll is still in flight, so the second session_start does not start another.
		expect(h.polls).toHaveLength(1);
	});

	test("a busy stopped poll aborts and notifies once", async () => {
		const h = install();
		await engage(h, false);
		expect(h.ctx.aborts).toBe(1);
		expect(h.ctx.notifies).toHaveLength(1);
		expect(h.ctx.notifies[0]?.level).toBe("warning");
		expectStopCopy(h.ctx.notifies[0]?.message ?? "");
	});

	test("a second busy stopped poll aborts again with no new notice", async () => {
		const h = install();
		await engage(h, false);
		const done = h.tick();
		expect(h.polls).toHaveLength(2);
		h.polls[1]?.resolve(status(true, "runaway loop"));
		await done;
		expect(h.ctx.aborts).toBe(2);
		expect(h.ctx.notifies).toHaveLength(1);
	});

	test("an idle stopped poll does not abort", async () => {
		const h = install();
		await engage(h, true);
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(1);
		expect(h.ctx.notifies[0]?.level).toBe("warning");
	});

	test("a prompt is refused and a tool call is blocked while stopped", async () => {
		const h = install();
		await engage(h, true);
		const before = h.ctx.notifies.length;
		expect(await h.input("ship the change")).toEqual({ handled: true });
		expect(h.ctx.notifies).toHaveLength(before + 1);
		expect(h.ctx.notifies.at(-1)?.level).toBe("warning");
		expectStopCopy(h.ctx.notifies.at(-1)?.message ?? "");
		const blocked = (await h.tool()) as { block?: boolean; reason?: string } | undefined;
		expect(blocked?.block).toBe(true);
		expectStopCopy(blocked?.reason ?? "");
		expect(h.ctx.notifies).toHaveLength(before + 1);
	});

	test("a throwing poll after stopped still blocks", async () => {
		const h = install();
		await engage(h, true);
		const notices = h.ctx.notifies.length;
		h.throwNext();
		await expect(h.tick()).resolves.toBeUndefined();
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(notices);
		expect(await h.input()).toEqual({ handled: true });
		const blocked = (await h.tool()) as { block?: boolean; reason?: string } | undefined;
		expect(blocked?.block).toBe(true);
		expect(blocked?.reason ?? "").toContain("omp-work stop release");

		h.rejectNext();
		await expect(h.tick()).resolves.toBeUndefined();
		expect(((await h.tool()) as { block?: boolean } | undefined)?.block).toBe(true);
		expect(h.ctx.notifies.every(notice => notice.level !== "info")).toBe(true);
	});

	test("tick during a pending poll makes no second stopStatus call", async () => {
		const h = install();
		h.sessionStart();
		expect(h.polls).toHaveLength(1);
		const first = h.tick();
		const second = h.tick();
		expect(first).toBe(second);
		expect(h.polls).toHaveLength(1);
		h.polls[0]?.resolve(status(false));
		await first;
		expect(h.polls).toHaveLength(1);
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(0);
	});

	test("a released poll notifies once and lets the prompt and tool through", async () => {
		const h = install();
		await engage(h, true);
		h.ctx.idle = false;
		const done = h.tick();
		expect(h.polls).toHaveLength(2);
		h.polls[1]?.resolve(status(false, "operator resumed work"));
		await done;
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(2);
		expect(h.ctx.notifies[1]).toEqual({ message: "Agent stop released.", level: "info" });
		expect(await h.input("continue")).toBeUndefined();
		expect(await h.tool()).toBeUndefined();

		const again = h.tick();
		h.polls[2]?.resolve(status(false, "operator resumed work"));
		await again;
		expect(h.ctx.notifies).toHaveLength(2);
		expect(await h.input("continue")).toBeUndefined();
		expect(await h.tool()).toBeUndefined();
	});
});
