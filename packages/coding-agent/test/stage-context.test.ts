import { afterEach, describe, expect, it, vi } from "bun:test";
import type { Api, Context, Model, ModelSpec } from "@oh-my-pi/pi-ai";
import { clearCustomApis, registerCustomApi } from "@oh-my-pi/pi-ai";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as ai from "@oh-my-pi/pi-ai";
import * as task from "@oh-my-pi/pi-coding-agent/task";
import { prepareNativeAuditRunner } from "../../../session-system/extensions/workflow/auditor-runner";
import { stageContextLines } from "../../../session-system/extensions/workflow/stage-context";
import { payloadHash, type CloseAttempt, type WorkflowView } from "@oh-my-pi/pi-work-client";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { MAX_STAGE_CONTEXT_BYTES, compileStageContext } from "@oh-my-pi/pi-coding-agent/session/stage-context";
import { TempDir } from "@oh-my-pi/pi-utils";
import { createAssistantMessage } from "./helpers/agent-session-setup";

describe("compileStageContext", () => {
	it("does not read the source when disabled", async () => {
		const compile = vi.fn(async () => {
			throw new Error("must not read");
		});
		expect(await compileStageContext(compile, false)).toBeUndefined();
		expect(compile).not.toHaveBeenCalled();
	});
	it("refuses launch if the promised context cannot compile", async () => {
		await expect(compileStageContext(undefined, true)).rejects.toThrow("compiler is required");
		await expect(
			compileStageContext(async () => {
				throw new Error("ledger unavailable");
			}, true),
		).rejects.toThrow("ledger unavailable");
		await expect(compileStageContext(async () => "  ", true)).rejects.toThrow("empty");
		await expect(compileStageContext(async () => "x".repeat(MAX_STAGE_CONTEXT_BYTES + 1), true)).rejects.toThrow(
			"exceeds",
		);
	});
});

