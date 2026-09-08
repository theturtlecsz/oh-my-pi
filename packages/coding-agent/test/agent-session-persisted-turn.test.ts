import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as path from "node:path";
import { Agent } from "@oh-my-pi/pi-agent-core";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { ExtensionRuntime, loadExtensionFromFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { runPrintMode } from "@oh-my-pi/pi-coding-agent/modes/print-mode";
import { initializeExtensions } from "@oh-my-pi/pi-coding-agent/modes/runtime-init";
import {
	AgentSession,
	type PersistedTurnContinuationRequest,
	type PersistedTurnRefusal,
} from "@oh-my-pi/pi-coding-agent/session/agent-session";
import type { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { TOOL_EXECUTION_START_CUSTOM_TYPE } from "@oh-my-pi/pi-coding-agent/session/exit-diagnostics";
import { convertToLlm } from "@oh-my-pi/pi-coding-agent/session/messages";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import { TempDir } from "@oh-my-pi/pi-utils";
import { createInMemoryAuthStorage } from "./helpers/agent-session-setup";

describe("persisted no-tool turn continuation", () => {
	let tempDir: TempDir;
	let auth: AuthStorage;
	let session: AgentSession | undefined;

	beforeEach(() => {
		tempDir = TempDir.createSync("omp-persisted-turn-");
		auth = createInMemoryAuthStorage();
		auth.setRuntimeApiKey("anthropic", "test-key");
	});
	afterEach(async () => {
		vi.restoreAllMocks();
		await session?.dispose();
		auth.close();
		tempDir.removeSync();
	});

	async function setup(register?: (pi: ExtensionAPI) => void) {
		const original = SessionManager.create(tempDir.path(), tempDir.path());
		const entryId = original.appendCustomMessageEntry(
			"execution-test",
			"Resume original assignment",
			false,
			{ identity: "original" },
			"agent",
		);
		await original.ensureOnDisk();
		await original.flush();
		const file = original.getSessionFile()!;
		await original.close();
		const manager = await SessionManager.open(file);
		const registry = new ModelRegistry(auth, path.join(tempDir.path(), "models.yml"));
		const runtime = new ExtensionRuntime();
		const extension = await loadExtensionFromFactory(
			pi => register?.(pi),
			tempDir.path(),
			new EventBus(),
			runtime,
			"persisted-test",
		);
		const runner = new ExtensionRunner([extension], runtime, tempDir.path(), manager, registry);
		const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
		const provider = createMockModel({ responses: [{ content: ["Recovered answer"] }] });
		const agent = new Agent({
			getApiKey: () => "test-key",
			initialState: { model, systemPrompt: ["Test"], tools: [], messages: manager.buildSessionContext().messages },
			streamFn: provider.stream,
			convertToLlm,
		});
		const current = new AgentSession({
			agent,
			sessionManager: manager,
			modelRegistry: registry,
			extensionRunner: runner,
			settings: Settings.isolated({ "compaction.enabled": false, "retry.enabled": false }),
			agentId: "Main",
		});
		session = current;
		const refusals: PersistedTurnRefusal[] = [];
		const request: PersistedTurnContinuationRequest = {
			sessionId: current.sessionId,
			entryId,
			expectedLeafId: manager.getLeafId()!,
			validateDispatch: async () => ({ ok: true }),
			onRefused: refusal => refusals.push(refusal),
		};
		const start = () => initializeExtensions(current, { reportSendError: () => {}, reportRuntimeError: () => {} });
		return { current, manager, provider, request, refusals, start, runtime };
	}

	it("coalesces one restored entry, waits for real startup, includes preparation notices, and persists one answer", async () => {
		const held = Promise.withResolvers<void>();
		const reached = Promise.withResolvers<void>();
		let request!: PersistedTurnContinuationRequest;
		let accepted: unknown;
		const fixture = await setup(pi => {
			pi.on("session_start", () => {
				accepted = pi.requestPersistedTurnContinuation(request);
			});
			pi.on("session_start", async () => {
				reached.resolve();
				await held.promise;
			});
			pi.on("before_agent_start", () => ({
				message: { customType: "required-context", content: "Prepared recovery context", display: false },
			}));
		});
		request = fixture.request;
		const starting = fixture.start();
		await reached.promise;
		expect(accepted).toEqual({ status: "scheduled" });
		expect(fixture.current.requestPersistedTurnContinuation(request)).toEqual({ status: "alreadyScheduled" });
		expect(fixture.current.requestPersistedTurnContinuation({ ...request, entryId: "different" })).toMatchObject({
			status: "refused",
			code: "conflicting-request",
		});
		expect(fixture.provider.calls).toHaveLength(0);
		held.resolve();
		await starting;
		await fixture.current.waitForIdle();
		await fixture.manager.flush();
		expect(fixture.refusals).toEqual([]);
		expect(fixture.provider.calls).toHaveLength(1);
		const input = JSON.stringify(fixture.provider.calls[0].context.messages);
		expect(input.match(/Resume original assignment/g)).toHaveLength(1);
		expect(input).toContain("Prepared recovery context");
		const reopened = await SessionManager.open(fixture.manager.getSessionFile()!);
		try {
			expect(
				reopened
					.getBranch()
					.filter(entry => entry.type === "custom_message" && entry.customType === "execution-test"),
			).toHaveLength(1);
			expect(
				reopened
					.getBranch()
					.filter(entry => entry.type === "message" && entry.message.role === "assistant")
					.map(entry => entry.type === "message" && entry.message.role === "assistant" && entry.message.content),
			).toEqual([[{ type: "text", text: "Recovered answer" }]]);
		} finally {
			await reopened.close();
		}
		expect(
			fixture.current.requestPersistedTurnContinuation({ ...request, expectedLeafId: fixture.manager.getLeafId()! }),
		).toMatchObject({ status: "refused", code: "turn-settled" });
		expect(fixture.provider.calls).toHaveLength(1);
	});

	it("fresh terminal authority refuses after preparation without provider dispatch", async () => {
		const f = await setup();
		await f.start();
		expect(
			f.current.requestPersistedTurnContinuation({
				...f.request,
				validateDispatch: async () => ({ ok: false, reason: "grant canceled" }),
			}),
		).toEqual({ status: "scheduled" });
		await f.current.waitForIdle();
		expect(f.refusals).toEqual([{ code: "authority-refused", reason: "grant canceled" }]);
		expect(f.provider.calls).toHaveLength(0);
	});

	it.each(["session", "leaf", "generation", "owner-input"] as const)(
		"refuses %s changes while authority validation awaits",
		async change => {
			const f = await setup();
			await f.start();
			const reached = Promise.withResolvers<void>();
			const release = Promise.withResolvers<void>();
			f.current.requestPersistedTurnContinuation({
				...f.request,
				validateDispatch: async () => {
					reached.resolve();
					await release.promise;
					return { ok: true };
				},
			});
			await reached.promise;
			if (change === "session") await f.manager.newSession();
			if (change === "leaf") f.manager.appendCustomEntry("late-metadata", {});
			if (change === "owner-input") await f.current.sendUserMessage("Owner takes over", { deliverAs: "followUp" });
			const aborting = change === "generation" ? f.current.abort() : undefined;
			release.resolve();
			await aborting;
			await f.current.waitForIdle();
			expect(f.refusals[0]?.code).toBe(
				change === "owner-input"
					? "queued-input"
					: change === "generation"
						? "session-unavailable"
						: "stale-identity",
			);
			expect(f.provider.calls).toHaveLength(0);
			if (change === "owner-input") expect(f.current.queuedMessageCount).toBe(1);
		},
	);

	it("retains an unresolved tool-start marker and refuses replay", async () => {
		const f = await setup();
		f.manager.appendCustomEntry(TOOL_EXECUTION_START_CUSTOM_TYPE, {
			toolCallId: "original-call",
			toolName: "task",
			args: { task: "original" },
			startedAt: Date.now(),
		});
		await f.start();
		const result = f.current.requestPersistedTurnContinuation({
			...f.request,
			expectedLeafId: f.manager.getLeafId()!,
		});
		expect(result).toMatchObject({ status: "refused", code: "pending-tools" });
		if (result.status === "refused") expect(result.reason).toContain("original-call");
		expect(f.provider.calls).toHaveLength(0);
	});

	it("flush failure refuses durability instead of treating mutable memory as persisted", async () => {
		const f = await setup();
		await f.start();
		vi.spyOn(f.manager, "flush").mockRejectedValueOnce(new Error("disk failure"));
		f.current.requestPersistedTurnContinuation(f.request);
		await f.current.waitForIdle();
		expect(f.refusals).toEqual([{ code: "persistence-failed", reason: "Error: disk failure" }]);
		expect(f.provider.calls).toHaveLength(0);
	});

	it("failed startup cannot authorize a recovered turn", async () => {
		const f = await setup(pi => {
			pi.on("session_start", () => {
				throw new Error("startup failed");
			});
		});
		await f.start();
		f.current.requestPersistedTurnContinuation(f.request);
		await f.current.waitForIdle();
		expect(f.refusals[0]?.code).toBe("startup-incomplete");
		expect(f.provider.calls).toHaveLength(0);
	});
	it.each(["missing", "reset", "owner-turn", "assignment"] as const)(
		"refuses %s branch conflicts without touching the transcript",
		async conflict => {
			const f = await setup();
			await f.start();
			if (conflict === "reset") f.manager.appendResetBoundary();
			if (conflict === "owner-turn")
				f.manager.appendMessage({ role: "user", content: "New owner turn", timestamp: Date.now() });
			if (conflict === "assignment")
				f.manager.appendCustomMessageEntry("execution-test", "Other assignment", false, {}, "agent");
			await f.manager.flush();
			const before = await Bun.file(f.manager.getSessionFile()!).text();
			const result = f.current.requestPersistedTurnContinuation({
				...f.request,
				entryId: conflict === "missing" ? "missing-entry" : f.request.entryId,
				expectedLeafId: f.manager.getLeafId()!,
			});
			expect(result).toMatchObject({
				status: "refused",
				code: conflict === "missing" ? "missing-anchor" : "unsafe-suffix",
			});
			expect(f.provider.calls).toHaveLength(0);
			expect(await Bun.file(f.manager.getSessionFile()!).text()).toBe(before);
		},
	);
	it("restores owner nextTurn input arriving inside preparation and makes no recovered request", async () => {
		const reached = Promise.withResolvers<void>();
		const release = Promise.withResolvers<void>();
		const f = await setup(pi =>
			pi.on("before_agent_start", async () => {
				reached.resolve();
				await release.promise;
			}),
		);
		await f.start();
		f.current.requestPersistedTurnContinuation(f.request);
		await reached.promise;
		await f.current.sendCustomMessage(
			{ customType: "owner-directive", content: "Owner takes over", display: true, attribution: "user" },
			{ deliverAs: "nextTurn" },
		);
		release.resolve();
		await f.current.waitForIdle();
		expect(f.provider.calls).toHaveLength(0);
		expect(f.refusals[0]?.code).toBe("queued-input");
		expect(f.current.queuedMessageCount).toBe(1);
	});
	it("captures request authority and identity before extension code can mutate them", async () => {
		const release = Promise.withResolvers<void>();
		const reached = Promise.withResolvers<void>();
		const f = await setup(pi =>
			pi.on("session_start", async () => {
				reached.resolve();
				await release.promise;
			}),
		);
		const starting = f.start();
		await reached.promise;
		const originalValidator = vi.fn(async (): Promise<{ ok: true }> => ({ ok: true }));
		f.request.validateDispatch = originalValidator;
		f.current.requestPersistedTurnContinuation(f.request);
		f.request.entryId = "retargeted-entry";
		f.request.validateDispatch = async () => ({ ok: false, reason: "replacement callback" });
		release.resolve();
		await starting;
		await f.current.waitForIdle();
		expect(originalValidator).toHaveBeenCalledTimes(1);
		expect(f.refusals).toEqual([]);
		expect(f.provider.calls).toHaveLength(1);
	});
	it.each(["before", "during"] as const)("respects deferred client ownership %s recovery validation", async when => {
		const f = await setup();
		await f.start();
		const bridge = { capabilities: {}, deferAgentInitiatedTurns: true };
		if (when === "before") f.current.setClientBridge(bridge);
		const result = f.current.requestPersistedTurnContinuation({
			...f.request,
			validateDispatch: async () => {
				if (when === "during") f.current.setClientBridge(bridge);
				return { ok: true };
			},
		});
		await f.current.waitForIdle();
		expect(f.provider.calls).toHaveLength(0);
		if (when === "before") expect(result).toMatchObject({ status: "refused", code: "unavailable" });
		else expect(f.refusals[0]?.code).toBe("unavailable");
	});

	it("abort after awaited validation cancels prepared-context dispatch synchronously", async () => {
		const f = await setup(pi =>
			pi.on("before_agent_start", () => ({
				message: { customType: "prep", content: "Prepared context", display: false },
			})),
		);
		await f.start();
		let aborting: Promise<void> | undefined;
		f.current.requestPersistedTurnContinuation({
			...f.request,
			validateDispatch: async () => {
				queueMicrotask(() =>
					queueMicrotask(() => {
						aborting = f.current.abort();
					}),
				);
				return { ok: true };
			},
		});
		await f.current.waitForIdle();
		await aborting;
		expect(f.provider.calls).toHaveLength(0);
		expect(f.refusals[0]?.code).toBe("session-unavailable");
	});

	it.each(["text", "json", "owner"] as const)(
		"print %s invocation settles owned work before output and disposal",
		async mode => {
			let request!: PersistedTurnContinuationRequest;
			let accepted: unknown;
			const f = await setup(pi =>
				pi.on("session_start", () => {
					accepted = pi.requestPersistedTurnContinuation(request);
				}),
			);
			request = f.request;
			const output: string[] = [];
			vi.spyOn(process.stdout, "write").mockImplementation((...args: unknown[]) => {
				output.push(String(args[0]));
				const callback = args.at(-1);
				if (typeof callback === "function") callback();
				return true;
			});
			await runPrintMode(f.current, {
				mode: mode === "json" ? "json" : "text",
				initialMessage: mode === "owner" ? "Explicit owner prompt" : undefined,
			});
			expect(f.provider.calls).toHaveLength(1);
			expect(f.refusals).toEqual([]);
			if (mode === "owner") {
				expect(accepted).toMatchObject({ status: "refused", code: "queued-input" });
				expect(JSON.stringify(f.provider.calls[0].context.messages)).toContain("Explicit owner prompt");
			} else expect(accepted).toEqual({ status: "scheduled" });
			if (mode === "json") {
				const events = output.map(line => JSON.parse(line));
				expect(events.some(event => event.type === "agent_end")).toBe(true);
				expect(
					events.some(
						event =>
							event.type === "message_end" &&
							event.message?.role === "assistant" &&
							event.message.content.some(
								(part: { type: string; text?: string }) => part.text === "Recovered answer",
							),
					),
				).toBe(true);
			} else expect(output.join("")).toContain("Recovered answer");
		},
	);
});
