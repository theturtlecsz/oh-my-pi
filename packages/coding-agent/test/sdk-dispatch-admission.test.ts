import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { AuthStorage } from "@oh-my-pi/pi-ai";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { createAgentSession } from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import * as settingsStreamFn from "@oh-my-pi/pi-coding-agent/session/settings-stream-fn";
import { removeSyncWithRetries, Snowflake } from "@oh-my-pi/pi-utils";
import { createInMemoryAuthStorage } from "./helpers/agent-session-setup";

interface SessionDirs {
	cwd: string;
	agentDir: string;
}

interface SetupOptions {
	admitFirstChatDispatch?: () => void | Promise<void>;
	onFirstChatDispatch?: () => void | Promise<void>;
}

const fixtureModel =
	getBundledModel("anthropic", "claude-3-5-sonnet-20241022") ??
	getBundledModel("openai", "gpt-4o") ??
	getBundledModel("anthropic", "claude-sonnet-4") ??
	getBundledModel("google", "gemini-2.5-flash");
if (!fixtureModel) {
	throw new Error("Fixture model not found in bundled models");
}

describe("createAgentSession first dispatch admission", () => {
	const tempDirs: string[] = [];
	const sessionsToDispose: AgentSession[] = [];
	const authStoragesToClose: AuthStorage[] = [];
	let providerCalls = 0;

	const makeDirs = (label: string): SessionDirs => {
		const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), `pi-sdk-dispatch-admission-${label}-${Snowflake.next()}-`));
		tempDirs.push(tempDir);
		const cwd = path.join(tempDir, "project");
		const agentDir = path.join(tempDir, "agent");
		fs.mkdirSync(cwd, { recursive: true });
		fs.mkdirSync(agentDir, { recursive: true });
		return { cwd, agentDir };
	};

	const createTestSession = async (options: SetupOptions = {}): Promise<AgentSession> => {
		const dirs = makeDirs("admission");
		const authStorage = createInMemoryAuthStorage();
		authStoragesToClose.push(authStorage);
		const sessionManager = SessionManager.inMemory();
		const modelRegistry = new ModelRegistry(authStorage, path.join(dirs.agentDir, "models.json"));

		const { session } = await createAgentSession({
			cwd: dirs.cwd,
			agentDir: dirs.agentDir,
			authStorage,
			modelRegistry,
			sessionManager,
			settings: Settings.isolated(),
			model: fixtureModel,
			disableExtensionDiscovery: true,
			extensions: [],
			skills: [],
			rules: [],
			contextFiles: [],
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
			skipPythonPreflight: true,
			workspaceTree: {
				rootPath: dirs.cwd,
				rendered: ".\n",
				truncated: false,
				totalLines: 1,
				agentsMdFiles: [],
			},
			toolNames: [],
			preloadedCustomToolPaths: [],
			admitFirstChatDispatch: options.admitFirstChatDispatch,
			onFirstChatDispatch: options.onFirstChatDispatch,
		});

		sessionsToDispose.push(session);
		return session;
	};

	beforeEach(() => {
		providerCalls = 0;
		vi.spyOn(settingsStreamFn, "createSettingsAwareStreamFn").mockImplementation(() => {
			return async () => {
				providerCalls++;
				return new AssistantMessageEventStream();
			};
		});
	});

	afterEach(async () => {
		vi.restoreAllMocks();
		for (const session of sessionsToDispose) {
			await session.dispose();
		}
		sessionsToDispose.length = 0;
		for (const authStorage of authStoragesToClose) {
			authStorage.close();
		}
		authStoragesToClose.length = 0;
		for (const dir of tempDirs) {
			if (fs.existsSync(dir)) {
				removeSyncWithRetries(dir);
			}
		}
		tempDirs.length = 0;
	});

	it("enforces async denial and sticky rejection on repeated calls", async () => {
		const session = await createTestSession({
			admitFirstChatDispatch: async () => {
				throw new Error("handoff unauthorized");
			},
		});

		await expect(session.agent.streamFn(fixtureModel, { messages: [] })).rejects.toThrow("handoff unauthorized");
		expect(providerCalls).toBe(0);

		await expect(session.agent.streamFn(fixtureModel, { messages: [] })).rejects.toThrow("handoff unauthorized");
		expect(providerCalls).toBe(0);
	});

	it("enforces synchronous throw and sticky rejection on repeated calls", async () => {
		const session = await createTestSession({
			admitFirstChatDispatch: () => {
				throw new Error("sync admission failure");
			},
		});

		await expect(session.agent.streamFn(fixtureModel, { messages: [] })).rejects.toThrow("sync admission failure");
		expect(providerCalls).toBe(0);

		await expect(session.agent.streamFn(fixtureModel, { messages: [] })).rejects.toThrow("sync admission failure");
		expect(providerCalls).toBe(0);
	});

	it("runs single admission under concurrent calls on success", async () => {
		let admissionCalls = 0;
		const gate = Promise.withResolvers<void>();

		const session = await createTestSession({
			admitFirstChatDispatch: () => {
				admissionCalls++;
				return gate.promise;
			},
		});

		const call1 = session.agent.streamFn(fixtureModel, { messages: [] });
		const call2 = session.agent.streamFn(fixtureModel, { messages: [] });

		gate.resolve();
		await Promise.all([call1, call2]);

		expect(admissionCalls).toBe(1);
		expect(providerCalls).toBe(2);
	});

	it("runs single admission under concurrent calls on failure", async () => {
		let admissionCalls = 0;
		const entered = Promise.withResolvers<void>();
		const gate = Promise.withResolvers<void>();

		const session = await createTestSession({
			admitFirstChatDispatch: () => {
				admissionCalls++;
				entered.resolve();
				return gate.promise;
			},
		});

		const call1 = session.agent.streamFn(fixtureModel, { messages: [] });
		const call2 = session.agent.streamFn(fixtureModel, { messages: [] });

		await entered.promise;

		const resultsPromise = Promise.allSettled([call1, call2]);
		gate.reject(new Error("concurrent handoff rejected"));
		const results = await resultsPromise;

		expect(results[0].status).toBe("rejected");
		expect(results[1].status).toBe("rejected");
		if (results[0].status === "rejected") {
			expect(results[0].reason?.message).toContain("concurrent handoff rejected");
		}
		if (results[1].status === "rejected") {
			expect(results[1].reason?.message).toContain("concurrent handoff rejected");
		}

		expect(admissionCalls).toBe(1);
		expect(providerCalls).toBe(0);
	});

	it("does not invoke admission gate or provider when signal is pre-aborted", async () => {
		let admissionCalled = false;
		const session = await createTestSession({
			admitFirstChatDispatch: async () => {
				admissionCalled = true;
			},
		});

		const controller = new AbortController();
		controller.abort(new Error("pre-aborted dispatch"));

		await expect(
			session.agent.streamFn(fixtureModel, { messages: [] }, { signal: controller.signal }),
		).rejects.toThrow("pre-aborted dispatch");

		expect(admissionCalled).toBe(false);
		expect(providerCalls).toBe(0);
	});

	it("rejects promptly on abort during unresolved gate without late dispatch after resolve", async () => {
		const gate = Promise.withResolvers<void>();
		const session = await createTestSession({
			admitFirstChatDispatch: () => gate.promise,
		});

		const controller = new AbortController();
		const callPromise = session.agent.streamFn(fixtureModel, { messages: [] }, { signal: controller.signal });

		controller.abort(new Error("caller aborted wait"));

		await expect(callPromise).rejects.toThrow("caller aborted wait");
		expect(providerCalls).toBe(0);

		gate.resolve();
		await Promise.resolve();
		await Promise.resolve();

		expect(providerCalls).toBe(0);
	});

	it("still allows provider dispatch when onFirstChatDispatch telemetry throws", async () => {
		let telemetryCalled = false;
		let admissionCalled = false;

		const session = await createTestSession({
			admitFirstChatDispatch: async () => {
				admissionCalled = true;
			},
			onFirstChatDispatch: () => {
				telemetryCalled = true;
				throw new Error("telemetry error");
			},
		});

		await session.agent.streamFn(fixtureModel, { messages: [] });

		expect(admissionCalled).toBe(true);
		expect(telemetryCalled).toBe(true);
		expect(providerCalls).toBe(1);
	});

	it("blocks provider dispatch when signal aborts during awaited telemetry", async () => {
		const controller = new AbortController();
		const telemetryGate = Promise.withResolvers<void>();

		const session = await createTestSession({
			admitFirstChatDispatch: async () => {},
			onFirstChatDispatch: async () => {
				controller.abort(new Error("aborted during telemetry"));
				await telemetryGate.promise;
			},
		});

		const callPromise = session.agent.streamFn(fixtureModel, { messages: [] }, { signal: controller.signal });

		telemetryGate.resolve();

		await expect(callPromise).rejects.toThrow("aborted during telemetry");
		expect(providerCalls).toBe(0);
	});

	it("dispatches to provider with default options when no hooks are supplied", async () => {
		const session = await createTestSession();

		await session.agent.streamFn(fixtureModel, { messages: [] });

		expect(providerCalls).toBe(1);
	});
});
