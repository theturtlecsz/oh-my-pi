import { afterEach, describe, expect, spyOn, test } from "bun:test";
import * as path from "node:path";
import { Agent } from "@oh-my-pi/pi-agent-core";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { TempDir } from "@oh-my-pi/pi-utils";
import { ModelRegistry } from "../src/config/model-registry";
import { Settings } from "../src/config/settings";
import { ExtensionRuntime, loadExtensionFromFactory } from "../src/extensibility/extensions/loader";
import { ExtensionRunner } from "../src/extensibility/extensions/runner";
import { initializeExtensions } from "../src/modes/runtime-init";
import { AgentSession } from "../src/session/agent-session";
import type { PersistedTurnRefusal } from "../src/session/agent-session-types";
import { convertToLlm } from "../src/session/messages";
import { SessionManager } from "../src/session/session-manager";
import { type PersistedTaskBindingV1, taskRecoveryHash } from "../src/task/recovery";
import { EventBus } from "../src/utils/event-bus";
import { createAssistantMessage, createInMemoryAuthStorage } from "./helpers/agent-session-setup";

type RevocationBoundary =
	| "after-validation"
	| "during-message-end"
	| "loaded-policy"
	| "owner-input"
	| "result-persistence-failure";
let cleanup: (() => Promise<void>) | undefined;
const restoreSpies: Array<() => void> = [];

afterEach(async () => {
	for (const restore of restoreSpies.splice(0)) restore();
	await cleanup?.();
	cleanup = undefined;
});

async function createRecoveryFixture(boundary: RevocationBoundary) {
	const temp = TempDir.createSync("task-result-guards-");
	const auth = createInMemoryAuthStorage();
	auth.setRuntimeApiKey("anthropic", "test-key");
	const manager = SessionManager.create(temp.path(), temp.path());
	const anchor = manager.appendCustomMessageEntry("execution-test", "Original assignment", false, {}, "agent");
	manager.appendCustomEntry("prompt-preparation", {
		version: 1,
		sessionId: manager.getSessionId(),
		anchorEntryId: anchor,
		batchId: "origin",
		preparationEntryIds: [],
	});
	const args = { name: "Child", agent: "task", task: "inspect" };
	const model = getBundledModel("anthropic", "claude-sonnet-4-5");
	if (!model) throw new Error("Required fixture model unavailable");
	const assistant = manager.appendMessage({
		...createAssistantMessage(""),
		content: [{ type: "toolCall", id: "original-call", name: "task", arguments: args }],
		api: model.api,
		provider: model.provider,
		model: model.id,
		stopReason: "toolUse",
	});
	const contract: PersistedTaskBindingV1["contract"] = {
		agent: { name: "task", description: "", systemPrompt: "", source: "bundled" },
		args,
		assignment: "inspect",
		initialization: { systemPrompt: "", task: "inspect", tools: [] },
		policy: {
			async: false,
			batch: false,
			isolation: "none",
			parentDepth: 0,
			parentSpawns: "*",
			maxDepth: 1,
			disabledAgents: [],
			approval: {},
			approvalMode: "yolo",
			modelOverride: {},
			executionPolicy: {},
		},
		runtime: {
			model: { provider: "fixture", api: "openai-completions", id: "child" },
			thinking: "off",
			tiers: {},
			tools: [],
		},
	};
	const binding: PersistedTaskBindingV1 = {
		version: 1,
		mode: "sync-flat",
		call: {
			bindingId: "binding",
			sessionId: manager.getSessionId(),
			promptEntryId: anchor,
			assistantEntryId: assistant,
			toolCallId: "original-call",
			argumentsSha256: taskRecoveryHash(args),
		},
		child: {
			registryId: "Child",
			sessionId: "child",
			sessionFile: path.join(temp.path(), "Child.jsonl"),
			initEntryId: "init",
			cwd: temp.path(),
		},
		contract,
		contractSha256: taskRecoveryHash(contract),
	};
	manager.appendCustomEntry("task-run-binding", binding);
	await manager.ensureOnDisk();
	await manager.flush();
	const runtime = new ExtensionRuntime();
	const messageEndReached = Promise.withResolvers<void>();
	const releaseMessageEnd = Promise.withResolvers<void>();
	const extension = await loadExtensionFromFactory(
		pi => {
			if (boundary === "during-message-end" || boundary === "owner-input") {
				pi.on("message_end", async event => {
					if (event.message.role === "toolResult" && event.message.toolCallId === "original-call") {
						messageEndReached.resolve();
						await releaseMessageEnd.promise;
					}
				});
			}
		},
		temp.path(),
		new EventBus(),
		runtime,
		"task-result-guard",
	);
	const registry = new ModelRegistry(auth, path.join(temp.path(), "models.yml"));
	const runner = new ExtensionRunner([extension], runtime, temp.path(), manager, registry);
	const provider = createMockModel({ responses: [{ content: ["Parent answer"] }] });
	const agent = new Agent({
		getApiKey: () => "test-key",
		initialState: { model, systemPrompt: ["Test"], tools: [], messages: manager.buildSessionContext().messages },
		streamFn: provider.stream,
		convertToLlm,
	});
	let nativeCompletions = 0;
	const session = new AgentSession({
		agent,
		sessionManager: manager,
		modelRegistry: registry,
		extensionRunner: runner,
		settings: Settings.isolated({ "compaction.enabled": false, "retry.enabled": false }),
		agentId: "Main",
		validateSynchronousTaskPolicy: async () => {},
		// Component boundary only: substitute native completion, while actual
		// AgentSession/ExtensionRunner/SessionManager handle result commit guards.
		// This does not exercise a real child and is not installed qualification.
		recoverSynchronousTask: async request => {
			nativeCompletions++;
			return {
				binding: request.binding,
				result: {
					content: [{ type: "text", text: "Component native completion" }],
					details: { results: [], projectAgentsDir: null, totalDurationMs: 0 },
				},
			};
		},
	});
	cleanup = async () => {
		releaseMessageEnd.resolve();
		await session.dispose();
		auth.close();
		temp.removeSync();
	};
	await initializeExtensions(session, { reportSendError: () => {}, reportRuntimeError: () => {} });
	return {
		session,
		manager,
		provider,
		anchor,
		messageEndReached,
		releaseMessageEnd,
		nativeCompletions: () => nativeCompletions,
	};
}

