// HOME-131/OMP-47 model bookends: /intake auto-routing to the intake role, and
// the reduced /summary audit gate — task-tool interception plus WorkService
// reserve/settle transport. The service owns every report/budget/identity gate
// (proven in workflow-sequence.test.ts audit mode and the Python service
// tests); this file proves the interception contract and transport shapes.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { ExtensionRunner, loadExtensions } from "@oh-my-pi/pi-coding-agent";
import { AUDIT_CONTRACT, transportPayload } from "../extensions/model-bookends";

const repoRoot = path.resolve(import.meta.dir, "../..");
const extPath = path.join(repoRoot, "session-system/extensions/model-bookends.ts");

interface Harness {
	runner: ExtensionRunner;
	setModelCalls: Array<{ provider: string; id: string }>;
	thinkingLevels: string[];
	notifies: string[];
}

async function makeHarness(depth = 0, opts: { intakeConfigured?: boolean; hasCredential?: boolean; auditRole?: string } = {}): Promise<Harness> {
	const { intakeConfigured = true, hasCredential = true, auditRole = "openai/gpt-5.2" } = opts;
	const result = await loadExtensions([extPath], repoRoot);
	if (result.errors.length > 0) throw new Error(result.errors.map(e => e.error).join("; "));
	const fableModel = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
	const gptModel = { id: "gpt-5.2", provider: "openai", name: "GPT 5.2", api: "openai-responses" };
	const fakeRegistry = { getAvailable: () => [fableModel, gptModel], hasProvider: () => true };
	const fakeSettings = {
		getModelRole: (role: string) => {
			if (intakeConfigured && role === "intake") return "anthropic/claude-fable-5:high";
			if (role === "audit") return auditRole;
			return undefined;
		},
		get: () => undefined,
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
			getCommands: () => [],
			setModel: async (model: { provider: string; id: string }) => {
				setModelCalls.push({ provider: model.provider, id: model.id });
				return hasCredential;
			},
			getThinkingLevel: () => "high",
			setThinkingLevel: (level: string) => {
				thinkingLevels.push(level);
			},
		} as never,
		{
			getModel: () => fableModel,
			isIdle: () => true,
			abort: () => {},
			hasPendingMessages: () => false,
			shutdown: () => {},
			getSystemPrompt: () => [],
		} as never,
		undefined,
		{
			theme: { fg: (_c: string, t: string) => t },
			setStatus: () => {},
			notify: (msg: string) => notifies.push(msg),
		} as never,
	);
	await runner.emit({ type: "session_start" } as never);
	return { runner, setModelCalls, thinkingLevels, notifies };
}

const taskCall = (id: string, input: Record<string, unknown>) =>
	({ type: "tool_call", toolName: "task", toolCallId: id, input }) as never;
const auditorCall = (id: string, task: unknown = "sealed body") =>
	taskCall(id, { context: "audit the completed work", tasks: [{ agent: "auditor", task }] });

async function armSummary(h: Harness): Promise<void> {
	await h.runner.emitInput("/summary", undefined, "interactive");
}

function blockOf(result: unknown): { block?: boolean; reason?: string } {
	return (result ?? {}) as { block?: boolean; reason?: string };
}

describe("transportPayload (untouched transport shapes)", () => {
	test("prefers details.results[0].output verbatim, any shape", () => {
		expect(transportPayload({ results: [{ output: "VERDICT: PASS" }] }, "ignored")).toBe("VERDICT: PASS");
		const objectPayload = { report: "VERDICT: PASS" };
		expect(transportPayload({ results: [{ output: objectPayload }] }, "ignored")).toBe(objectPayload);
		const arrayPayload = [1, 2];
		expect(transportPayload({ results: [{ output: arrayPayload }] }, "ignored")).toBe(arrayPayload);
	});
	test("falls back to raw content text; undefined when nothing arrived", () => {
		expect(transportPayload(undefined, "raw text")).toBe("raw text");
		expect(transportPayload({ results: [] }, "raw text")).toBe("raw text");
		expect(transportPayload(undefined, "   ")).toBeUndefined();
		expect(transportPayload(undefined, "")).toBeUndefined();
	});
});

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
});

