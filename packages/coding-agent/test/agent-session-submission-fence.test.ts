/**
 * OMP-246 contract 1 at the worker seam: an accepted terminal `yield` is a
 * submission fence inside the same assistant response.
 *
 * The agent loop schedules exclusive tools (every write) behind the shared
 * batch, and the session's afterToolCall hook aborts the run with
 * TERMINAL_TOOL_RESULT_ABORT_REASON the moment a terminal yield result lands.
 * A write emitted after the yield therefore never starts: it is paired with a
 * synthetic "not executed" result and no further provider call is made. A
 * record before the yield executes once. Shared work admitted before the yield
 * settles keeps its real result. The control case removes the terminal marker
 * (an incremental yield) and shows the write executing and the loop
 * continuing, so the fence assertions above discriminate a missing fence.
 */
import { afterAll, afterEach, describe, expect, it, vi } from "bun:test";
import { type } from "@oh-my-pi/omptype";
import { Agent, type AgentTool } from "@oh-my-pi/pi-agent-core";
import { createMockModel, type MockModel, type MockResponse } from "@oh-my-pi/pi-ai/providers/mock";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { convertToLlm } from "@oh-my-pi/pi-coding-agent/session/messages";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { TempDir } from "@oh-my-pi/pi-utils";
import { createInMemoryAuthStorage } from "./helpers/agent-session-setup";

const yieldSchema = type({ result: type("unknown") });
const valueSchema = type({ value: type("string") });

type Harness = { session: AgentSession; tempDir: TempDir; mock: MockModel };
const activeHarnesses: Harness[] = [];
const sharedAuthStorage = createInMemoryAuthStorage();
sharedAuthStorage.setRuntimeApiKey("mock", "test-key");
const sharedModelRegistry = new ModelRegistry(sharedAuthStorage);

afterAll(() => {
	sharedAuthStorage.close();
});

afterEach(async () => {
	for (const harness of activeHarnesses.splice(0)) {
		await harness.session.dispose();
		harness.tempDir.removeSync();
	}
	vi.restoreAllMocks();
});

type YieldDetails = { status: "success"; data: unknown; type?: string[] };

/** Terminal submission: the shape the session's terminal-yield predicate accepts. */
function terminalYieldTool(
	options: { beforeReturn?: () => Promise<void> } = {},
): AgentTool<typeof yieldSchema, YieldDetails> {
	return {
		name: "yield",
		label: "Submit Result",
		description: "Finish the task with structured JSON output.",
		parameters: yieldSchema,
		async execute(_toolCallId, params) {
			await options.beforeReturn?.();
			return {
				content: [{ type: "text", text: "Result submitted." }],
				details: { status: "success", data: params.result },
			};
		},
	};
}

/** Known-bad control: an incremental section yield carries no terminal marker, so no fence is raised. */
function incrementalYieldTool(): AgentTool<typeof yieldSchema, YieldDetails> {
	return {
		name: "yield",
		label: "Submit Section",
		description: "Submit one labelled section of the output.",
		parameters: yieldSchema,
		async execute(_toolCallId, params) {
			return {
				content: [{ type: "text", text: "Section submitted." }],
				details: { status: "success", data: params.result, type: ["findings"] },
			};
		},
	};
}

/** A write: exclusive like every mutating tool, so it queues behind the shared batch. */
function recordTool(executions: string[]): AgentTool<typeof valueSchema, { value: string }> {
	return {
		name: "record",
		label: "Record",
		description: "Record a value (exclusive, like every write tool).",
		parameters: valueSchema,
		concurrency: "exclusive",
		async execute(_toolCallId, params) {
			executions.push(params.value);
			return { content: [{ type: "text", text: `recorded:${params.value}` }], details: { value: params.value } };
		},
	};
}

/** A shared read probe that is admitted before the yield lands and finishes only when released. */
function probeTool(
	executions: string[],
	gate: { started: PromiseWithResolvers<void>; release: PromiseWithResolvers<void> },
): AgentTool<typeof valueSchema, { value: string }> {
	return {
		name: "probe",
		label: "Probe",
		description: "Shared read-only probe.",
		parameters: valueSchema,
		concurrency: "shared",
		async execute(_toolCallId, params) {
			gate.started.resolve();
			await gate.release.promise;
			executions.push(params.value);
			return { content: [{ type: "text", text: `probe:${params.value}` }], details: { value: params.value } };
		},
	};
}

type ScriptedCall = { id: string; name: string; arguments: Record<string, unknown> };

function toolCalls(...calls: ScriptedCall[]): MockResponse {
	return {
		content: calls.map(call => ({
			type: "toolCall" as const,
			id: call.id,
			name: call.name,
			arguments: call.arguments,
		})),
		stopReason: "toolUse",
	};
}

const trailingStop = (text: string): MockResponse => ({ content: [text], stopReason: "stop" });
const yieldCall = (id: string): ScriptedCall => ({
	id,
	name: "yield",
	arguments: { result: { data: { done: true } } },
});
const recordCall = (id: string, value: string): ScriptedCall => ({ id, name: "record", arguments: { value } });
const probeCall = (id: string, value: string): ScriptedCall => ({ id, name: "probe", arguments: { value } });

