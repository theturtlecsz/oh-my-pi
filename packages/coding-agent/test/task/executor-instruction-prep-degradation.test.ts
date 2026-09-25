import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { AuthStorage } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as discovery from "@oh-my-pi/pi-coding-agent/discovery";
import { AgentLifecycleManager } from "@oh-my-pi/pi-coding-agent/registry/agent-lifecycle";
import { AgentRegistry } from "@oh-my-pi/pi-coding-agent/registry/agent-registry";
import * as sdkModule from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSessionEvent } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { runSubprocess } from "@oh-my-pi/pi-coding-agent/task/executor";
import type { AgentDefinition } from "@oh-my-pi/pi-coding-agent/task/types";
import { TempDir } from "@oh-my-pi/pi-utils";

const OMP248_SYSTEM_MARKER = "OMP248_SYSTEM_MARKER";

const baseAgent: AgentDefinition = {
	name: "task",
	description: "test",
	systemPrompt: "test",
	source: "bundled",
};

describe("executor instruction-prep degradation reporting", () => {
	const originalLoadCapability = discovery.loadCapability;
	const realCreate = sdkModule.createAgentSession;
	let delayMs = 0;

	beforeEach(() => {
		delayMs = 0;
		vi.spyOn(discovery, "loadCapability").mockImplementation(async (id, options) => {
			if (id === "system-prompt") {
				if (delayMs > 0) {
					await Bun.sleep(delayMs);
				}
				return await originalLoadCapability(id, options);
			}
			return await originalLoadCapability(id, options);
		});

		vi.spyOn(ModelRegistry.prototype, "refresh").mockResolvedValue(undefined);

		vi.spyOn(sdkModule, "createAgentSession").mockImplementation(async opts => {
			const result = await realCreate({
				...opts,
				enableMCP: false,
				disableExtensionDiscovery: true,
				skipPythonPreflight: true,
				enableLsp: false,
			});
			const session = result.session;
			const listeners: Array<(event: AgentSessionEvent) => void> = [];
			const originalSubscribe = session.subscribe.bind(session);
			vi.spyOn(session, "subscribe").mockImplementation((listener: (event: AgentSessionEvent) => void) => {
				listeners.push(listener);
				const unsubscribe = originalSubscribe(listener);
				return () => {
					const idx = listeners.indexOf(listener);
					if (idx >= 0) listeners.splice(idx, 1);
					unsubscribe();
				};
			});
			vi.spyOn(session, "prompt").mockImplementation(async () => {
				for (const listener of [...listeners]) {
					listener({
						type: "tool_execution_end",
						toolCallId: "tool-pass-through",
						toolName: "yield",
						result: {
							content: [{ type: "text", text: "Result submitted." }],
							details: { status: "success", data: { ok: true } },
						},
						isError: false,
					} as AgentSessionEvent);
				}
				return true;
			});
			return result;
		});
	});

	afterEach(async () => {
		vi.restoreAllMocks();
		AgentLifecycleManager.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
	});

	async function runDegradationTest(options: { delay: number; id: string }) {
		delayMs = options.delay;
		using tempDir = TempDir.createSync("@pi-task-prep-degradation-");
		const projectDir = tempDir.join("project");
		fs.mkdirSync(projectDir, { recursive: true });
		const ompDir = path.join(projectDir, ".omp");
		fs.mkdirSync(ompDir, { recursive: true });
		fs.writeFileSync(path.join(ompDir, "SYSTEM.md"), OMP248_SYSTEM_MARKER);

		const artifactsDir = tempDir.join("artifacts");
		fs.mkdirSync(artifactsDir, { recursive: true });

		const authStorage = await AuthStorage.create(tempDir.join("auth.db"));
		const model = getBundledModel("openai", "gpt-4o-mini");
		authStorage.setRuntimeApiKey(model.provider, "test-api-key");
		const modelRegistry = new ModelRegistry(authStorage, tempDir.join("models.json"));

		const settings = Settings.isolated();
		settings.setModelRole("default", `${model.provider}/${model.id}`);

		const stderrChunks: string[] = [];
		const stderrSpy = vi.spyOn(process.stderr, "write").mockImplementation((chunk: unknown) => {
			stderrChunks.push(String(chunk));
			return true;
		});

		try {
			const result = await runSubprocess({
				cwd: projectDir,
				agent: baseAgent,
				task: "test task",
				index: 0,
				id: options.id,
				authStorage,
				modelRegistry,
				settings,
				artifactsDir,
				contextFiles: [],
				skills: [],
				enableLsp: false,
			});

			const peek = await SessionManager.peekSessionInit(path.join(artifactsDir, `${options.id}.jsonl`));

			return {
				result,
				init: peek?.init ?? null,
				stderr: stderrChunks.join(""),
			};
		} finally {
			if (options.delay > 0) {
				await Bun.sleep(1500);
			}
			await authStorage.close();
			stderrSpy.mockRestore();
		}
	}

	it("delay 0: persists marker in system prompt and omits instructionPrepDegradations", async () => {
		const { result, init } = await runDegradationTest({ delay: 0, id: "subagent-delay-0" });

		expect(result.exitCode).toBe(0);
		expect(init).toBeDefined();
		expect(init?.systemPrompt).toContain(OMP248_SYSTEM_MARKER);
		expect("instructionPrepDegradations" in init!).toBe(false);
	}, 40000);

	it("delay 6000: omits marker, persists timeout degradation, and names step on stderr", async () => {
		const { result, init, stderr } = await runDegradationTest({ delay: 6000, id: "subagent-delay-6000" });

		expect(result.exitCode).toBe(0);
		expect(init).toBeDefined();
		expect(init?.systemPrompt).not.toContain(OMP248_SYSTEM_MARKER);
		expect(init?.instructionPrepDegradations).toEqual([{ source: "loadSystemPromptFiles", cause: "timeout" }]);
		expect(stderr).toContain("loadSystemPromptFiles");
	}, 40000);
});
