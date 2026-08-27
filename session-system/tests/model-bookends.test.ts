// HOME-131/OMP-168 model bookends: /intake auto-routing to the intake role.
// OMP-168 removed model-transported audit gates: audits run natively through
// work run_audit, so /summary, before_agent_start, auditor task calls, and
// task results receive zero interception from this extension.
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";

const repoRoot = path.resolve(import.meta.dir, "../..");
const extPath = path.join(repoRoot, "session-system/extensions/model-bookends.ts");

interface Harness {
	runner: ExtensionRunner;
	setModelCalls: Array<{ provider: string; id: string }>;
	thinkingLevels: string[];
	notifies: string[];
}

async function makeHarness(depth = 0, opts: { intakeConfigured?: boolean; hasCredential?: boolean } = {}): Promise<Harness> {
	const { intakeConfigured = true, hasCredential = true } = opts;
	const result = await loadExtensions([extPath], repoRoot);
	if (result.errors.length > 0) throw new Error(result.errors.map(e => e.error).join("; "));
	const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
	const gptModel = { id: "gpt-5.2", provider: "openai", name: "GPT 5.2", api: "openai-responses" };
	const fakeRegistry = {
		getAvailable: () => (intakeConfigured ? [fableModel, gptModel] : [gptModel]),
		find: (p: string, id: string) => (p === "anthropic" && id === "claude-fable-5" ? fableModel : id === "gpt-5.2" ? gptModel : undefined),
		hasProvider: () => true,
		resolver: () => () => undefined,
	};
	const fakeSettings = {
		getModelRole: (role: string) => (role === "intake" && intakeConfigured ? "anthropic/claude-fable-5" : undefined),
		getModelRoles: () => (intakeConfigured ? { intake: "anthropic/claude-fable-5" } : {}),
		get: (key: string) => (key === "thinkingLevel" ? "medium" : undefined),
		getStorage: () => undefined,
	};
	const runner = new ExtensionRunner(
		result.extensions,
		result.runtime,
		repoRoot,
		{ getCwd: () => repoRoot, getBranch: () => [], getSessionId: () => "session-test" } as never,
		fakeRegistry as never,
		undefined,
		fakeSettings as never,
		undefined,
		undefined,
		depth,
	);
	const setModelCalls: Array<{ provider: string; id: string }> = [];
	const thinkingLevels: string[] = [];
	const notifies: string[] = [];
	runner.initialize(
		{
			setModel: async (model: { provider: string; id: string }) => {
				if (!hasCredential) return false;
				setModelCalls.push({ provider: model.provider, id: model.id });
				return true;
			},
			setThinkingLevel: (level: string) => {
				thinkingLevels.push(level);
			},
		} as never,
		{
			getModel: () => fableModel,
			isIdle: () => true,
			abort: () => {},
		} as never,
		undefined,
		{
			notify: (msg: string) => {
				notifies.push(msg);
			},
		} as never,
	);
	await runner.emit({ type: "session_start" } as never);
	return { runner, setModelCalls, thinkingLevels, notifies };
}

const taskCall = (id: string, input: Record<string, unknown>) =>
	({ type: "tool_call", toolName: "task", toolCallId: id, input }) as never;
const auditorCall = (id: string, task: unknown = "sealed body") =>
	taskCall(id, { context: "audit the completed work", tasks: [{ agent: "auditor", task }] });

describe("/intake routing (HOME-131)", () => {
	test("owner /intake switches to the intake role at :high and forwards", async () => {
		const h = await makeHarness();
		const result = (await h.runner.emitInput("/intake plan the thing", undefined, "interactive")) as { text?: string } | undefined;
		expect(h.setModelCalls).toEqual([{ provider: "anthropic", id: "claude-fable-5" }]);
		expect(h.thinkingLevels).toEqual(["high"]);
		expect(result?.text).toBe("/skill:intake plan the thing");
	});
	test("unresolvable intake role fails closed", async () => {
		const h = await makeHarness(0, { intakeConfigured: false });
		const result = (await h.runner.emitInput("/intake x", undefined, "interactive")) as { handled?: boolean } | undefined;
		expect(result?.handled).toBe(true);
		expect(h.notifies.some(n => n.includes("could not resolve @intake"))).toBe(true);
	});
	test("missing credential fails closed without forwarding", async () => {
		const h = await makeHarness(0, { hasCredential: false });
		const result = (await h.runner.emitInput("/intake x", undefined, "interactive")) as { handled?: boolean } | undefined;
		expect(result?.handled).toBe(true);
		expect(h.notifies.some(n => n.includes("no credential"))).toBe(true);
	});
	test("subagent /intake is ignored", async () => {
		const h = await makeHarness(1);
		const result = await h.runner.emitInput("/intake x", undefined, "interactive");
		expect(result?.text).toBeUndefined();
		expect(result?.handled).toBeUndefined();
		expect(h.setModelCalls).toHaveLength(0);
	});
});

describe("audit cutover (OMP-168: no model-transport interception)", () => {
	test("/summary and before_agent_start inject no audit contract", async () => {
		const h = await makeHarness();
		await h.runner.emitInput("/summary", undefined, "interactive");
		const startResult = await h.runner.emitBeforeAgentStart("x", undefined, []);
		expect(startResult).toBeUndefined();
	});

	test("task calls with agent:auditor pass through untouched", async () => {
		const h = await makeHarness();
		await h.runner.emitInput("/summary", undefined, "interactive");
		const callResult = await h.runner.emitToolCall(auditorCall("t-1"));
		expect(callResult).toBeUndefined();
	});

	test("tool results for auditor tasks receive no ledger settlement interception", async () => {
		const h = await makeHarness();
		await h.runner.emitInput("/summary", undefined, "interactive");
		const resResult = await h.runner.emitToolResult({
			type: "tool_result",
			toolName: "task",
			toolCallId: "t-1",
			input: {},
			content: [{ type: "text", text: "VERDICT: PASS" }],
			details: { results: [{ output: "VERDICT: PASS" }] },
			isError: false,
		} as never);
		expect(resResult).toBeUndefined();
	});
});