async function createHarness(tools: AgentTool[], responses: MockResponse[]): Promise<Harness> {
	const tempDir = TempDir.createSync("@pi-submission-fence-");
	const mock = createMockModel({ responses });
	const settings = Settings.isolated({
		"compaction.enabled": false,
		"retry.enabled": false,
		"todo.enabled": false,
		"todo.eager": "default",
		"todo.reminders": false,
	});
	settings.setModelRole("default", `${mock.provider}/${mock.id}`);
	const agent = new Agent({
		getApiKey: () => "test-key",
		initialState: { model: mock, systemPrompt: ["Test"], tools, messages: [] },
		convertToLlm,
		streamFn: mock.stream,
	});
	const session = new AgentSession({
		agent,
		sessionManager: SessionManager.inMemory(tempDir.path()),
		settings,
		modelRegistry: sharedModelRegistry,
		toolRegistry: new Map(tools.map(tool => [tool.name, tool])),
	});
	const harness = { session, tempDir, mock };
	activeHarnesses.push(harness);
	return harness;
}

function toolResult(session: AgentSession, toolCallId: string): { isError: boolean; text: string } {
	const message = session.agent.state.messages.find(
		candidate => candidate.role === "toolResult" && candidate.toolCallId === toolCallId,
	);
	if (message?.role !== "toolResult") throw new Error(`no tool result recorded for ${toolCallId}`);
	const text = message.content.flatMap(block => (block.type === "text" ? [block.text] : [])).join("\n");
	return { isError: message.isError === true, text };
}

describe("OMP-246 submission fence: accepted terminal yield then same-response write", () => {
	it("a write emitted after the terminal yield never executes and no further provider call is made", async () => {
		const executions: string[] = [];
		const { session, mock } = await createHarness([terminalYieldTool(), recordTool(executions)] as AgentTool[], [
			toolCalls(yieldCall("call-yield"), recordCall("call-record", "late-write")),
			trailingStop("must not be reached"),
		]);

		await session.prompt("report, then try to write");
		await session.waitForIdle();

		expect(executions).toEqual([]);
		expect(mock.calls).toHaveLength(1);
		expect(toolResult(session, "call-yield")).toEqual({ isError: false, text: "Result submitted." });
		const refused = toolResult(session, "call-record");
		expect(refused.isError).toBe(true);
		expect(refused.text).toContain("Tool was not executed");
	});

	it("a write recorded before the terminal yield executes exactly once", async () => {
		const executions: string[] = [];
		const { session, mock } = await createHarness([terminalYieldTool(), recordTool(executions)] as AgentTool[], [
			toolCalls(recordCall("call-record", "early-write"), yieldCall("call-yield")),
			trailingStop("must not be reached"),
		]);

		await session.prompt("write, then report");
		await session.waitForIdle();

		expect(executions).toEqual(["early-write"]);
		expect(mock.calls).toHaveLength(1);
		expect(toolResult(session, "call-record")).toEqual({ isError: false, text: "recorded:early-write" });
		expect(toolResult(session, "call-yield")).toEqual({ isError: false, text: "Result submitted." });
	});

	it("shared work admitted before the yield settles keeps its real result and the run ends at the yield", async () => {
		const executions: string[] = [];
		const gate = { started: Promise.withResolvers<void>(), release: Promise.withResolvers<void>() };
		const yieldTool = terminalYieldTool({
			beforeReturn: async () => {
				await gate.started.promise;
				// Release on a later macrotask: the terminal abort has landed by then.
				setTimeout(() => gate.release.resolve(), 0);
			},
		});
		const { session, mock } = await createHarness([yieldTool, probeTool(executions, gate)] as AgentTool[], [
			toolCalls(yieldCall("call-yield"), probeCall("call-probe", "ok")),
			trailingStop("must not be reached"),
		]);

		await session.prompt("report while a probe is in flight");
		await session.waitForIdle();

		expect(executions).toEqual(["ok"]);
		expect(mock.calls).toHaveLength(1);
		expect(toolResult(session, "call-probe")).toEqual({ isError: false, text: "probe:ok" });
		expect(toolResult(session, "call-yield")).toEqual({ isError: false, text: "Result submitted." });
	});

	it("known-bad control: without a terminal marker the later write executes and the loop continues", async () => {
		const executions: string[] = [];
		const { session, mock } = await createHarness([incrementalYieldTool(), recordTool(executions)] as AgentTool[], [
			toolCalls(yieldCall("call-yield"), recordCall("call-record", "late-write")),
			trailingStop("continued"),
		]);

		await session.prompt("partial report, then write");
		await session.waitForIdle();

		expect(executions).toEqual(["late-write"]);
		expect(mock.calls).toHaveLength(2);
		expect(toolResult(session, "call-record")).toEqual({ isError: false, text: "recorded:late-write" });
	});
});
