import { afterEach, describe, expect, it } from "bun:test";
import type { Api, Context, Model, ModelSpec } from "@oh-my-pi/pi-ai";
import { clearCustomApis, registerCustomApi } from "@oh-my-pi/pi-ai";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { buildModel } from "@oh-my-pi/pi-catalog/build";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { createAgentSession } from "@oh-my-pi/pi-coding-agent/sdk";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { MAX_STAGE_CONTEXT_BYTES, loadStageContext } from "@oh-my-pi/pi-coding-agent/session/stage-context";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { TempDir } from "@oh-my-pi/pi-utils";
import { createAssistantMessage } from "./helpers/agent-session-setup";

describe("loadStageContext", () => {
	it("is off when no file is configured", async () => {
		expect(await loadStageContext(undefined)).toBeUndefined();
	});

	it("fails loud when the configured file is missing, empty or oversized", async () => {
		using dir = TempDir.createSync("@pi-stage-context-");
		await Bun.write(dir.join("empty.md"), "  \n");
		await Bun.write(dir.join("big.md"), "x".repeat(MAX_STAGE_CONTEXT_BYTES + 1));
		await expect(loadStageContext(dir.join("missing.md"))).rejects.toThrow();
		await expect(loadStageContext(dir.join("empty.md"))).rejects.toThrow("empty");
		await expect(loadStageContext(dir.join("big.md"))).rejects.toThrow("exceeds");
	});

	it("returns the trimmed digest text", async () => {
		using dir = TempDir.createSync("@pi-stage-context-");
		await Bun.write(dir.join("ctx.md"), "\nSTAGE s03 of OMP-1\n");
		expect(await loadStageContext(dir.join("ctx.md"))).toBe("STAGE s03 of OMP-1");
	});
});

describe("stage context on the provider wire", () => {
	const sessions: Array<{ dispose(): Promise<void> }> = [];
	const saved = process.env.PI_STAGE_CONTEXT_FILE;

	afterEach(async () => {
		clearCustomApis();
		if (saved === undefined) delete process.env.PI_STAGE_CONTEXT_FILE;
		else process.env.PI_STAGE_CONTEXT_FILE = saved;
		for (const session of sessions.splice(0)) await session.dispose();
	});

	async function firstRequestText(contextFile: string | undefined, id: string): Promise<string> {
		using tempDir = TempDir.createSync("@pi-stage-context-wire-");
		if (contextFile === undefined) delete process.env.PI_STAGE_CONTEXT_FILE;
		else {
			await Bun.write(tempDir.join("ctx.md"), contextFile);
			process.env.PI_STAGE_CONTEXT_FILE = tempDir.join("ctx.md");
		}
		const api = `test-${id}`;
		const contexts: Context[] = [];
		registerCustomApi(api, (_model, context) => {
			contexts.push(context);
			const stream = new AssistantMessageEventStream();
			queueMicrotask(() => {
				const message = createAssistantMessage("ok");
				stream.push({ type: "text_delta", contentIndex: 0, delta: "ok", partial: message });
				stream.push({ type: "done", reason: "stop", message });
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
			contextWindow: 4096,
			maxTokens: 1024,
		} as ModelSpec<Api>) as Model<Api>;
		const authStorage = await AuthStorage.create(tempDir.join("auth.db"));
		authStorage.keys.setRuntime(model.provider, "test-key");
		try {
			const { session } = await createAgentSession({
				cwd: tempDir.path(),
				agentDir: tempDir.path(),
				sessionManager: SessionManager.inMemory(tempDir.path()),
				authStorage,
				modelRegistry: new ModelRegistry(authStorage, tempDir.join("models.yml")),
				settings: Settings.isolated({ "compaction.enabled": false }),
				model,
				disableExtensionDiscovery: true,
				skills: [],
				contextFiles: [],
				promptTemplates: [],
				slashCommands: [],
				enableMCP: false,
				enableLsp: false,
				skipPythonPreflight: true,
				taskDepth: 1,
				agentId: "SubAgent",
			});
			sessions.push(session);
			await session.sendUserMessage("first");
			expect(contexts).toHaveLength(1);
			const content = contexts[0]!.messages[0]!.content;
			return typeof content === "string" ? content : JSON.stringify(content);
		} finally {
			authStorage.close();
		}
	}

	it("puts the compiled stage context in the first provider request when the flag is on", async () => {
		const text = await firstRequestText("LEDGER DIGEST: stage s03 inputs", "stage-ctx-on");
		expect(text).toContain("LEDGER DIGEST: stage s03 inputs");
		expect(text).toContain("first");
	});

	it("leaves the first provider request without stage context when the flag is off", async () => {
		const text = await firstRequestText(undefined, "stage-ctx-off");
		expect(text).not.toContain("Stage context");
	});
});
