import { afterEach, expect, it, vi } from "bun:test";
import { Type } from "@oh-my-pi/omptype/typebox";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { ExtensionRuntime } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { ExtensionRunner } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/runner";
import { ExtensionToolWrapper } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/wrapper";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";

afterEach(() => vi.restoreAllMocks());

it("refuses unattended exec approval without prompting or waiting (L-RT-02/03/04, L-CB-03)", async () => {
	const authStorage = await AuthStorage.create(":memory:");
	try {
		const modelRegistry = new ModelRegistry(authStorage);
		const sessionManager = SessionManager.inMemory(process.cwd());
		const settings = Settings.isolated({ "tools.approvalMode": "write" });
		const runner = new ExtensionRunner(
			[],
			new ExtensionRuntime(),
			process.cwd(),
			sessionManager,
			modelRegistry,
			undefined,
			settings,
		);
		// A prompt would never resolve: the headless path must reject without asking.
		const select = vi
			.spyOn(runner.getUIContext(), "select")
			.mockImplementation(() => Promise.withResolvers<string | undefined>().promise);
		const execute = vi.fn(async () => ({ content: [{ type: "text" as const, text: "executed" }] }));
		const wrapper = new ExtensionToolWrapper(
			{
				name: "dangerous_tool",
				label: "Dangerous Tool",
				description: "Exec tool requiring approval",
				parameters: Type.Object({}),
				approval: "exec",
				execute,
			},
			runner,
		);

		expect(runner.hasUI()).toBe(false);
		await expect(
			wrapper.execute("call-unattended", {}, undefined, undefined, {
				sessionManager,
				modelRegistry,
				model: undefined,
				isIdle: () => true,
				hasQueuedMessages: () => false,
				abort: () => {},
				hasUI: false,
				settings,
			}),
		).rejects.toThrow('Tool "dangerous_tool" requires approval but no interactive UI available.');
		expect(select).not.toHaveBeenCalled();
		expect(execute).not.toHaveBeenCalled();
	} finally {
		authStorage.close();
	}
}, 1_000);
