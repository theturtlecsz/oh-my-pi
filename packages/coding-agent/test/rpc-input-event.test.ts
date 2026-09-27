/**
 * RPC `prompt` must run the message through extension `input` handlers, the
 * same pipeline the interactive editor uses (`emitInput(text, images,
 * "interactive")`). A workflow-host extension pauses an active execution grant
 * in that handler, so RPC input that skips it leaves an owner-typed text
 * unhandled. `applyRpcPromptInput` is the RPC entry to that pipeline.
 */
import { afterAll, beforeAll, describe, expect, it, vi } from "bun:test";
import * as path from "node:path";
import type { ImageContent } from "@oh-my-pi/pi-ai";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { ExtensionRuntime, loadExtensionFromFactory } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import type { Extension, InputEvent, InputEventResult } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/types";
import { applyRpcPromptInput } from "@oh-my-pi/pi-coding-agent/modes/rpc/rpc-mode";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import { TempDir } from "@oh-my-pi/pi-utils";

describe("applyRpcPromptInput", () => {
	let sharedTempDir: TempDir;
	let authStorage: AuthStorage;
	let modelRegistry: ModelRegistry;

	beforeAll(async () => {
		sharedTempDir = TempDir.createSync("@pi-rpc-input-event-");
		authStorage = await AuthStorage.create(path.join(sharedTempDir.path(), "testauth.db"));
		modelRegistry = new ModelRegistry(authStorage);
	});

	afterAll(() => {
		authStorage.close();
		sharedTempDir.removeSync();
	});

	const runnerWith = async (handler: (event: InputEvent) => InputEventResult | undefined): Promise<Extension> => {
		const runtime = new ExtensionRuntime();
		return loadExtensionFromFactory(
			pi => {
				pi.on("input", async (event: unknown) => handler(event as InputEvent));
			},
			sharedTempDir.path(),
			new EventBus(),
			runtime,
			"rpc-input-fixture",
		);
	};

	const makeRunner = (...extensions: Extension[]): ExtensionRunner =>
		new ExtensionRunner(
			extensions,
			new ExtensionRuntime(),
			sharedTempDir.path(),
			SessionManager.inMemory(),
			modelRegistry,
		);

	it("routes plain text through the handler with source rpc and yields the transformed text", async () => {
		const seen: InputEvent[] = [];
		const runner = makeRunner(
			await runnerWith(event => {
				seen.push(event);
				return { text: event.text.toUpperCase() };
			}),
		);

		const result = await applyRpcPromptInput(runner, "resume the plan", undefined);

		expect(seen).toHaveLength(1);
		expect(seen[0]?.source).toBe("rpc");
		expect(seen[0]?.text).toBe("resume the plan");
		expect(seen[0]?.originalText).toBe("resume the plan");
		expect(result).toEqual({ message: "RESUME THE PLAN", images: undefined, handled: false });
	});

	it("replaces images when the handler returns them", async () => {
		const replacement: ImageContent = { type: "image", data: "bmV3", mimeType: "image/png" };
		const runner = makeRunner(await runnerWith(() => ({ images: [replacement] })));

		const result = await applyRpcPromptInput(runner, "look at this", [
			{ type: "image", data: "b2xk", mimeType: "image/png" },
		]);

		expect(result.message).toBe("look at this");
		expect(result.images).toEqual([replacement]);
		expect(result.handled).toBe(false);
	});

	it("reports handled when the handler consumes the input", async () => {
		const runner = makeRunner(await runnerWith(() => ({ handled: true })));

		const result = await applyRpcPromptInput(runner, "pause the grant", undefined);

		expect(result).toEqual({ message: "pause the grant", images: undefined, handled: true });
	});

	it("does not call the handler for a slash command", async () => {
		const handler = vi.fn(() => ({}));
		const runner = makeRunner(await runnerWith(handler));

		const images: ImageContent[] = [{ type: "image", data: "b2xk", mimeType: "image/png" }];
		const result = await applyRpcPromptInput(runner, "/execute resume X", images);

		expect(handler).not.toHaveBeenCalled();
		expect(result).toEqual({ message: "/execute resume X", images, handled: false });
	});

	it("passes the message and images through when no handler is registered", async () => {
		const runner = makeRunner();

		const images: ImageContent[] = [{ type: "image", data: "b2xk", mimeType: "image/png" }];
		const result = await applyRpcPromptInput(runner, "plain owner text", images);

		expect(result).toEqual({ message: "plain owner text", images, handled: false });
	});

	it("passes through when there is no runner", async () => {
		const result = await applyRpcPromptInput(undefined, "plain owner text", undefined);

		expect(result).toEqual({ message: "plain owner text", images: undefined, handled: false });
	});
});
