/**
 * Dispatch-time authority checks for hidden `deliverAs: "nextTurn"` extension
 * messages (S2.5). A queued hidden message may carry a `validateDispatch`
 * re-check: refused or thrown before the message can own a turn (no provider
 * request, no session entry), and re-checked once more at the turn's first
 * model call so a validator that flips or throws after the drain still refuses.
 */
import { afterEach, describe, expect, it } from "bun:test";
import { Agent } from "@oh-my-pi/pi-agent-core";
import { createMockModel, type MockCall } from "@oh-my-pi/pi-ai/providers/mock";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { convertToLlm } from "@oh-my-pi/pi-coding-agent/session/messages";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";

function contextIncludesText(call: MockCall, text: string): boolean {
	return call.context.messages.some(message => {
		if (typeof message.content === "string") return message.content.includes(text);
		return message.content.some(block => block.type === "text" && block.text.includes(text));
	});
}

describe("AgentSession hidden next-turn dispatch authority", () => {
	let session: AgentSession | undefined;
	let authStorage: AuthStorage | undefined;

	afterEach(async () => {
		if (session) await session.dispose();
		session = undefined;
		authStorage?.close();
		authStorage = undefined;
	});

	async function createSession(extensionRunner?: ExtensionRunner) {
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		let providerCalls = 0;
		const started = Promise.withResolvers<void>();
		const gate = Promise.withResolvers<void>();
		const mock = createMockModel({
			handler: async () => {
				providerCalls++;
				started.resolve();
				await gate.promise;
				return { content: ["Done"] };
			},
		});
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [], messages: [] },
			convertToLlm,
			streamFn: mock.stream,
		});
		authStorage = await AuthStorage.create(":memory:");
		authStorage.keys.setRuntime("anthropic", "test-key");
		session = new AgentSession({
			agent,
			sessionManager: SessionManager.inMemory(),
			settings: Settings.isolated({ "compaction.enabled": false }),
			modelRegistry: new ModelRegistry(authStorage),
			extensionRunner,
		});
		return {
			session,
			mock,
			started: started.promise,
			release: () => gate.resolve(),
			get providerCalls() {
				return providerCalls;
			},
		};
	}

	function customEntries(customType: string) {
		if (!session) throw new Error("session not created");
		return session.sessionManager
			.getEntries()
			.filter(entry => entry.type === "custom_message" && "customType" in entry && entry.customType === customType);
	}

	it("drops a streaming-queued hidden message whose validator refuses", async () => {
		const harness = await createSession();
		const refusal = Promise.withResolvers<{ ok: true } | { ok: false; reason: string }>();
		const promptTask = harness.session.prompt("hello");
		await harness.started;

		await harness.session.sendCustomMessage(
			{ customType: "hidden-refused", content: "hidden-note-refused", display: false, attribution: "agent" },
			{ deliverAs: "nextTurn", triggerTurn: true, validateDispatch: () => refusal.promise },
		);
		refusal.resolve({ ok: false, reason: "authority revoked" });

		harness.release();
		await promptTask;
		await harness.session.waitForIdle();

		expect(harness.providerCalls).toBe(1);
		expect(harness.mock.calls).toHaveLength(1);
		expect(customEntries("hidden-refused")).toHaveLength(0);
	});

	it("delivers a streaming-queued hidden message whose validator allows", async () => {
		const harness = await createSession();
		const promptTask = harness.session.prompt("hello");
		await harness.started;

		await harness.session.sendCustomMessage(
			{ customType: "hidden-admitted", content: "hidden-note-admitted", display: false, attribution: "agent" },
			{ deliverAs: "nextTurn", triggerTurn: true, validateDispatch: async () => ({ ok: true }) },
		);

		harness.release();
		await promptTask;
		await harness.session.waitForIdle();

		// Exactly one further provider call, and it carries the hidden message.
		expect(harness.mock.calls).toHaveLength(2);
		expect(contextIncludesText(harness.mock.calls[1]!, "hidden-note-admitted")).toBe(true);
	});

	it("refuses the turn when the validator flips inside a before_agent_start handler", async () => {
		let queued = false;
		let refuse = false;
		const emitBeforeAgentStart = async () => {
			if (queued) refuse = true;
			return undefined;
		};
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			hasHandlers: () => false,
			emitBeforeAgentStart,
			emit: async () => undefined,
		} as unknown as ExtensionRunner;

		const harness = await createSession(extensionRunner);
		const promptTask = harness.session.prompt("hello");
		await harness.started;

		// Queued while streaming: the pre-turn admission sees `refuse === false`,
		// so only the first-model-call gate can stop the continuation turn.
		await harness.session.sendCustomMessage(
			{ customType: "hidden-flip", content: "hidden-note-flip", display: false, attribution: "agent" },
			{
				deliverAs: "nextTurn",
				triggerTurn: true,
				validateDispatch: async () => (refuse ? { ok: false, reason: "flipped" } : { ok: true }),
			},
		);
		queued = true;

		harness.release();
		await promptTask;
		await harness.session.waitForIdle();

		expect(harness.providerCalls).toBe(1);
		expect(harness.mock.calls).toHaveLength(1);
	});

	it("refuses the turn when the re-check validator throws after before_agent_start", async () => {
		let queued = false;
		let throwOnRecheck = false;
		let validations = 0;
		const emitBeforeAgentStart = async () => {
			if (queued) throwOnRecheck = true;
			return undefined;
		};
		const extensionRunner = {
			setTaskResultProcessingGate: () => {},
			hasHandlers: () => false,
			emitBeforeAgentStart,
			emit: async () => undefined,
		} as unknown as ExtensionRunner;

		const harness = await createSession(extensionRunner);
		const promptTask = harness.session.prompt("hello");
		await harness.started;

		// Admitted while streaming (first validation returns ok). The continuation
		// turn's before_agent_start then arms the throw, so only the pre-model
		// re-check can stop that turn.
		await harness.session.sendCustomMessage(
			{ customType: "hidden-recheck-throw", content: "hidden-note-throw", display: false, attribution: "agent" },
			{
				deliverAs: "nextTurn",
				triggerTurn: true,
				validateDispatch: async () => {
					validations++;
					if (throwOnRecheck) throw new Error("recheck failed");
					return { ok: true };
				},
			},
		);
		queued = true;

		harness.release();
		await promptTask;
		await harness.session.waitForIdle();

		expect(validations).toBe(2);
		expect(harness.providerCalls).toBe(1);
		expect(harness.mock.calls).toHaveLength(1);
	});

	it("refuses an idle-triggered hidden message without a provider call", async () => {
		const harness = await createSession();

		await harness.session.sendCustomMessage(
			{ customType: "hidden-idle-refused", content: "hidden-idle-refused", display: false, attribution: "agent" },
			{ deliverAs: "nextTurn", triggerTurn: true, validateDispatch: async () => ({ ok: false, reason: "revoked" }) },
		);
		await harness.session.waitForIdle();

		expect(harness.providerCalls).toBe(0);
		expect(harness.mock.calls).toHaveLength(0);
		expect(customEntries("hidden-idle-refused")).toHaveLength(0);
	});

	it("refuses an idle-triggered hidden message when the validator throws", async () => {
		const harness = await createSession();

		await harness.session.sendCustomMessage(
			{ customType: "hidden-idle-threw", content: "hidden-idle-threw", display: false, attribution: "agent" },
			{
				deliverAs: "nextTurn",
				triggerTurn: true,
				validateDispatch: async () => {
					throw new Error("authority lookup failed");
				},
			},
		);
		await harness.session.waitForIdle();

		expect(harness.providerCalls).toBe(0);
		expect(harness.mock.calls).toHaveLength(0);
		expect(customEntries("hidden-idle-threw")).toHaveLength(0);
	});
});
