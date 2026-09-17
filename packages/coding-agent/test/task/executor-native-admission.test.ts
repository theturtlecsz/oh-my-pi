import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { AuthStorage } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as settingsStreamFn from "@oh-my-pi/pi-coding-agent/session/settings-stream-fn";
import { runSubprocess } from "@oh-my-pi/pi-coding-agent/task/executor";
import { removeSyncWithRetries, Snowflake } from "@oh-my-pi/pi-utils";
import { createInMemoryAuthStorage } from "../helpers/agent-session-setup";

interface TestDirs {
	tempDir: string;
	agentDir: string;
	artifactsDir: string;
	authStorage: AuthStorage;
	modelRegistry: ModelRegistry;
}

const fixtureModel =
	getBundledModel("anthropic", "claude-3-5-sonnet-20241022") ??
	getBundledModel("openai", "gpt-4o") ??
	getBundledModel("anthropic", "claude-sonnet-4") ??
	getBundledModel("google", "gemini-2.5-flash");
if (!fixtureModel) {
	throw new Error("Fixture model not found in bundled models");
}

describe("runSubprocess native admission enforcement", () => {
	const tempDirs: string[] = [];
	const authStoragesToClose: AuthStorage[] = [];
	let providerCalls = 0;

	const setupDirsAndAuth = (): TestDirs => {
		const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), `pi-executor-native-admission-${Snowflake.next()}-`));
		tempDirs.push(tempDir);
		const agentDir = path.join(tempDir, "agent");
		const artifactsDir = path.join(tempDir, "artifacts");
		fs.mkdirSync(agentDir, { recursive: true });
		fs.mkdirSync(artifactsDir, { recursive: true });

		const authStorage = createInMemoryAuthStorage();
		authStoragesToClose.push(authStorage);
		const modelRegistry = new ModelRegistry(authStorage, path.join(agentDir, "models.json"));

		return { tempDir, agentDir, artifactsDir, authStorage, modelRegistry };
	};

	beforeEach(() => {
		providerCalls = 0;
		vi.spyOn(ModelRegistry.prototype, "refresh").mockImplementation(async () => {});
	});

	afterEach(async () => {
		vi.restoreAllMocks();
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

	it("enforces rejection when native handoff rejects", async () => {
		let handoffCalls = 0;
		const { tempDir, artifactsDir, authStorage, modelRegistry } = setupDirsAndAuth();

		vi.spyOn(settingsStreamFn, "createSettingsAwareStreamFn").mockImplementation(() => {
			return async () => {
				providerCalls++;
				throw new Error("UNEXPECTED_PROVIDER_AFTER_DENIAL");
			};
		});

		const result = await runSubprocess({
			cwd: tempDir,
			artifactsDir,
			agent: {
				name: "task",
				description: "test agent",
				systemPrompt: "test system prompt",
				tools: ["read"],
				source: "bundled",
			},
			task: "execute native task",
			index: 0,
			id: "native-admission-reject",
			authStorage,
			modelRegistry,
			settings: Settings.isolated(),
			modelOverride: `${fixtureModel.provider}/${fixtureModel.id}`,
			getApiKey: () => "FIXTURE_KEY",
			restrictToolNames: true,
			enableLsp: false,
			enableIrc: false,
			enableMCP: false,
			rules: [],
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			preloadedExtensionPaths: [],
			preloadedCustomToolPaths: [],
			maxRuntimeMs: 5000,
			onNativeStageHandoff: async () => {
				handoffCalls++;
				throw new Error("native handoff unauthorized");
			},
		});

		expect(handoffCalls).toBe(1);
		expect(providerCalls).toBe(0);
		expect(result.exitCode).not.toBe(0);
		expect(String(result.error)).toContain("native handoff unauthorized");
	});

	it("dispatches to provider once and surfaces terminal sentinel on success", async () => {
		let handoffCalls = 0;
		const { tempDir, artifactsDir, authStorage, modelRegistry } = setupDirsAndAuth();

		vi.spyOn(settingsStreamFn, "createSettingsAwareStreamFn").mockImplementation(() => {
			return async () => {
				providerCalls++;
				throw new Error("TERMINAL_SENTINEL_DISPATCH");
			};
		});

		const result = await runSubprocess({
			cwd: tempDir,
			artifactsDir,
			agent: {
				name: "task",
				description: "test agent",
				systemPrompt: "test system prompt",
				tools: ["read"],
				source: "bundled",
			},
			task: "execute native task",
			index: 0,
			id: "native-admission-success",
			authStorage,
			modelRegistry,
			settings: Settings.isolated(),
			modelOverride: `${fixtureModel.provider}/${fixtureModel.id}`,
			getApiKey: () => "FIXTURE_KEY",
			restrictToolNames: true,
			enableLsp: false,
			enableIrc: false,
			enableMCP: false,
			rules: [],
			skills: [],
			contextFiles: [],
			promptTemplates: [],
			preloadedExtensionPaths: [],
			preloadedCustomToolPaths: [],
			maxRuntimeMs: 5000,
			onNativeStageHandoff: async () => {
				handoffCalls++;
			},
		});

		expect(handoffCalls).toBe(1);
		expect(providerCalls).toBe(1);
		expect(String(result.error)).toContain("TERMINAL_SENTINEL_DISPATCH");
	});
});
