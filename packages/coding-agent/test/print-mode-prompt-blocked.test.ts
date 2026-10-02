/**
 * Print mode must report a `before_agent_start` block instead of letting the
 * rejection escape as a fatal dump.
 *
 * OMP-536-s01 made `session.prompt` reject with `PromptBlockedError` when a
 * handler returns `block: true`, carrying the reason as its message. Print mode
 * now reports `Error: <reason>` on stderr, sends no further messages, and exits
 * 1 — in both text and JSON mode.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import type { AssistantMessage } from "@oh-my-pi/pi-ai";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { runPrintMode } from "@oh-my-pi/pi-coding-agent/modes/print-mode";
import { type AgentSession, PromptBlockedError } from "@oh-my-pi/pi-coding-agent/session/agent-session";

const BLOCK_REASON = "Agent stop engaged: drill. Only the owner can release it via omp-work stop release.";

function makeAssistantMessage(text: string): AssistantMessage {
	return {
		role: "assistant",
		content: [{ type: "text", text }],
		api: "anthropic-messages",
		provider: "anthropic",
		model: "claude-sonnet-4-5",
		stopReason: "stop",
		usage: {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		timestamp: Date.now(),
	};
}

interface FakeSession {
	session: AgentSession;
	getPromptCalls: () => string[];
	disposed: () => boolean;
}

/** Session whose `prompt` either rejects with `blocked` or pushes a final reply. */
function createFakeSession(blocked: boolean): FakeSession {
	const messages: AssistantMessage[] = [];
	const promptCalls: string[] = [];
	let disposed = false;

	const session = {
		state: { messages },
		getLastAssistantMessage: () => messages.findLast(message => message.role === "assistant"),
		settings: Settings.isolated(),
		sessionManager: {
			getHeader: () => undefined,
			buildSessionContext: () => ({ messages: [] }),
			getEntries: () => [],
			onPersistenceError: () => () => {},
		},
		extensionRunner: undefined,
		subscribe: () => () => {},
		prompt: async (text: string) => {
			promptCalls.push(text);
			if (blocked) throw new PromptBlockedError(BLOCK_REASON);
			messages.push(makeAssistantMessage("final answer"));
			return true;
		},
		setTextOutputCommitted: () => {},
		waitForIdle: async () => {},
		prepareForHeadlessAdvisorDrain: () => {},
		waitForAdvisorCatchup: async () => true,
		dispose: async () => {
			disposed = true;
		},
	} as unknown as AgentSession;

	return { session, getPromptCalls: () => promptCalls, disposed: () => disposed };
}

describe("print mode prompt blocked", () => {
	let stderrOutput: string[];
	let stdoutOutput: string[];

	beforeEach(() => {
		stderrOutput = [];
		stdoutOutput = [];
		vi.spyOn(process.stderr, "write").mockImplementation((chunk: unknown) => {
			stderrOutput.push(String(chunk));
			return true;
		});
		vi.spyOn(process.stdout, "write").mockImplementation((...args: unknown[]) => {
			const chunk = args[0];
			if (typeof chunk === "string") stdoutOutput.push(chunk);
			const last = args[args.length - 1];
			if (typeof last === "function") (last as () => void)();
			return true;
		});
	});

	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("reports the block reason, sends no later message, and exits 1", async () => {
		const fake = createFakeSession(true);
		const exitCode = await runPrintMode(fake.session, {
			mode: "text",
			initialMessage: "first",
			messages: ["second"],
		});

		expect(exitCode).toBe(1);
		expect(stderrOutput.join("")).toContain(`Error: ${BLOCK_REASON}`);
		// The block persists for the whole run: a later prompt would be refused
		// by the same handler, so print mode must not dispatch it.
		expect(fake.getPromptCalls()).toEqual(["first"]);
		expect(fake.disposed()).toBe(true);
	});

	it("reports the block reason in JSON mode and exits 1", async () => {
		const fake = createFakeSession(true);
		const exitCode = await runPrintMode(fake.session, {
			mode: "json",
			initialMessage: "first",
			messages: ["second"],
		});

		expect(exitCode).toBe(1);
		expect(stderrOutput.join("")).toContain(`Error: ${BLOCK_REASON}`);
		expect(fake.getPromptCalls()).toEqual(["first"]);
	});

	it("returns 0 and prints the reply when the prompt is accepted", async () => {
		const fake = createFakeSession(false);
		const exitCode = await runPrintMode(fake.session, {
			mode: "text",
			initialMessage: "first",
			messages: ["second"],
		});

		expect(exitCode).toBe(0);
		expect(stdoutOutput.join("")).toBe("final answer\n");
		expect(stderrOutput.join("")).not.toContain("Error:");
		expect(fake.getPromptCalls()).toEqual(["first", "second"]);
		expect(fake.disposed()).toBe(true);
	});
});
