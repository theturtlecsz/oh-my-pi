import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import { ModelRegistry } from "../src/config/model-registry";
import { ExtensionRuntime, loadExtensionFromFactory } from "../src/extensibility/extensions/loader";
import { ExtensionRunner } from "../src/extensibility/extensions/runner";
import type { ExtensionFactory, ToolCallEvent } from "../src/extensibility/extensions/types";
import type { TaskResultAuthorityContext, ToolCallEventResult } from "../src/extensibility/shared-events";
import { SessionManager } from "../src/session/session-manager";
import { EventBus } from "../src/utils/event-bus";
import { createInMemoryAuthStorage } from "./helpers/agent-session-setup";

let temp: TempDir;
let manager: SessionManager;
let closeAuth: (() => void) | undefined;

beforeEach(() => {
	temp = TempDir.createSync("@pi-task-authority-");
	manager = SessionManager.inMemory(temp.path());
});

afterEach(async () => {
	await manager.close();
	closeAuth?.();
	closeAuth = undefined;
	temp.removeSync();
});

async function makeRunner(factories: ExtensionFactory[]): Promise<ExtensionRunner> {
	const runtime = new ExtensionRuntime();
	const bus = new EventBus();
	const extensions = await Promise.all(
		factories.map((factory, index) =>
			loadExtensionFromFactory(factory, temp.path(), bus, runtime, `authority-${index}`),
		),
	);
	const auth = createInMemoryAuthStorage();
	closeAuth = () => auth.close();
	return new ExtensionRunner(
		extensions,
		runtime,
		temp.path(),
		manager,
		new ModelRegistry(auth, path.join(temp.path(), "models.yml")),
	);
}

function event(): ToolCallEvent {
	return {
		type: "tool_call",
		toolName: "task",
		toolCallId: "original-call",
		input: { agent: "task", task: "  inspect file  " },
		taskResultOrigin: { sessionId: manager.getSessionId(), promptEntryId: "original-prompt" },
	};
}

function context(): TaskResultAuthorityContext {
	return {
		sessionId: manager.getSessionId(),
		promptEntryId: "original-prompt",
		assistantEntryId: "original-assistant",
		toolCallId: "original-call",
	};
}

// Runner-consumer boundary only. Actual SDK qualification/ownership is covered
// by AgentSession tests; this makes an erased callback observable as an effect.
async function consume(result: ToolCallEventResult | undefined): Promise<boolean> {
	if (result?.block) return false;
	const decision = await result?.taskResultAuthority?.(context());
	if (decision && !decision.ok) return false;
	await Bun.write(path.join(temp.path(), "unexpected-effect"), "processed");
	return true;
}

describe("runtime task authority aggregation", () => {
	test("earlier denial survives later ordinary handler and transformed execution input", async () => {
		let validations = 0;
		const runner = await makeRunner([
			pi =>
				pi.on("tool_call", () => ({
					taskResultAuthority: async () => {
						validations++;
						return { ok: false, reason: "Execution authority revoked" };
					},
				})),
			pi => pi.on("tool_call", () => ({ block: false })),
			pi =>
				pi.on("tool_call", call => {
					const input = call.input as Record<string, unknown>;
					return { input: { ...input, task: String(input.task).trim().toUpperCase() } };
				}),
		]);
		const result = await runner.emitToolCall(event());
		expect(result?.input).toEqual({ agent: "task", task: "INSPECT FILE" });
		expect(await consume(result)).toBe(false);
		expect(validations).toBe(1);
		expect(await Bun.file(path.join(temp.path(), "unexpected-effect")).exists()).toBe(false);
	});

	test("multiple authorizers block rather than allowing the final validator to win", async () => {
		let validations = 0;
		const authorizer: ExtensionFactory = pi =>
			pi.on("tool_call", () => ({
				taskResultAuthority: async () => {
					validations++;
					return { ok: true };
				},
			}));
		const runner = await makeRunner([authorizer, authorizer]);
		const result = await runner.emitToolCall(event());
		expect(result?.block).toBe(true);
		expect(result?.reason).toContain("Multiple task result authority validators");
		expect(await consume(result)).toBe(false);
		expect(validations).toBe(0);
		expect(await Bun.file(path.join(temp.path(), "unexpected-effect")).exists()).toBe(false);
	});

	test("a serialized authority value from a runtime handler cannot downgrade to ordinary processing", async () => {
		// JS extensions can return invalid runtime shapes despite TypeScript's API.
		const malformed = { taskResultAuthority: { ok: true } } as unknown as ToolCallEventResult;
		const runner = await makeRunner([
			pi => pi.on("tool_call", () => malformed),
			pi => pi.on("tool_call", () => ({ block: false })),
		]);
		const result = await runner.emitToolCall(event());
		expect(result?.block).toBe(true);
		expect(result?.reason).toContain("runtime validator");
		expect(await consume(result)).toBe(false);
		expect(await Bun.file(path.join(temp.path(), "unexpected-effect")).exists()).toBe(false);
	});
});