describe("stage context on the provider wire", () => {
	afterEach(() => {
		clearCustomApis();
		vi.restoreAllMocks();
	});

	async function firstRequestText(enabled: boolean, id: string, flag = "OMP_STAGE_CONTEXT_PUSH"): Promise<string> {
		using tempDir = TempDir.createSync("@pi-stage-context-wire-");
		const savedOmp = process.env.OMP_STAGE_CONTEXT_PUSH;
		const savedPi = process.env.PI_STAGE_CONTEXT_PUSH;
		const api = `test-${id}`;
		const contexts: Context[] = [];
		registerCustomApi(api, (_model, context) => {
			contexts.push(context);
			const stream = new AssistantMessageEventStream();
			queueMicrotask(() => {
				const message = createAssistantMessage("ok");
				if (contexts.length === 1) {
					stream.push({ type: "text_delta", contentIndex: 0, delta: "ok", partial: message });
					stream.push({ type: "done", reason: "stop", message });
					return;
				}
				message.content = [
					{ type: "toolCall", id: "yield-result", name: "yield", arguments: { data: { report: "PASS" } } },
				];
				message.stopReason = "toolUse";
				const toolCall = message.content[0];
				if (toolCall.type !== "toolCall") throw new Error("Expected tool call");
				stream.push({ type: "toolcall_start", contentIndex: 0, partial: message });
				stream.push({ type: "toolcall_end", contentIndex: 0, toolCall, partial: message });
				stream.push({ type: "done", reason: "toolUse", message });
			});
			return stream;
		});
		const model = buildModel({
			id,
			name: id,
			api,
			provider: "managed-primary",
			baseUrl: "http://127.0.0.1:8080/v1",
			reasoning: false,
			input: ["text"],
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
			contextWindow: 16000,
			maxTokens: 1024,
		} as ModelSpec<Api>) as Model<Api>;
		const authStorage = await AuthStorage.create(tempDir.join("auth.db"));
		authStorage.keys.setRuntime(model.provider, "test-key");
		const settings = Settings.isolated({ "compaction.enabled": false });
		settings.setModelRole("audit", `${model.provider}/${model.id}`);
		const registry = new ModelRegistry(authStorage, tempDir.join("models.yml"));
		vi.spyOn(registry, "getAvailable").mockReturnValue([model]);
		vi.spyOn(registry, "find").mockReturnValue(model);
		vi.spyOn(registry, "refresh").mockResolvedValue();
		vi.spyOn(Settings, "loadReadOnly").mockResolvedValue(settings);
		vi.spyOn(ai, "completeSimple").mockResolvedValue(createAssistantMessage("OK"));
		vi.spyOn(task, "discoverAgents").mockResolvedValue({
			agents: [
				{
					name: "auditor",
					description: "test",
					systemPrompt: "Audit",
					tools: ["yield"],
					output: { type: "object", properties: { report: { type: "string" } }, required: ["report"] },
					source: "bundled",
				},
			],
			projectAgentsDir: null,
		});
		const compile = vi.fn(async () => {
			await Bun.write(tempDir.join("context.json"), JSON.stringify({ command: ["stage-compiler"] }));
			const workflow = {
				item: { revision: { revision_id: "rev-1" } },
				receipts: [],
				close_attempts: [],
			} as unknown as WorkflowView;
			const text = "INPUT: current revision rev-1";
			const lines = await stageContextLines(
				{ key: "OMP-1", cwd: tempDir.path() },
				{
					workflow: async () => workflow,
					liveAttempt: () => ({ attempt_id: "attempt-1" }) as CloseAttempt,
					env: { OMP_KNOWLEDGE_CONFIG_DIR: tempDir.path() },
					run: async (_argv, stdin) => {
						expect(JSON.parse(stdin).stage).toBe("audit");
						expect(JSON.parse(stdin).workflow.item.revision.revision_id).toBe("rev-1");
						return {
							exitCode: 0,
							stderr: "",
							stdout: JSON.stringify({
								text,
								bundle_sha256: payloadHash(text),
								bundle_id: "bundle-1",
								stage: "audit",
								tokens: 10,
								token_budget: 100,
							}),
						};
					},
				},
			);
			return ["LEDGER DIGEST: stage s03 inputs", ...lines].join("\n");
		});
		try {
			delete process.env.OMP_STAGE_CONTEXT_PUSH;
			delete process.env.PI_STAGE_CONTEXT_PUSH;
			if (enabled) process.env[flag] = "1";
			const ctx = {
				cwd: tempDir.path(),
				taskDepth: 0,
				models: { resolve: () => model },
				modelRegistry: registry,
			} as unknown as ExtensionContext;
			const runner = await prepareNativeAuditRunner(ctx, undefined, compile);
			const result = await runner("first", id);
			expect(result.error).toBeUndefined();
			expect(result.started).toBe(true);
			expect(compile).toHaveBeenCalledTimes(enabled ? 1 : 0);
			expect(contexts).toHaveLength(2);
			for (const context of contexts) {
				expect(JSON.stringify(context).split("STAGE CONTEXT audit bundle=bundle-1").length - 1).toBe(
					enabled ? 1 : 0,
				);
			}
			return JSON.stringify(contexts[0]);
		} finally {
			authStorage.close();
			if (savedOmp === undefined) delete process.env.OMP_STAGE_CONTEXT_PUSH;
			else process.env.OMP_STAGE_CONTEXT_PUSH = savedOmp;
			if (savedPi === undefined) delete process.env.PI_STAGE_CONTEXT_PUSH;
			else process.env.PI_STAGE_CONTEXT_PUSH = savedPi;
		}
	}

	it("puts the compiled stage context in the first provider request when the flag is on", async () => {
		const text = await firstRequestText(true, "stage-ctx-on");
		expect(text).toContain("LEDGER DIGEST: stage s03 inputs");
		expect(text).toContain("STAGE CONTEXT audit bundle=bundle-1");
		expect(text).toContain("INPUT: current revision rev-1");
		expect(text).toContain("first");
	});

	it("leaves the first provider request without stage context when the flag is off", async () => {
		const text = await firstRequestText(false, "stage-ctx-off");
		expect(text).not.toContain("STAGE CONTEXT");
		expect(text).not.toContain("LEDGER DIGEST");
	});
	it("accepts the PI flag alias for the native launch", async () => {
		const text = await firstRequestText(true, "stage-ctx-alias", "PI_STAGE_CONTEXT_PUSH");
		expect(text).toContain("STAGE CONTEXT audit bundle=bundle-1");
	});
});
