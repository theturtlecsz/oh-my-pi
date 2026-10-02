// OMP-536: before_agent_start reads stopStatus directly and can refuse a prompt
// while the first poll is still pending. The read does not change poll state.
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
	isIdle: () => boolean;
	abort: () => void;
	setInterval: (callback: () => Promise<void>, ms?: number) => object;
	clearTimer: (timer: object) => void;
	ui: { notify: (message: string, level?: "info" | "warning" | "error") => void };
}

function fakeCtx(): FakeCtx {
	const ctx: FakeCtx = {
		idle: false,
		aborts: 0,
		notifies: [],
		intervals: [],
		isIdle: () => ctx.idle,
		abort: () => {
			ctx.aborts += 1;
		},
		setInterval: (callback, ms) => {
			const handle = {};
			ctx.intervals.push({ callback, ms: ms ?? 0, handle });
			return handle;
		},
		clearTimer: () => {},
		ui: {
			notify: (message, level) => {
				ctx.notifies.push({ message, level });
			},
		},
	};
	return ctx;
}

function install() {
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
	installAgentStopGate(pi as unknown as ExtensionAPI, client);
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
		prompt() {
			return handlers.get("before_agent_start")?.(
				{ type: "before_agent_start", prompt: "ship the change", systemPrompt: [] },
				ctx,
			);
		},
	};
}

type Admission = { block?: boolean; reason?: string } | undefined;

async function stillPending(poll: Deferred<StopStatusView> | undefined): Promise<void> {
	const state = await Promise.race([poll?.promise.then(() => "settled"), Promise.resolve("pending")]);
	expect(state).toBe("pending");
}

describe("agent stop prompt admission (OMP-536)", () => {
	test("a stopped read blocks while the first poll is still pending", async () => {
		const h = install();
		h.sessionStart();
		expect(h.polls).toHaveLength(1);
		await stillPending(h.polls[0]);

		const admission = h.prompt();
		expect(h.polls).toHaveLength(2);
		h.polls[1]?.resolve(status(true, "drill"));
		const result = (await admission) as Admission;

		expect(result?.block).toBe(true);
		expect(result?.reason ?? "").toContain("Agent stop engaged");
		expect(result?.reason ?? "").toContain("drill");
		expect(result?.reason ?? "").toContain("omp-work stop release");
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(0);
		await stillPending(h.polls[0]);
		expect(await h.input()).toBeUndefined();
		expect(await h.tool()).toBeUndefined();
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(0);
	});

	test("a released read allows the prompt", async () => {
		const h = install();
		h.sessionStart();
		const admission = h.prompt();
		expect(h.polls).toHaveLength(2);
		h.polls[1]?.resolve(status(false));
		expect(await admission).toBeUndefined();
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(0);
		await stillPending(h.polls[0]);
	});

	test("a rejected read after a settled stopped poll blocks, and does not abort or notify", async () => {
		const h = install();
		h.sessionStart();
		const done = h.tick();
		expect(h.polls).toHaveLength(1);
		h.polls[0]?.resolve(status(true, "runaway loop"));
		await done;
		expect(h.ctx.aborts).toBe(1);
		expect(h.ctx.notifies).toHaveLength(1);

		h.rejectNext();
		const rejected = (await h.prompt()) as Admission;
		expect(rejected?.block).toBe(true);
		expect(rejected?.reason ?? "").toContain("Agent stop engaged");
		expect(rejected?.reason ?? "").toContain("runaway loop");
		expect(rejected?.reason ?? "").toContain("omp-work stop release");
		expect(h.ctx.aborts).toBe(1);
		expect(h.ctx.notifies).toHaveLength(1);

		h.throwNext();
		const thrown = (await h.prompt()) as Admission;
		expect(thrown?.block).toBe(true);
		expect(h.ctx.aborts).toBe(1);
		expect(h.ctx.notifies).toHaveLength(1);
	});

	test("a rejected or throwing read with no settled poll allows the prompt", async () => {
		const h = install();
		h.sessionStart();
		await stillPending(h.polls[0]);

		h.rejectNext();
		expect(await h.prompt()).toBeUndefined();
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(0);
		await stillPending(h.polls[0]);

		h.throwNext();
		expect(await h.prompt()).toBeUndefined();
		expect(h.ctx.aborts).toBe(0);
		expect(h.ctx.notifies).toHaveLength(0);
		expect(await h.input()).toBeUndefined();
		expect(await h.tool()).toBeUndefined();
	});
});
