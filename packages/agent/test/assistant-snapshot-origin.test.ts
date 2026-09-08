import { describe, expect, test } from "bun:test";
import type { AssistantMessage, Message } from "@oh-my-pi/pi-ai";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { agentLoop, assistantSnapshotOrigin } from "../src/agent-loop";
import type { AgentContext, AgentLoopConfig } from "../src/types";
import { createAssistantMessage, createUserMessage } from "./helpers";

async function runInvocation(providerMessage: AssistantMessage, register: (message: AssistantMessage) => void) {
	const context: AgentContext = { systemPrompt: [], messages: [], tools: [] };
	const config: AgentLoopConfig = {
		model: createMockModel().model,
		convertToLlm: messages =>
			messages.filter(
				message => message.role === "user" || message.role === "assistant" || message.role === "toolResult",
			) as Message[],
		transformAssistantMessage: message => {
			register(message);
			for (const part of message.content) {
				if (part.type === "text") part.text = part.text.toUpperCase();
			}
		},
	};
	const stream = agentLoop([createUserMessage("input")], context, config, undefined, () => {
		const response = new AssistantMessageEventStream();
		response.push({ type: "done", reason: "stop", message: providerMessage });
		response.end();
		return response;
	});
	const snapshots: AssistantMessage[] = [];
	for await (const event of stream) {
		if ((event.type === "message_start" || event.type === "message_end") && event.message.role === "assistant") {
			snapshots.push(event.message);
		}
	}
	const final = (await stream.result()).find((message): message is AssistantMessage => message.role === "assistant");
	if (!final) throw new Error("Actual agent loop returned no assistant message");
	return { snapshots, final };
}

describe("assistant snapshot authorization provenance", () => {
	test("core descendants retain granted invocation rights but copied message bodies cannot acquire them", async () => {
		const rights = new WeakSet<AssistantMessage>();
		const raw = createAssistantMessage([{ type: "text", text: "provider text" }]);
		const result = await runInvocation(raw, message => {
			const origin = assistantSnapshotOrigin(message);
			if (!origin) throw new Error("Core transformation received an unregistered assistant");
			rights.add(origin);
		});
		const effects: string[] = [];
		const consume = (message: AssistantMessage): boolean => {
			const origin = assistantSnapshotOrigin(message);
			if (!origin || !rights.has(origin)) return false;
			effects.push(
				message.content
					.filter(part => part.type === "text")
					.map(part => part.text)
					.join(""),
			);
			return true;
		};
		// Start, end and returned context are independently copied core views.
		expect(result.snapshots).toHaveLength(2);
		for (const message of [...result.snapshots, result.final]) expect(consume(message)).toBe(true);
		expect(effects).toEqual(["PROVIDER TEXT", "PROVIDER TEXT", "PROVIDER TEXT"]);
		const shallow = { ...result.final };
		const serialized = JSON.parse(JSON.stringify(result.final)) as AssistantMessage;
		expect(consume(shallow)).toBe(false);
		expect(consume(serialized)).toBe(false);
		expect(consume(raw)).toBe(false);
		expect(effects).toHaveLength(3);
	});

	test("reusing the exact provider object cannot reuse an earlier invocation's grant", async () => {
		const grants = new WeakMap<AssistantMessage, string>();
		const raw = createAssistantMessage([{ type: "text", text: "same provider object" }]);
		const first = await runInvocation(raw, message => {
			const origin = assistantSnapshotOrigin(message);
			if (!origin) throw new Error("First invocation lacks core origin");
			grants.set(origin, "first invocation");
		});
		const second = await runInvocation(raw, message => {
			const origin = assistantSnapshotOrigin(message);
			if (!origin) throw new Error("Second invocation lacks core origin");
			grants.set(origin, "second invocation");
		});
		const consume = (message: AssistantMessage, grant: string): boolean => {
			const origin = assistantSnapshotOrigin(message);
			return origin !== undefined && grants.get(origin) === grant;
		};
		// Equal content is insufficient: authorization must follow this request.
		expect(first.final.content).toEqual(second.final.content);
		expect(consume(first.final, "first invocation")).toBe(true);
		expect(consume(second.final, "first invocation")).toBe(false);
		expect(consume(first.final, "second invocation")).toBe(false);
		expect(consume(second.final, "second invocation")).toBe(true);
	});
});
