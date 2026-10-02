import { afterEach, describe, expect, it, vi } from "bun:test";
import type { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { LoadExtensionsResult } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { AgentRegistry } from "@oh-my-pi/pi-coding-agent/registry/agent-registry";
import type { CreateAgentSessionResult } from "@oh-my-pi/pi-coding-agent/sdk";
import * as sdkModule from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession, AgentSessionEvent } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { runSubprocess } from "@oh-my-pi/pi-coding-agent/task/executor";
import type { AgentDefinition } from "@oh-my-pi/pi-coding-agent/task/types";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import { createSessionDefaults } from "../helpers/session-defaults";

/**
 * Contract: `task.maxRuntimeMs` has a non-zero default of 2 hours. A subagent
 * spawned without an explicit `task.maxRuntimeMs` setting must still arm a
 * 7_200_000 ms wall-clock timer, so a provider stream hang eventually aborts
 * with a runtime-limit reason instead of waiting forever (`Unlimited` is now
 * opt-in via `0`).
 */

interface HangingSessionHandle {
	session: AgentSession;
	abortCalls: () => number;
}

function createHangingSession(): HangingSessionHandle {
	let abortCount = 0;
	const { promise: hang, resolve: releaseHang } = Promise.withResolvers<void>();
	const session: Partial<AgentSession> = {
		...createSessionDefaults(),
		state: { messages: [] } as never,
		agent: { state: { systemPrompt: ["test"] } } as never,
		extensionRunner: undefined as never,
		sessionManager: {
			appendSessionInit: () => {},
		} as never,
		getActiveToolNames: () => ["read", "yield"],
		getEnabledToolNames: () => ["read", "yield"],
		subscribe: (_listener: (event: AgentSessionEvent) => void) => () => {},
		prompt: async () => {
			await hang;
			return true;
		},
		waitForIdle: async () => {
			await hang;
		},
		abort: async () => {
			abortCount += 1;
			releaseHang();
		},
	};
	return {
		session: session as AgentSession,
		abortCalls: () => abortCount,
	};
}

function mockCreateAgentSession(session: AgentSession) {
	return vi.spyOn(sdkModule, "createAgentSession").mockResolvedValue({
		session,
		extensionsResult: {} as unknown as LoadExtensionsResult,
		setToolUIContext: () => {},
		eventBus: new EventBus(),
	} satisfies CreateAgentSessionResult);
}

describe("runSubprocess default runtime limit (task.maxRuntimeMs)", () => {
	afterEach(() => {
		vi.restoreAllMocks();
		AgentRegistry.resetGlobalForTests();
	});

	const baseAgent: AgentDefinition = {
		name: "task",
		description: "test",
		systemPrompt: "test",
		source: "bundled",
	};

	const baseOptions = {
		cwd: "/tmp",
		agent: baseAgent,
		task: "do work",
		index: 0,
		id: "subagent-default-runtime",
		modelRegistry: { refresh: async () => {} } as unknown as ModelRegistry,
		enableLsp: false,
	};

	it("arms the 2-hour default when the settings omit task.maxRuntimeMs", async () => {
		const settings = Settings.isolated();
		const handle = createHangingSession();
		mockCreateAgentSession(handle.session);

		const realSetTimeout = globalThis.setTimeout;
		let runtimeHandler: (() => void) | undefined;
		const timerSpy = vi.spyOn(globalThis, "setTimeout").mockImplementation(((
			handler: () => void,
			ms?: number,
			...rest: unknown[]
		) => {
			if (ms === 7_200_000) runtimeHandler = handler;
			return realSetTimeout(handler, ms, ...rest);
		}) as typeof globalThis.setTimeout);

		const run = runSubprocess({ ...baseOptions, id: "subagent-default-limit", settings });
		try {
			// The wall-clock timer is armed during monitor creation, before the
			// session ever prompts. Wait for it, then fire it manually so the test
			// does not depend on the real 2-hour delay.
			for (let i = 0; i < 200 && runtimeHandler === undefined; i++) await Bun.sleep(5);
			expect(runtimeHandler).toBeDefined();
			runtimeHandler?.();
			const result = await run;
			expect(result.aborted).toBe(true);
			expect(result.abortReason).toContain("task.maxRuntimeMs=7200000");
		} finally {
			timerSpy.mockRestore();
			// Safety: never leave the hanging child (and its run promise) alive if
			// the assertion above threw before the timer fired.
			void handle.session.abort();
		}
	});

	it("stops the child at the configured cap when task.maxRuntimeMs=50", async () => {
		const settings = Settings.isolated({ "task.maxRuntimeMs": 50 });
		const handle = createHangingSession();
		mockCreateAgentSession(handle.session);

		const result = await runSubprocess({ ...baseOptions, id: "subagent-explicit-limit", settings });

		expect(result.aborted).toBe(true);
		expect(result.exitCode).toBe(1);
		expect(result.abortReason).toContain("task.maxRuntimeMs=50");
		expect(handle.abortCalls()).toBeGreaterThanOrEqual(1);
	});
});
