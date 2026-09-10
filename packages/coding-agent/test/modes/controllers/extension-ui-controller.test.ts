import { afterAll, afterEach, beforeAll, describe, expect, it, type Mock, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { scheduler } from "node:timers/promises";
import { Agent } from "@oh-my-pi/pi-agent-core";
import type { AssistantMessage } from "@oh-my-pi/pi-ai";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { Container, type OverlayOptions, setKeybindings } from "@oh-my-pi/pi-tui";
import { logger } from "@oh-my-pi/pi-utils";
import { KeybindingsManager } from "../../../src/config/keybindings";
import { ModelRegistry } from "../../../src/config/model-registry";
import { Settings } from "../../../src/config/settings";
import { TtsrManager } from "../../../src/export/ttsr";
import type { ExtensionAskDialogQuestion, ExtensionUIContext } from "../../../src/extensibility/extensions";
import { ExtensionRuntime, loadExtensionFromFactory } from "../../../src/extensibility/extensions/loader";
import { ExtensionRunner } from "../../../src/extensibility/extensions/runner";
import type { ExtensionHandler, MessageEndEvent } from "../../../src/extensibility/extensions/types";
import { AskDialogComponent } from "../../../src/modes/components/ask-dialog";
import { CustomEditor } from "../../../src/modes/components/custom-editor";
import { ExtensionUiController } from "../../../src/modes/controllers/extension-ui-controller";
import { getEditorTheme, getThemeByName, setThemeInstance } from "../../../src/modes/theme/theme";
import type { InteractiveModeContext } from "../../../src/modes/types";
import { AgentSession } from "../../../src/session/agent-session";
import { AuthStorage } from "../../../src/session/auth-storage";
import { USER_INTERRUPT_LABEL } from "../../../src/session/messages";
import { SessionManager } from "../../../src/session/session-manager";
import { EventBus } from "../../../src/utils/event-bus";

afterEach(() => {
	setKeybindings(KeybindingsManager.inMemory());
});

beforeAll(async () => {
	const dark = await getThemeByName("dark");
	if (!dark) throw new Error("Failed to load dark theme");
	setThemeInstance(dark);
});

function makeHarness() {
	const editor = new CustomEditor(getEditorTheme());
	const editorContainer = new Container();
	editorContainer.addChild(editor);
	const requestRender = vi.fn();
	const setFocus = vi.fn();
	const addAutocompleteProvider = vi.fn();
	const fakeHandle = {
		hide: vi.fn(),
		setHidden: vi.fn(),
		isHidden: vi.fn(() => false),
	};
	const showOverlay = vi.fn(() => fakeHandle);
	let uiContext: ExtensionUIContext | undefined;
	const ctx = {
		editor,
		ui: {
			requestRender,
			setFocus,
			showOverlay,
			terminal: { rows: 40 },
		},
		editorContainer,
		session: {
			extensionRunner: undefined,
			setUsageFallbackConfirmer: vi.fn(),
		},
		setToolUIContext(context: ExtensionUIContext, hasUI: boolean): void {
			expect(hasUI).toBe(true);
			uiContext = context;
		},
		addAutocompleteProvider,
		syncComposerShape: vi.fn(),
	} as unknown as InteractiveModeContext;

	const controller = new ExtensionUiController(ctx);

	return {
		editor,
		requestRender,
		addAutocompleteProvider,
		editorContainer,
		setFocus,
		showOverlay,
		fakeHandle,
		controller,
		async init(): Promise<ExtensionUIContext> {
			await controller.initHooksAndCustomTools();
			expect(uiContext).toBeDefined();
			return uiContext!;
		},
	};
}

describe("ExtensionUiController editor UI", () => {
	it("requests a render after extension pasteToEditor mutates the prompt", async () => {
		const harness = makeHarness();
		const ui = await harness.init();

		ui.pasteToEditor("hello");
		ui.pasteToEditor(" world");

		expect(harness.editor.getText()).toBe("hello world");
		expect(harness.requestRender).toHaveBeenCalledTimes(2);
	});

	it("requests a render after extension setEditorText replaces the prompt", async () => {
		const harness = makeHarness();
		const ui = await harness.init();

		ui.setEditorText("hello");

		expect(harness.editor.getText()).toBe("hello");
		expect(harness.requestRender).toHaveBeenCalledTimes(1);
	});

	it("keeps a populated prompt visible and routes input to it until the draft is cleared", async () => {
		const harness = makeHarness();
		harness.editor.setText("finish this wor");
		const questions: ExtensionAskDialogQuestion[] = [
			{ id: "confirm", question: "Continue?", options: [{ label: "Yes" }, { label: "No" }] },
		];

		const pending = harness.controller.showAskDialog(questions);
		const ask = harness.editorContainer.children[0];
		expect(ask).toBeInstanceOf(AskDialogComponent);
		expect(harness.editorContainer.children).toEqual([ask, harness.editor]);

		ask?.handleInput?.("d");
		expect(harness.editor.getText()).toBe("finish this word");

		harness.editor.setText("");
		ask?.handleInput?.("\n");
		expect(await pending).toEqual({
			kind: "submit",
			results: [
				{
					id: "confirm",
					question: "Continue?",
					options: ["Yes", "No"],
					multi: false,
					selectedOptions: ["Yes"],
					customInput: undefined,
					note: undefined,
					timedOut: undefined,
				},
			],
		});
		expect(harness.editorContainer.children).toEqual([harness.editor]);
	});

	it("does not fire editor-slot shortcuts that would orphan the ask dialog (#6738)", () => {
		const harness = makeHarness();
		harness.editor.setText("draft in progress");
		// Simulate an editor-slot shortcut like the Agent Hub binding, whose
		// handler clears editorContainer and would strand the pending ask.
		let hubOpened = false;
		harness.editor.setCustomKeyHandler("ctrl+s", () => {
			hubOpened = true;
			harness.editorContainer.clear();
		});
		const questions: ExtensionAskDialogQuestion[] = [
			{ id: "confirm", question: "Continue?", options: [{ label: "Yes" }, { label: "No" }] },
		];

		harness.controller.showAskDialog(questions);
		const ask = harness.editorContainer.children[0];
		expect(ask).toBeInstanceOf(AskDialogComponent);

		// Ctrl+S reaches the draft editor while ask is open; the shortcut must be
		// swallowed, the draft untouched, and the ask surface preserved.
		ask?.handleInput?.("\x13");
		expect(hubOpened).toBe(false);
		expect(harness.editor.getText()).toBe("draft in progress");
		expect(harness.editorContainer.children).toEqual([ask, harness.editor]);
	});

	it("exposes the draft editor cursor while it proxies input, and drops it once cleared (#6738)", () => {
		const harness = makeHarness();
		harness.editor.setText("finish this wor");
		const questions: ExtensionAskDialogQuestion[] = [
			{ id: "confirm", question: "Continue?", options: [{ label: "Yes" }, { label: "No" }] },
		];

		harness.controller.showAskDialog(questions);
		const ask = harness.editorContainer.children[0];
		expect(ask).toBeInstanceOf(AskDialogComponent);

		// The ask dialog holds TUI focus, but rendering it must mirror focus onto
		// the draft editor so its insertion cursor is visible.
		ask?.render?.(80);
		expect(harness.editor.focused).toBe(true);

		// Once the draft clears, the ask controls take over and the editor cursor
		// must not linger.
		harness.editor.setText("");
		ask?.render?.(80);
		expect(harness.editor.focused).toBe(false);
	});

	it("lets the clear action empty the draft and lift the ask guard (#6738)", () => {
		const harness = makeHarness();
		// Route Ctrl+C to the guard: keep app.clear on Ctrl+C but move the ask
		// cancel key off it, so Ctrl+C reaches draft editing instead of cancelling.
		setKeybindings(KeybindingsManager.inMemory({ "tui.select.cancel": "ctrl+g" }));
		harness.editor.setActionKeys("app.clear", ["ctrl+c"]);
		let cleared = 0;
		// Mirror interactive wiring: app.clear (Ctrl+C) clears the draft.
		harness.editor.onClear = () => {
			cleared++;
			harness.editor.setText("");
		};
		harness.editor.setText("half typed prompt");
		const questions: ExtensionAskDialogQuestion[] = [
			{ id: "confirm", question: "Continue?", options: [{ label: "Yes" }, { label: "No" }] },
		];

		harness.controller.showAskDialog(questions);
		const ask = harness.editorContainer.children[0];
		expect(ask).toBeInstanceOf(AskDialogComponent);

		// Ctrl+C is reserved by the base editor and never clears; the guard must
		// dispatch the configured clear action so the "finish or clear" hint works.
		ask?.handleInput?.("\x03");
		expect(cleared).toBe(1);
		expect(harness.editor.getText()).toBe("");

		// With the draft gone the guard releases: the next key reaches the ask
		// controls and submits the highlighted option.
		ask?.handleInput?.("\n");
		expect(harness.editorContainer.children).toEqual([harness.editor]);
	});

	it("remounts the draft editor when the ask surface is restored after a nested prompt (#6738)", async () => {
		const harness = makeHarness();
		harness.editor.setText("half typed prompt");
		const questions: ExtensionAskDialogQuestion[] = [
			{ id: "confirm", question: "Continue?", options: [{ label: "Yes" }, { label: "No" }] },
		];

		harness.controller.showAskDialog(questions);
		const ask = harness.editorContainer.children[0];
		expect(ask).toBeInstanceOf(AskDialogComponent);
		expect(harness.editorContainer.children).toEqual([ask, harness.editor]);

		// Draft submitted: the guard lifts and ask controls take input; open the
		// note prompt, which swaps the container to the nested editor.
		harness.editor.setText("");
		ask?.handleInput?.("n");
		const promptEditor = harness.editorContainer.children[0];
		expect(promptEditor).not.toBe(ask);

		// A failed async submission restores the draft while the nested prompt is
		// open, re-blocking the guard.
		harness.editor.setText("half typed prompt");

		// Cancelling the nested prompt restores the ask surface; the draft editor
		// must be remounted so routed input lands on a visible surface.
		promptEditor?.handleInput?.("\x1b");
		expect(harness.editorContainer.children).toEqual([ask, harness.editor]);
		// The dialog's prompt-active latch clears when the awaited onPrompt
		// promise settles; yield a microtask before routing the next key.
		await Promise.resolve();
		ask?.handleInput?.("!");
		expect(harness.editor.getText()).toBe("half typed prompt!");
	});

	it("bridges addAutocompleteProvider factories to the interactive mode context (#4919)", async () => {
		const harness = makeHarness();
		const ui = await harness.init();

		expect(typeof ui.addAutocompleteProvider).toBe("function");

		const factory = (current: unknown) => current as never;
		ui.addAutocompleteProvider(factory);

		expect(harness.addAutocompleteProvider).toHaveBeenCalledTimes(1);
		expect(harness.addAutocompleteProvider).toHaveBeenCalledWith(factory);
	});
});

describe("ExtensionUiController custom overlay", () => {
	// showHookCustom mounts the overlay in the `.then` of a Promise.try chain;
	// draining the microtask queue a few times settles it without real timers.
	const flushMicrotasks = async () => {
		for (let i = 0; i < 3; i++) await Promise.resolve();
	};

	it("forwards overlayOptions to showOverlay and invokes onHandle", async () => {
		const harness = makeHarness();
		const ui = await harness.init();
		const onHandle = vi.fn();
		const overlayOptions: OverlayOptions = {
			anchor: "bottom-center",
			width: "85%",
			maxHeight: "55%",
			margin: { bottom: 1, left: 2, right: 2 },
		};

		ui.custom<void>(() => new Container(), { overlay: true, overlayOptions, onHandle });

		await flushMicrotasks();
		expect(harness.showOverlay).toHaveBeenCalledTimes(1);
		expect(harness.showOverlay).toHaveBeenCalledWith(expect.any(Container), overlayOptions);
		expect(onHandle).toHaveBeenCalledTimes(1);
		expect(onHandle).toHaveBeenCalledWith(harness.fakeHandle);
	});

	it("resolves overlayOptions factories before showing the overlay", async () => {
		const harness = makeHarness();
		const ui = await harness.init();
		const overlayOptions: OverlayOptions = { anchor: "top-right", width: 40 };
		const resolveOverlayOptions = vi.fn(() => overlayOptions);

		ui.custom<void>(() => new Container(), {
			overlay: true,
			overlayOptions: resolveOverlayOptions,
		});

		await flushMicrotasks();
		expect(resolveOverlayOptions).toHaveBeenCalledTimes(1);
		expect(harness.showOverlay).toHaveBeenCalledWith(expect.any(Container), overlayOptions);
	});

	it("falls back to the full-cover defaults when overlayOptions is absent", async () => {
		const harness = makeHarness();
		const ui = await harness.init();

		ui.custom<void>(() => new Container(), { overlay: true });

		await flushMicrotasks();
		expect(harness.showOverlay).toHaveBeenCalledTimes(1);
		expect(harness.showOverlay).toHaveBeenCalledWith(expect.any(Container), {
			anchor: "bottom-center",
			width: "100%",
			maxHeight: "100%",
			margin: 0,
		});
	});

	it("rejects and restores the editor when a custom factory fails", async () => {
		const harness = makeHarness();
		const ui = await harness.init();
		const failure = new Error("custom factory failed");

		await expect(ui.custom(() => Promise.reject(failure))).rejects.toBe(failure);

		expect(harness.editorContainer.children).toEqual([harness.editor]);
		expect(harness.setFocus).toHaveBeenLastCalledWith(harness.editor);
	});

	it("aborts a pending custom factory and disposes its late component", async () => {
		const harness = makeHarness();
		const ui = await harness.init();
		harness.editor.setText("draft before factory");
		const controller = new AbortController();
		const factory = Promise.withResolvers<Container>();
		const component = new Container() as Container & { dispose: Mock<() => void> };
		component.dispose = vi.fn();

		const pending = ui.custom(() => factory.promise, { signal: controller.signal });
		harness.editor.setText("draft typed while factory is pending");
		controller.abort();

		await expect(pending).rejects.toBe(controller.signal.reason);
		factory.resolve(component);
		await flushMicrotasks();

		expect(component.dispose).toHaveBeenCalledTimes(1);
		expect(harness.editorContainer.children).toEqual([harness.editor]);
		expect(harness.editor.getText()).toBe("draft typed while factory is pending");
	});
});

describe("ExtensionUiController real hook abort boundary", () => {
	let directory: string;
	let auth: AuthStorage;
	let registry: ModelRegistry;
	const sessions: AgentSession[] = [];

	beforeAll(async () => {
		directory = await fs.mkdtemp(path.join(os.tmpdir(), "interactive-abort-contract-"));
		auth = await AuthStorage.create(path.join(directory, "auth.db"));
		auth.setRuntimeApiKey("anthropic", "test-key");
		registry = new ModelRegistry(auth, path.join(directory, "models.yml"));
	});
	afterEach(async () => {
		vi.restoreAllMocks();
		for (const session of sessions.splice(0)) await session.dispose();
	});
	afterAll(async () => {
		auth.close();
		await fs.rm(directory, { recursive: true, force: true });
	});

	function message(text: string, stopReason: "stop" | "aborted" = "stop"): AssistantMessage {
		return {
			role: "assistant",
			content: [{ type: "text", text }],
			api: "anthropic-messages",
			provider: "anthropic",
			model: "mock",
			stopReason,
			timestamp: 1720000000000,
			usage: {
				input: 0,
				output: 0,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 0,
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			},
		};
	}

	async function realHarness(route: "initial" | "rebind", handler: ExtensionHandler<MessageEndEvent>) {
		const ttsrManager = new TtsrManager({
			enabled: true,
			contextMode: "discard",
			interruptMode: "always",
			repeatMode: "once",
			repeatGap: 10,
		});
		ttsrManager.addRule({
			name: "no-unwrap",
			path: "/fixture/no-unwrap.md",
			content: "Avoid unwrap",
			condition: ["\\.unwrap\\("],
			_source: { provider: "test", providerName: "test", path: "/fixture/no-unwrap.md", level: "project" },
		});
		let streams = 0;
		const agent = new Agent({
			initialState: { model: getBundledModel("anthropic", "claude-sonnet-4-5")!, tools: [] },
			getApiKey: () => "test-key",
			streamFn: (_model, _context, options) => {
				streams++;
				const stream = new AssistantMessageEventStream();
				queueMicrotask(() => {
					const partial = message("result.unwrap(");
					options?.signal?.addEventListener(
						"abort",
						() => {
							stream.push({ type: "error", reason: "aborted", error: message("result.unwrap(", "aborted") });
						},
						{ once: true },
					);
					stream.push({ type: "start", partial });
					stream.push({ type: "text_delta", contentIndex: 0, delta: "result.unwrap(", partial });
				});
				return stream;
			},
		});
		const manager = SessionManager.inMemory();
		const runtime = new ExtensionRuntime();
		const extension = await loadExtensionFromFactory(
			pi => pi.on("message_end", handler),
			directory,
			new EventBus(),
			runtime,
			"interactive-abort-contract",
		);
		const runner = new ExtensionRunner([extension], runtime, directory, manager, registry);
		const session = new AgentSession({
			agent,
			sessionManager: manager,
			modelRegistry: registry,
			extensionRunner: runner,
			ttsrManager,
			settings: Settings.isolated({ "compaction.enabled": false, "retry.enabled": false }),
		});
		sessions.push(session);
		const editor = new CustomEditor(getEditorTheme());
		const editorContainer = new Container();
		editorContainer.addChild(editor);
		const showError = vi.fn((_error: string) => {});
		const ctx = {
			session,
			sessionManager: manager,
			editor,
			editorContainer,
			showError,
			ui: { requestRender: vi.fn(), setFocus: vi.fn(), terminal: { rows: 40 } },
			setToolUIContext: vi.fn(),
			syncComposerShape: vi.fn(),
			present: vi.fn(),
		} as unknown as InteractiveModeContext;
		const controller = new ExtensionUiController(ctx);
		await controller.initHooksAndCustomTools();
		if (route === "rebind") controller.initializeHookRunner(controller.getToolUIContext()!, true);
		return { session, agent, manager, runner, showError, streams: () => streams };
	}

	for (const route of ["initial", "rebind"] as const) {
		for (const form of ["await", "return"] as const) {
			it(`${route} ${form} hook cancels synchronously and drains ordered records without a TTSR cycle`, async () => {
				const retryElapsed = Promise.withResolvers<void>();
				const wait = scheduler.wait.bind(scheduler);
				vi.spyOn(scheduler, "wait").mockImplementation(async (delay, options) => {
					await wait(delay, options);
					if (delay === 50) retryElapsed.resolve();
				});
				const entered = Promise.withResolvers<void>();
				const release = Promise.withResolvers<void>();
				const finished = Promise.withResolvers<void>();
				const abortResults: unknown[] = [];
				const abortPromises: Promise<void>[] = [];
				let abortDrained = false;
				const abortHandler: ExtensionHandler<MessageEndEvent> =
					form === "await"
						? async (_event, ctx) => {
								const result = ctx.abort();
								abortResults.push(result);
								await result;
							}
						: (_event, ctx) => ctx.abort();
				const f = await realHarness(route, async (event, ctx) => {
					if (event.message.role !== "assistant") return;
					entered.resolve();
					await release.promise;
					const result = abortHandler(event, ctx);
					if (form === "return") abortResults.push(result);
					expect(coreAbort).toHaveBeenLastCalledWith(USER_INTERRUPT_LABEL);
					expect(f.session.isTtsrAbortPending).toBe(false);
					expect(abortDrained).toBe(false);
					await result;
					finished.resolve();
				});
				const coreAbort = vi.spyOn(f.agent, "abort");
				const realAbort = f.session.abort.bind(f.session);
				vi.spyOn(f.session, "abort").mockImplementation(options => {
					const pending = realAbort(options);
					abortPromises.push(pending);
					void pending.then(() => {
						abortDrained = true;
					});
					return pending;
				});
				const prompt = f.session.prompt("Write Rust code");
				await entered.promise;
				// Keep the real retry timer pending behind the supported extension handler.
				await retryElapsed.promise;
				expect(f.streams()).toBe(1);
				expect(
					f.manager.getEntries().some(entry => entry.type === "message" && entry.message.role === "assistant"),
				).toBe(false);
				release.resolve();
				await finished.promise;
				await prompt;
				await Promise.all(abortPromises);
				await f.session.waitForIdle();
				expect(abortResults).toEqual([undefined]);
				expect(abortPromises).toHaveLength(1);
				expect(abortDrained).toBe(true);
				expect(
					f.manager.getEntries().find(entry => entry.type === "message" && entry.message.role === "assistant"),
				).toMatchObject({ type: "message", message: { stopReason: "aborted", timestamp: 1720000000000 } });
				expect(f.streams()).toBe(1);
				expect(f.agent.state.isStreaming).toBe(false);
				expect(
					f.manager
						.getEntries()
						.filter(entry => entry.type === "message")
						.map(entry => entry.message.role),
				).toEqual(["user", "assistant"]);
				expect(
					f.manager
						.getEntries()
						.some(entry => entry.type === "custom_message" && entry.customType === "ttsr-injection"),
				).toBe(false);
				expect(f.showError).not.toHaveBeenCalled();
			});
		}
		for (const [faultKind, fault] of [
			["Error", new Error("abort dependency failed")],
			["non-Error", "abort dependency failed"],
			["null-prototype", Object.assign(Object.create(null), { message: "abort dependency failed" })],
		] as const) {
			for (const reporterKind of ["normal", "Error", "null-prototype"] as const) {
				it(`${route} reports ${faultKind} abort rejection with ${reporterKind} UI reporter`, async () => {
					const returned: unknown[] = [];
					const f = await realHarness(route, (_event, ctx) => {
						returned.push(ctx.abort());
					});
					const realAbort = f.session.abort.bind(f.session);
					// Controlled dependency fault after the real ordinary abort has drained.
					vi.spyOn(f.session, "abort").mockImplementation(async options => {
						await realAbort(options);
						throw fault;
					});
					const reported = Promise.withResolvers<void>();
					f.showError.mockImplementation(() => {
						if (reporterKind === "Error") throw new Error("UI reporter failed");
						if (reporterKind === "null-prototype")
							throw Object.assign(Object.create(null), { message: "UI reporter failed" });
						reported.resolve();
					});
					const fallback = vi.spyOn(logger, "error").mockImplementation(() => {
						reported.resolve();
					});
					const unhandled: unknown[] = [];
					const onUnhandled = (error: unknown) => {
						unhandled.push(error);
					};
					process.on("unhandledRejection", onUnhandled);
					try {
						await f.runner.emit({ type: "message_end", message: message("completed") });
						await reported.promise;
						await scheduler.wait(0);
						expect(returned).toEqual([undefined]);
						expect(f.showError).toHaveBeenCalledWith("Extension abort failed: abort dependency failed");
						if (reporterKind !== "normal")
							expect(fallback).toHaveBeenCalledWith("Extension abort error reporting failed", {
								path: "<interactive>",
								error: "abort dependency failed",
								reportError: "UI reporter failed",
							});
						else expect(fallback).not.toHaveBeenCalled();
						expect(unhandled).toEqual([]);
					} finally {
						process.off("unhandledRejection", onUnhandled);
					}
				});
			}
		}
	}
});