describe("task recovery result commit guards", () => {
	test.each([
		"after-validation",
		"during-message-end",
		"loaded-policy",
		"owner-input",
		"result-persistence-failure",
	] as const)("refuses result and provider dispatch at %s", async boundary => {
		const f = await createRecoveryFixture(boundary);
		let checks = 0;
		let revoked = false;
		let aborting: Promise<void> | undefined;
		const refusals: PersistedTurnRefusal[] = [];
		if (boundary === "result-persistence-failure") {
			const append = f.manager.appendMessage.bind(f.manager);
			const appendSpy = spyOn(f.manager, "appendMessage").mockImplementation((message, taskResult) => {
				if (message.role === "toolResult" && message.toolCallId === "original-call") {
					revoked = true;
					throw new Error("Original task result persistence failed");
				}
				return append(message, taskResult);
			});
			restoreSpies.push(() => appendSpy.mockRestore());
		}
		const outcome = f.session.requestPersistedTurnContinuation({
			sessionId: f.session.sessionId,
			entryId: f.anchor,
			expectedLeafId: f.manager.getLeafId()!,
			recoverSynchronousTask: true,
			onRefused: refusal => refusals.push(refusal),
			validateDispatch: async () => {
				checks++;
				// First check admits recovery; second checks returned native result.
				// Revoke after that validator resolves, before the commit boundary.
				if (checks === 2 && (boundary === "after-validation" || boundary === "loaded-policy")) {
					queueMicrotask(() =>
						queueMicrotask(() => {
							revoked = true;
							if (boundary === "loaded-policy") f.session.settings.set("tools.approval", { task: "deny" });
							else aborting = f.session.abort();
						}),
					);
				}
				return { ok: true };
			},
		});
		expect(outcome.status).toBe("scheduled");
		if (boundary === "during-message-end" || boundary === "owner-input") {
			await f.messageEndReached.promise;
			// Public owner input itself revokes recovery. Test its queue separately
			// from an explicit abort, which has its own interruption semantics.
			if (boundary === "owner-input") {
				await f.session.sendUserMessage("Owner correction", { deliverAs: "followUp" });
				expect(f.session.getQueuedMessages().followUp).toEqual(["Owner correction"]);
			} else aborting = f.session.abort();
			revoked = true;
			f.releaseMessageEnd.resolve();
		}
		await f.session.waitForIdle();
		await aborting;
		await f.manager.flush();
		expect(revoked).toBe(true);
		expect(f.nativeCompletions()).toBe(1);
		expect(refusals).toHaveLength(1);
		expect(refusals[0]?.code).toBe("dispatch-failed");
		if (boundary === "loaded-policy")
			expect(refusals[0]?.reason).toContain("Loaded parent approval/spawn policy changed");
		if (boundary === "result-persistence-failure")
			expect(refusals[0]?.reason).toContain("Original task result persistence failed");
		expect(f.provider.calls).toHaveLength(0);
		expect(
			f.manager
				.getEntries()
				.filter(
					entry =>
						entry.type === "message" &&
						entry.message.role === "toolResult" &&
						entry.message.toolCallId === "original-call",
				),
		).toHaveLength(0);
		const file = f.manager.getSessionFile();
		if (!file) throw new Error("Guard fixture has no durable session file");
		const reloaded = await SessionManager.open(file, undefined, undefined, { suppressBreadcrumb: true });
		try {
			expect(
				reloaded
					.getEntries()
					.filter(
						entry =>
							entry.type === "message" &&
							entry.message.role === "toolResult" &&
							entry.message.toolCallId === "original-call",
					),
			).toHaveLength(0);
		} finally {
			await reloaded.close();
		}
		expect(
			f.session.messages.filter(message => message.role === "toolResult" && message.toolCallId === "original-call"),
		).toHaveLength(0);
		if (boundary === "owner-input") {
			expect(f.session.getQueuedMessages()).toEqual({ steering: [], followUp: ["Owner correction"] });
		}
	});
});
