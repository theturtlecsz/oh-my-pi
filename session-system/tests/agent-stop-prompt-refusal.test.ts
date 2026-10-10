import { afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { createMockModel, type MockModel, registerMockApi } from "@oh-my-pi/pi-ai/providers/mock";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { runPrintMode } from "@oh-my-pi/pi-coding-agent/modes/print-mode";
import { createAgentSession } from "@oh-my-pi/pi-coding-agent/sdk";
import { type AgentSession, PromptBlockedError } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import type { StopStatusView } from "@oh-my-pi/pi-work-client";
import { installAgentStopGate } from "../extensions/workflow/agent-stop";

registerMockApi();

const WORKSPACE = "00000000-0000-0000-0000-000000000001";

describe("agent stop prompt refusal integration (OMP-536)", () => {
	let tempDir: string;
	let authStorage: AuthStorage | undefined;
	let session: AgentSession | undefined;
	let stderrOutput: string[];
	let stdoutOutput: string[];

	beforeEach(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "agent-stop-refusal-"));
		stderrOutput = [];
		stdoutOutput = [];
		vi.spyOn(process.stderr, "write").mockImplementation((chunk: unknown, ...args: unknown[]) => {
			stderrOutput.push(String(chunk));
			const last = args[args.length - 1];
			if (typeof last === "function") (last as () => void)();
			return true;
		});
		vi.spyOn(process.stdout, "write").mockImplementation((chunk: unknown, ...args: unknown[]) => {
			stdoutOutput.push(String(chunk));
			const last = args[args.length - 1];
			if (typeof last === "function") (last as () => void)();
			return true;
		});
	});

	afterEach(async () => {
		if (session) {
			await session.dispose().catch(() => {});
			session = undefined;
		}
		if (authStorage) {
			authStorage.close();
			authStorage = undefined;
		}
		await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
		vi.restoreAllMocks();
	});

	async function createSession(
		stopped: boolean,
		stopReason: string | null = stopped ? "drill" : null,
	): Promise<{ session: AgentSession; mock: MockModel }> {
		authStorage = await AuthStorage.create(path.join(tempDir, "auth.db"));
		authStorage.keys.setRuntime("mock", "test-key");
		const modelRegistry = new ModelRegistry(authStorage, path.join(tempDir, "models.yml"));
		const mock = createMockModel({
			responses: [{ content: ["ok"] }],
		});
		const fakeClient = {
			stopStatus: async (): Promise<StopStatusView> => ({
				workspace_id: WORKSPACE,
				stopped,
				reason: stopReason,
				changed_at: null,
				changed_by_actor_kind: stopped ? "owner" : null,
			}),
		};

		const created = await createAgentSession({
			cwd: tempDir,
			agentDir: tempDir,
			sessionManager: SessionManager.inMemory(),
			authStorage,
			modelRegistry,
			model: mock,
			settings: Settings.isolated({
				"compaction.enabled": false,
				"todo.enabled": false,
				"retry.enabled": false,
			}),
			disableExtensionDiscovery: true,
			extensions: [pi => installAgentStopGate(pi, fakeClient)],
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
			skipPythonPreflight: true,
			rules: [],
			preloadedCustomToolPaths: [],
			toolNames: ["read"],
		});

		session = created.session;
		return { session: created.session, mock };
	}

	test("stopped ('drill') runPrintMode returns 1, names the stop on stderr, and dispatches 0 requests", async () => {
		const { session: activeSession, mock } = await createSession(true, "drill");

		const exitCode = await runPrintMode(activeSession, {
			mode: "text",
			initialMessage: "reply with the word ok",
		});

		expect(exitCode).toBe(1);
		const stderr = stderrOutput.join("");
		expect(stderr).toContain("Agent stop engaged");
		expect(stderr).toContain("drill");
		expect(mock.calls).toHaveLength(0);
	});

	test("stopped session.prompt rejects with a message naming the stop, and dispatches 0 requests", async () => {
		const { session: activeSession, mock } = await createSession(true, "drill");

		let caughtError: unknown;
		try {
			await activeSession.prompt("hi");
		} catch (error) {
			caughtError = error;
		}

		expect(caughtError).toBeInstanceOf(PromptBlockedError);
		expect((caughtError as Error).message).toContain("Agent stop engaged");
		expect((caughtError as Error).message).toContain("drill");
		expect(mock.calls).toHaveLength(0);
	});

	test("released runPrintMode returns 0 and dispatches 1 provider request", async () => {
		const { session: activeSession, mock } = await createSession(false);

		const exitCode = await runPrintMode(activeSession, {
			mode: "text",
			initialMessage: "reply with the word ok",
		});

		expect(exitCode).toBe(0);
		expect(mock.calls).toHaveLength(1);
	});
});
