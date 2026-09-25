import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { AuthStorage } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as discovery from "@oh-my-pi/pi-coding-agent/discovery";
import { createAgentSession } from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { TempDir } from "@oh-my-pi/pi-utils";

const OMP248_SYSTEM_MARKER = "OMP248_SYSTEM_MARKER";

async function createContextSession(
	cwd: string,
	settings: Settings,
	options: {
		systemPrompt?: ((defaultPrompt: string[]) => string | string[]) | string | string[];
	} = {},
): Promise<{ session: AgentSession; authStorage: AuthStorage; sessionManager: SessionManager }> {
	const ompDir = path.join(cwd, ".omp");
	fs.mkdirSync(ompDir, { recursive: true });
	await Bun.write(path.join(ompDir, "SYSTEM.md"), OMP248_SYSTEM_MARKER);

	const authStorage = await AuthStorage.create(`${cwd}/auth.db`);
	const model = getBundledModel("openai", "gpt-4o-mini");
	const modelRegistry = new ModelRegistry(authStorage, `${cwd}/models.json`);
	const sessionManager = SessionManager.inMemory(cwd);
	const { session } = await createAgentSession({
		cwd,
		agentDir: cwd,
		modelRegistry,
		sessionManager,
		settings,
		model,
		disableExtensionDiscovery: true,
		promptTemplates: [],
		slashCommands: [],
		enableMCP: false,
		enableLsp: false,
		toolNames: ["read", "grep"],
		restrictToolNames: true,
		skipPythonPreflight: true,
		...(options.systemPrompt !== undefined ? { systemPrompt: options.systemPrompt } : {}),
	});
	return { session, authStorage, sessionManager };
}

describe("instruction-prep degradation reporting on AgentSession", () => {
	const originalLoadCapability = discovery.loadCapability;
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
	});

	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("contains marker and reports empty degradations when delay is 0", async () => {
		delayMs = 0;
		using tempDir = TempDir.createSync("@omp-prep-degradation-1-");
		const { session, authStorage } = await createContextSession(tempDir.path(), Settings.isolated({}));

		try {
			expect(session.agent.state.systemPrompt.join("\n")).toContain(OMP248_SYSTEM_MARKER);
			expect(session.instructionPrepDegradations).toEqual([]);
		} finally {
			await session.dispose();
			authStorage.close();
		}
	}, 30000);

	it("omits marker and reports timeout degradation when delay is 6000", async () => {
		delayMs = 6000;
		using tempDir = TempDir.createSync("@omp-prep-degradation-2-");
		const { session, authStorage } = await createContextSession(tempDir.path(), Settings.isolated({}));

		try {
			expect(session.agent.state.systemPrompt.join("\n")).not.toContain(OMP248_SYSTEM_MARKER);
			expect(session.instructionPrepDegradations).toEqual([{ source: "loadSystemPromptFiles", cause: "timeout" }]);
			await Bun.sleep(1500);
		} finally {
			await session.dispose();
			authStorage.close();
		}
	}, 30000);

	it("reports timeout degradation with systemPrompt override when delay is 6000", async () => {
		delayMs = 6000;
		using tempDir = TempDir.createSync("@omp-prep-degradation-3-");
		const { session, authStorage } = await createContextSession(tempDir.path(), Settings.isolated({}), {
			systemPrompt: p => p,
		});

		try {
			expect(session.agent.state.systemPrompt.join("\n")).not.toContain(OMP248_SYSTEM_MARKER);
			expect(session.instructionPrepDegradations).toEqual([{ source: "loadSystemPromptFiles", cause: "timeout" }]);
			await Bun.sleep(1500);
		} finally {
			await session.dispose();
			authStorage.close();
		}
	}, 30000);

	it("clears degradations and restores marker when tool rebuild occurs after timeout", async () => {
		delayMs = 6000;
		using tempDir = TempDir.createSync("@omp-prep-degradation-4-");
		const { session, authStorage } = await createContextSession(tempDir.path(), Settings.isolated({}));

		try {
			expect(session.agent.state.systemPrompt.join("\n")).not.toContain(OMP248_SYSTEM_MARKER);
			expect(session.instructionPrepDegradations).toEqual([{ source: "loadSystemPromptFiles", cause: "timeout" }]);
			await Bun.sleep(1500);

			delayMs = 0;
			await session.setActiveToolsByName(["read"]);

			expect(session.agent.state.systemPrompt.join("\n")).toContain(OMP248_SYSTEM_MARKER);
			expect(session.instructionPrepDegradations).toEqual([]);
		} finally {
			await session.dispose();
			authStorage.close();
		}
	}, 30000);
});
