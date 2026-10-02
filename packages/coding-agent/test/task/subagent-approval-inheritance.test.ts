/**
 * L-RT-02: a child inherits the parent's approval mode. A full-power parent
 * (SDK/CLI auto-approve) still gives the child yolo and autoApprove.
 * The child stays headless (`hasUI: false`); a tool the inherited mode does
 * not approve is refused by the existing no-UI path.
 */
import { afterEach, describe, expect, it, vi } from "bun:test";
import type { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import type { LoadExtensionsResult } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { AgentRegistry } from "@oh-my-pi/pi-coding-agent/registry/agent-registry";
import type { CreateAgentSessionOptions, CreateAgentSessionResult } from "@oh-my-pi/pi-coding-agent/sdk";
import * as sdkModule from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession, AgentSessionEvent, PromptOptions } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { createSubagentSettings, runSubprocess } from "@oh-my-pi/pi-coding-agent/task/executor";
import type { AgentDefinition } from "@oh-my-pi/pi-coding-agent/task/types";
import { cfgToolsApprovalMode } from "@oh-my-pi/pi-coding-agent/tools/settings";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import { createSessionDefaults } from "../helpers/session-defaults";

type ApprovalMode = "always-ask" | "write" | "yolo";

function yieldEmittingSession(): AgentSession {
	const listeners: Array<(event: AgentSessionEvent) => void> = [];
	const emit = (event: AgentSessionEvent) => {
		for (const listener of listeners) listener(event);
	};
	const session = {
		...createSessionDefaults(),
		state: { messages: [] },
		agent: { state: { systemPrompt: ["test"] } },
		model: undefined,
		extensionRunner: undefined,
		sessionManager: { appendSessionInit: () => {} },
		getActiveToolNames: () => ["read", "yield"],
		getEnabledToolNames: () => ["read", "yield"],
		subscribe: (listener: (event: AgentSessionEvent) => void) => {
			listeners.push(listener);
			return () => {
				const index = listeners.indexOf(listener);
				if (index >= 0) listeners.splice(index, 1);
			};
		},
		prompt: async (_text: string, _options?: PromptOptions) => {
			emit({
				type: "tool_execution_end",
				toolCallId: "tool-approval",
				toolName: "yield",
				result: {
					content: [{ type: "text", text: "Result submitted." }],
					details: { status: "success", data: { ok: true } },
				},
				isError: false,
			});
			return true;
		},
	};
	return session as unknown as AgentSession;
}

function createSessionResult(session: AgentSession): CreateAgentSessionResult {
	return {
		session,
		extensionsResult: { extensions: [], errors: [], runtime: {} as unknown } as unknown as LoadExtensionsResult,
		setToolUIContext: () => {},
		eventBus: new EventBus(),
	};
}

const baseAgent: AgentDefinition = {
	name: "task",
	description: "test",
	systemPrompt: "test",
	source: "bundled",
};

describe("createSubagentSettings approval inheritance (L-RT-02)", () => {
	it.each(["write", "always-ask", "yolo"] as const)(
		"parent %s gives the child the same mode",
		(mode: ApprovalMode) => {
			const parent = Settings.isolated({ "tools.approvalMode": mode });
			expect(cfgToolsApprovalMode.get(createSubagentSettings(parent))).toBe(mode);
		},
	);
});

describe("runSubprocess approval inheritance (L-RT-02)", () => {
	afterEach(() => {
		vi.restoreAllMocks();
		AgentRegistry.resetGlobalForTests();
	});

	async function spawn(
		id: string,
		parentAutoApprove?: boolean,
	): Promise<CreateAgentSessionOptions & { settings: Settings }> {
		const spy = vi
			.spyOn(sdkModule, "createAgentSession")
			.mockResolvedValue(createSessionResult(yieldEmittingSession()));
		const result = await runSubprocess({
			cwd: "/tmp",
			agent: baseAgent,
			task: "do work",
			index: 0,
			id,
			settings: Settings.isolated({ "tools.approvalMode": "write" }),
			modelRegistry: { refresh: async () => {} } as unknown as ModelRegistry,
			enableLsp: false,
			...(parentAutoApprove === undefined ? {} : { parentAutoApprove }),
		});
		expect(result.exitCode).toBe(0);
		expect(spy).toHaveBeenCalledTimes(1);
		const options = spy.mock.calls[0]?.[0];
		if (!options?.settings) throw new Error("createAgentSession was called without settings");
		return options as CreateAgentSessionOptions & { settings: NonNullable<CreateAgentSessionOptions["settings"]> };
	}

	it("gives a full-power parent yolo and autoApprove, and stays headless", async () => {
		const options = await spawn("subagent-approval-full-power", true);
		expect(options.autoApprove).toBe(true);
		expect(options.hasUI).toBe(false);
		expect(cfgToolsApprovalMode.get(options.settings)).toBe("yolo");
	});

	it("keeps the parent write mode and does not set autoApprove when the parent is not full power", async () => {
		const options = await spawn("subagent-approval-inherit");
		expect(options.autoApprove).not.toBe(true);
		expect(options.hasUI).toBe(false);
		expect(cfgToolsApprovalMode.get(options.settings)).toBe("write");
	});

	it("treats an explicit false the same as an omitted parentAutoApprove", async () => {
		const options = await spawn("subagent-approval-explicit-false", false);
		expect(options.autoApprove).not.toBe(true);
		expect(options.hasUI).toBe(false);
		expect(cfgToolsApprovalMode.get(options.settings)).toBe("write");
	});
});
