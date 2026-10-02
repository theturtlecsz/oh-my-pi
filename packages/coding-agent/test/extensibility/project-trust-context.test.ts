import { afterAll, afterEach, beforeAll, describe, expect, it } from "bun:test";
import * as path from "node:path";
import type { Model } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import type { ExtensionRuntime } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { AgentRegistry } from "@oh-my-pi/pi-coding-agent/registry/agent-registry";
import { createAgentSession } from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import type { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { TempDir } from "@oh-my-pi/pi-utils";
import { createInMemoryAuthStorage } from "../helpers/agent-session-setup";

describe("project trust context (L-RT-03)", () => {
	let authStorage: AuthStorage;
	let modelRegistry: ModelRegistry;
	let model: Model;

	beforeAll(() => {
		authStorage = createInMemoryAuthStorage();
		authStorage.keys.setRuntime("anthropic", "test-key");
		modelRegistry = new ModelRegistry(authStorage);
		const bundled = getBundledModel("anthropic", "claude-sonnet-4-5");
		if (!bundled) throw new Error("Expected built-in anthropic model to exist");
		model = bundled;
	});

	afterAll(() => {
		authStorage.close();
	});

	let cwdDir: TempDir | undefined;
	let agentDir: TempDir | undefined;
	let session: AgentSession | undefined;

	afterEach(async () => {
		await session?.dispose();
		session = undefined;
		try {
			await cwdDir?.remove();
		} catch {}
		try {
			await agentDir?.remove();
		} catch {}
		cwdDir = undefined;
		agentDir = undefined;
	});

	it("reflects project trust from <agentDir>/trusted-projects.json in extension and command contexts", async () => {
		cwdDir = TempDir.createSync("@pi-cwd-");
		agentDir = TempDir.createSync("@pi-agent-");

		const settings = Settings.isolated({
			"async.enabled": false,
			"compaction.enabled": false,
		});
		await settings.reloadForCwd(cwdDir.path());

		const result = await createAgentSession({
			cwd: cwdDir.path(),
			agentDir: agentDir.path(),
			sessionManager: SessionManager.create(cwdDir.path(), cwdDir.path()),
			agentRegistry: new AgentRegistry(),
			authStorage,
			modelRegistry,
			settings,
			model,
			disableExtensionDiscovery: true,
			skills: [],
			contextFiles: [],
			workspaceTree: {
				rootPath: cwdDir.path(),
				rendered: "",
				truncated: false,
				totalLines: 0,
				agentsMdFiles: [],
			},
			promptTemplates: [],
			slashCommands: [],
			enableMCP: false,
			enableLsp: false,
		});
		session = result.session;

		const runner = session.extensionRunner;
		expect(runner).toBeDefined();

		// Initially, cwd is not listed in trusted-projects.json: both contexts report false
		expect(runner!.createContext().isProjectTrusted()).toBe(false);
		expect(runner!.createCommandContext().isProjectTrusted()).toBe(false);

		// With cwd listed in <agentDir>/trusted-projects.json, both report true
		const trustedConfig = {
			version: 1,
			paths: [cwdDir.path()],
		};
		await Bun.write(path.join(agentDir.path(), "trusted-projects.json"), JSON.stringify(trustedConfig));

		expect(runner!.createContext().isProjectTrusted()).toBe(true);
		expect(runner!.createCommandContext().isProjectTrusted()).toBe(true);
	});

	it("runner with setProjectTrustCheck follows a /move style cwd change (stub getCwd)", () => {
		let currentCwd = "/tmp/untrusted-dir";
		const sessionManager = {
			getCwd: () => currentCwd,
			getSessionId: () => "test-session",
		} as unknown as SessionManager;

		const runner = new ExtensionRunner(
			[],
			{ flagValues: new Map(), pendingProviderRegistrations: [] } as unknown as ExtensionRuntime,
			currentCwd,
			sessionManager,
			{} as never,
		);

		runner.setProjectTrustCheck(dir => dir === "/tmp/trusted-dir");

		const ctx = runner.createContext();
		const cmdCtx = runner.createCommandContext();

		expect(ctx.isProjectTrusted()).toBe(false);
		expect(cmdCtx.isProjectTrusted()).toBe(false);

		// Simulate /move: cwd changes in sessionManager
		currentCwd = "/tmp/trusted-dir";

		expect(ctx.isProjectTrusted()).toBe(true);
		expect(cmdCtx.isProjectTrusted()).toBe(true);
	});
});