describe("audit gate interception (OMP-47)", () => {
	test("injects the audit contract once after /summary", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const first = await h.runner.emitBeforeAgentStart("x", undefined, []);
		expect(first?.messages?.[0]?.content).toBe(AUDIT_CONTRACT);
		expect(AUDIT_CONTRACT).toContain("AUDIT TASK");
		expect(AUDIT_CONTRACT).toContain("byte-for-byte");
		const second = await h.runner.emitBeforeAgentStart("y", undefined, []);
		expect(second).toBeUndefined();
	});

	test("non-auditor task batches pass through untouched", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const result = await h.runner.emitToolCall(taskCall("t-1", { tasks: [{ agent: "scout", task: "look around" }] }));
		expect(result).toBeUndefined();
	});

	test("the auditor must be alone in its batch", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const result = blockOf(
			await h.runner.emitToolCall(taskCall("t-2", { tasks: [{ agent: "auditor", task: "body" }, { agent: "scout", task: "x" }] })),
		);
		expect(result.block).toBe(true);
		expect(result.reason).toContain("only task in its batch");
	});

	test("a non-string task body is refused", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const result = blockOf(await h.runner.emitToolCall(auditorCall("t-3", { sections: true })));
		expect(result.block).toBe(true);
		expect(result.reason).toContain("sealed task body");
	});

	test("outputSchema is refused — the report must stay plain text", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const result = blockOf(
			await h.runner.emitToolCall(taskCall("t-4", { tasks: [{ agent: "auditor", task: "body", outputSchema: { type: "object" } }] })),
		);
		expect(result.block).toBe(true);
		expect(result.reason).toContain("outputSchema");
	});

	test("an unresolvable @audit role fails closed before any service call", async () => {
		const h = await makeHarness(0, { auditRole: "" });
		await armSummary(h);
		const result = blockOf(await h.runner.emitToolCall(auditorCall("t-5")));
		expect(result.block).toBe(true);
		expect(result.reason).toContain("@audit");
	});

	test("a dormant Work Ledger backend refuses the spawn (no reservation possible)", async () => {
		// Point XDG config at an empty dir: no omp-work/client.json → service() = null.
		const savedXdg = process.env.XDG_CONFIG_HOME;
		process.env.XDG_CONFIG_HOME = fs.mkdtempSync(path.join(os.tmpdir(), "mb-dormant-"));
		try {
			const h = await makeHarness();
			await armSummary(h);
			const result = blockOf(await h.runner.emitToolCall(auditorCall("t-6")));
			expect(result.block).toBe(true);
			expect(result.reason).toContain("dormant");
		} finally {
			if (savedXdg === undefined) delete process.env.XDG_CONFIG_HOME;
			else process.env.XDG_CONFIG_HOME = savedXdg;
		}
	});

	test("tool_result is ignored when no auditor launch was reserved", async () => {
		const h = await makeHarness();
		await armSummary(h);
		const result = await h.runner.emitToolResult({
			type: "tool_result",
			toolName: "task",
			toolCallId: "unrelated",
			input: {},
			content: [{ type: "text", text: "report" }],
			isError: false,
		} as never);
		expect(result).toBeUndefined();
	});

	test("the gate is inert without /summary and in subagents", async () => {
		const idle = await makeHarness();
		expect(await idle.runner.emitToolCall(auditorCall("t-7"))).toBeUndefined();
		const sub = await makeHarness(1);
		await sub.runner.emitInput("/summary", undefined, "interactive");
		expect(await sub.runner.emitToolCall(auditorCall("t-8"))).toBeUndefined();
	});

	test("a session switch disarms the gate", async () => {
		const h = await makeHarness();
		await armSummary(h);
		await h.runner.emit({ type: "session_switch", reason: "resume" } as never);
		expect(await h.runner.emitToolCall(auditorCall("t-9"))).toBeUndefined();
	});
});
