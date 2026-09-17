import { afterEach, describe, expect, spyOn, test } from "bun:test";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { Model } from "@oh-my-pi/pi-ai";
import { canonicalJson, sha256Hex, type BeginStagePreflightPayload, type RecordStagePreflightPayload, type StageLaunch, type StagePreflight, type WorkItemView } from "@oh-my-pi/pi-work-client";
import * as auditorRunner from "../extensions/workflow/auditor-runner";
import { dispatchNativeStage } from "../extensions/workflow/native-stage-dispatch";
import type { KnowledgeBridge } from "../extensions/workflow/knowledge-bridge";
import type { WorkflowBackend } from "../extensions/workflow/backend";

const workId = "00000000-0000-7000-8000-000000000001";
const revisionId = "00000000-0000-7000-8000-000000000002";
const grantId = "00000000-0000-7000-8000-000000000003";
const workspaceId = "00000000-0000-7000-8000-000000000004";

function model(): Model {
	return {
		id: "gpt-5.6-luna",
		name: "Luna",
		provider: "openai-codex",
		api: "openai-codex-responses",
		baseUrl: "https://example.invalid",
		reasoning: true,
		input: ["text"],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 1000,
		maxTokens: 100,
		compat: {} as Model["compat"],
		thinking: { mode: "effort", efforts: ["high"] },
	} as Model;
}

function item(): WorkItemView {
	return {
		work_id: workId,
		workspace_id: workspaceId,
		alias: { work_id: workId, key: "OMP-1", primary: true, origin: "local" },
		state: "BACKLOG",
		revision: {
			revision_id: revisionId,
			work_id: workId,
			revision_number: 1,
			title: "native",
			description: "do work",
			scope: "repo",
			acceptance_criteria: ["it works"],
			content_sha256: "a".repeat(64),
			created_by: "test",
			created_at: "2026-09-15T00:00:00Z",
		},
		candidate: null,
		project_id: null,
		repository_id: "00000000-0000-7000-8000-000000000005",
		archived: false,
	};
}

function launch(status: StageLaunch["status"] = "reserved"): StageLaunch {
	return {
		launch_id: "00000000-0000-7000-8000-000000000006",
		workspace_id: workspaceId,
		work_id: workId,
		revision_id: revisionId,
		candidate_id: null,
		attempt_id: null,
		grant_id: grantId,
		role: "implement",
		request_sha256: "b".repeat(64),
		tool_call_id: "call-1",
		task_sha256: "c".repeat(64),
		prepared_context_sha256: "d".repeat(64),
		requested_selector: "openai-codex/gpt-5.6-luna:high",
		requested_provider: "openai-codex",
		requested_model: "gpt-5.6-luna",
		requested_api: "openai-codex-responses",
		requested_effort: "high",
		requested_wire_model: "gpt-5.6-luna",
		resolved_selector: "openai-codex/gpt-5.6-luna:high",
		resolved_provider: "openai-codex",
		resolved_model: "gpt-5.6-luna",
		served_selector: null,
		served_model: null,
		is_fallback: true,
		fallback_reason: null,
		status,
		outcome_sha256: null,
		outcome: null,
		reserved_at: "2026-09-15T00:00:00Z",
		handed_off_at: null,
		settled_at: null,
	};
}

describe("native stage dispatch", () => {
	afterEach(() => {
		for (const restore of [prepareRestore]) restore?.();
	});
	let prepareRestore: (() => void) | undefined;

		test("reserves, hands off at first provider dispatch, then settles one canonical outcome", async () => {
		const events: string[] = [];
		let reservationInput: { fallbackReason?: string | null } | undefined;
		const selectedModel = model();
		const prepared = launch();
		const settled = launch("settled");
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => async (_task, _id) => {
			events.push("runner");
			await options.onHandoff?.();
			events.push("provider");
			return { started: true, payload: "{\"ok\":true}", resolvedModel: "openai-codex/gpt-5.6-luna:high" };
		});
		prepareRestore = () => prepare.mockRestore();
		const fakeWorkClient = {
			workItem: async () => item(),
			workflow: async () => ({ stage_launches: [settled] }),
		};
		const backend = {
			workspaceId,
			workClient: fakeWorkClient,
			reserveStageLaunch: async input => { events.push("reserve"); reservationInput = input; return prepared; },
			handoffStageLaunch: async () => { events.push("handoff"); return { ...prepared, status: "handed_off" as const }; },
			settleStageLaunch: async () => { events.push("settle"); return settled; },
		} as unknown as WorkflowBackend;
		const bridge = {
			prepareStageContext: async () => ({ content: "retrieved facts", bundle: { bundle_id: "bundle-1", bundle_sha256: "e".repeat(64) } }),
		} as unknown as KnowledgeBridge;
		const ctx = {
			models: {
				resolve: () => selectedModel,
				list: () => [selectedModel],
				current: () => selectedModel,
				family: () => "openai-codex/gpt-5.6-luna",
			},
		} as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, bridge, {
			workKey: "OMP-1",
			role: "implement",
			taskBody: "edit sealed file",
			toolCallId: "call-1",
			grantId,
		});

		expect(events).toEqual(["reserve", "runner", "handoff", "provider", "settle"]);
		expect(result.launch.status).toBe("settled");
		expect(result.contextBundleId).toBe("bundle-1");
		expect(reservationInput?.fallbackReason).toBe("primary route unavailable or native transport preflight failed");
	});

	test("reconciles lost handoff response and never settles interrupted launch", async () => {
		const events: string[] = [];
		const prepared = launch();
		const interrupted = launch("interrupted");
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => async (_task, _id) => {
			events.push("runner");
			try {
				await options.onHandoff?.();
			} catch {
				events.push("handoff-error");
			}
			events.push("provider");
			return { started: true, payload: "{\"ok\":true}", resolvedModel: "openai-codex/gpt-5.6-luna:high" };
		});
		prepareRestore = () => prepare.mockRestore();
		const fakeWorkClient = {
			workItem: async () => item(),
			workflow: async () => ({ stage_launches: [interrupted] }),
		};
		const backend = {
			workspaceId,
			workClient: fakeWorkClient,
			reserveStageLaunch: async () => { events.push("reserve"); return prepared; },
			handoffStageLaunch: async () => { events.push("handoff"); throw new Error("connection reset after commit"); },
			reconcileStageLaunch: async () => { events.push("reconcile"); return interrupted; },
			settleStageLaunch: async () => { events.push("settle"); throw new Error("interrupted launch cannot settle"); },
		} as unknown as WorkflowBackend;
		const ctx = {
			models: {
				resolve: () => model(),
				list: () => [model()],
				current: () => model(),
				family: () => "openai-codex/gpt-5.6-luna",
			},
		} as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1",
			role: "implement",
			taskBody: "edit sealed file",
			toolCallId: "call-loss-1",
			grantId,
		});

		expect(events).toEqual(["reserve", "runner", "handoff", "handoff-error", "provider", "reconcile"]);
		expect(result.launch.status).toBe("interrupted");
	});

	test("replays settled launch outcome without invoking provider", async () => {
		const events: string[] = [];
		const settled = { ...launch("settled"), outcome: { started: true, payload: "{\"replayed\":true}", resolved_model: "openai-codex/gpt-5.6-luna:high" } };
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, _options) => async () => {
			events.push("provider");
			return { started: true, payload: "{\"unexpected\":true}" };
		});
		prepareRestore = () => prepare.mockRestore();
		const backend = {
			workspaceId,
			workClient: { workItem: async () => item() },
			reserveStageLaunch: async () => { events.push("reserve"); return settled; },
		} as unknown as WorkflowBackend;
		const ctx = { models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" } } as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1", role: "implement", taskBody: "edit sealed file", toolCallId: "call-replay-1", grantId,
		});

		expect(events).toEqual(["reserve"]);
		expect(result.launch.status).toBe("settled");
		expect(result.run.payload).toBe("{\"replayed\":true}");
	});

	test("preserves handed-off replay uncertainty without invoking provider", async () => {
		const events: string[] = [];
		const handedOff = launch("handed_off");
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, _options) => async () => {
			events.push("provider");
			return { started: true, payload: "{\"unexpected\":true}" };
		});
		prepareRestore = () => prepare.mockRestore();
		const backend = {
			workspaceId,
			workClient: { workItem: async () => item() },
			reserveStageLaunch: async () => { events.push("reserve"); return handedOff; },
		} as unknown as WorkflowBackend;
		const ctx = { models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" } } as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1", role: "implement", taskBody: "edit sealed file", toolCallId: "call-replay-2", grantId,
		});

		expect(events).toEqual(["reserve"]);
		expect(result.launch.status).toBe("handed_off");
		expect(result.run.error).toContain("automatic reconciliation refused");
	});

	test("recovers reservation response loss from durable workflow identity", async () => {
		const events: string[] = [];
		const prepared = launch();
		const settled = launch("settled");
		let requestSha256 = "";
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => async () => {
			events.push("provider");
			await options.onHandoff?.();
			return { started: true, payload: "{\"recovered\":true}", resolvedModel: "openai-codex/gpt-5.6-luna:high" };
		});
		prepareRestore = () => prepare.mockRestore();
		const backend = {
			workspaceId,
			workClient: {
				workItem: async () => item(),
				workflow: async () => ({ stage_launches: [{ ...prepared, request_sha256: requestSha256, tool_call_id: "call-response-loss" }] }),
			},
			reserveStageLaunch: async input => { events.push("reserve"); requestSha256 = input.requestSha256; throw new Error("response lost after commit"); },
			handoffStageLaunch: async () => { events.push("handoff"); return { ...prepared, status: "handed_off" as const }; },
			settleStageLaunch: async () => { events.push("settle"); return settled; },
		} as unknown as WorkflowBackend;
		const ctx = { models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" } } as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1", role: "implement", taskBody: "edit sealed file", toolCallId: "call-response-loss", grantId,
		});

		expect(events).toEqual(["reserve", "provider", "handoff", "settle"]);
		expect(result.launch.status).toBe("settled");
	});

	test("settlement persists measured usage without cost and request count, and hashes outcome", async () => {
		const events: string[] = [];
		let settledInput: { launchId: string; outcomeSha256: string; outcome: Record<string, unknown> } | undefined;
		const prepared = launch();
		const settled = launch("settled");
		const measuredUsage: auditorRunner.NativeAuditUsage = {
			input: 120,
			output: 45,
			cacheRead: 10,
			cacheWrite: 5,
			totalTokens: 180,
			reasoningTokens: 20,
		};
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => async () => {
			events.push("runner");
			await options.onHandoff?.();
			events.push("provider");
			return {
				started: true,
				payload: "{\"ok\":true}",
				resolvedModel: "openai-codex/gpt-5.6-luna:high",
				usage: measuredUsage,
				requests: 2,
			};
		});
		prepareRestore = () => prepare.mockRestore();
		const fakeWorkClient = {
			workItem: async () => item(),
			workflow: async () => ({ stage_launches: [settled] }),
		};
		const backend = {
			workspaceId,
			workClient: fakeWorkClient,
			reserveStageLaunch: async () => { events.push("reserve"); return prepared; },
			handoffStageLaunch: async () => { events.push("handoff"); return { ...prepared, status: "handed_off" as const }; },
			settleStageLaunch: async input => { events.push("settle"); settledInput = input; return settled; },
		} as unknown as WorkflowBackend;
		const ctx = {
			models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" },
		} as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1",
			role: "implement",
			taskBody: "edit sealed file",
			toolCallId: "call-measured-1",
			grantId,
		});

		expect(events).toEqual(["reserve", "runner", "handoff", "provider", "settle"]);
		expect(result.launch.status).toBe("settled");
		expect(settledInput).toBeDefined();
		expect(settledInput!.outcome).toMatchObject({
			started: true,
			payload: "{\"ok\":true}",
			usage: {
				input: 120,
				output: 45,
				cacheRead: 10,
				cacheWrite: 5,
				totalTokens: 180,
				reasoningTokens: 20,
			},
			requests: 2,
		});
		expect("cost" in (settledInput!.outcome.usage as Record<string, unknown>)).toBe(false);
		expect(settledInput!.outcomeSha256).toBe(sha256Hex(canonicalJson(settledInput!.outcome)));
		expect(result.run.usage).toEqual(measuredUsage);
		expect(result.run.requests).toBe(2);
	});

	test("settlement persists explicit null for absent usage and requests, never zero", async () => {
		let settledInput: { launchId: string; outcomeSha256: string; outcome: Record<string, unknown> } | undefined;
		const prepared = launch();
		const settled = launch("settled");
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => async () => {
			await options.onHandoff?.();
			return { started: true, payload: "{\"ok\":true}", resolvedModel: "openai-codex/gpt-5.6-luna:high" };
		});
		prepareRestore = () => prepare.mockRestore();
		const fakeWorkClient = {
			workItem: async () => item(),
			workflow: async () => ({ stage_launches: [settled] }),
		};
		const backend = {
			workspaceId,
			workClient: fakeWorkClient,
			reserveStageLaunch: async () => prepared,
			handoffStageLaunch: async () => ({ ...prepared, status: "handed_off" as const }),
			settleStageLaunch: async input => { settledInput = input; return settled; },
		} as unknown as WorkflowBackend;
		const ctx = {
			models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" },
		} as unknown as ExtensionContext;

		await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1",
			role: "implement",
			taskBody: "edit sealed file",
			toolCallId: "call-absent-1",
			grantId,
		});

		expect(settledInput).toBeDefined();
		expect(settledInput!.outcome.usage).toBeNull();
		expect(settledInput!.outcome.requests).toBeNull();
		expect(settledInput!.outcome.usage).not.toBe(0);
		expect(settledInput!.outcome.requests).not.toBe(0);
		expect(settledInput!.outcomeSha256).toBe(sha256Hex(canonicalJson(settledInput!.outcome)));
	});

	test("replays settled launch outcome returning persisted usage and requests without invoking provider", async () => {
		const events: string[] = [];
		const persistedUsage = { input: 150, output: 60, cacheRead: 20, cacheWrite: 0, totalTokens: 230 };
		const settled = {
			...launch("settled"),
			outcome: {
				started: true,
				payload: "{\"replayed\":true}",
				resolved_model: "openai-codex/gpt-5.6-luna:high",
				usage: persistedUsage,
				requests: 4,
			},
		};
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async () => async () => {
			events.push("provider");
			return { started: true, payload: "{\"unexpected\":true}" };
		});
		prepareRestore = () => prepare.mockRestore();
		const backend = {
			workspaceId,
			workClient: { workItem: async () => item() },
			reserveStageLaunch: async () => { events.push("reserve"); return settled; },
		} as unknown as WorkflowBackend;
		const ctx = { models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" } } as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1", role: "implement", taskBody: "edit sealed file", toolCallId: "call-replay-usage", grantId,
		});

		expect(events).toEqual(["reserve"]);
		expect(result.launch.status).toBe("settled");
		expect(result.run.requests).toBe(4);
		expect(result.run.usage).toEqual(persistedUsage);
		expect("cost" in (result.run.usage as Record<string, unknown>)).toBe(false);
	});

	test("replays settled launch with absent or malformed measurement conservatively omitting fields", async () => {
		const settledNull = {
			...launch("settled"),
			outcome: {
				started: true,
				payload: "{\"replayed\":true}",
				usage: null,
				requests: null,
			},
		};
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async () => async () => {
			throw new Error("unexpected runner call");
		});
		prepareRestore = () => prepare.mockRestore();
		const backendNull = {
			workspaceId,
			workClient: { workItem: async () => item() },
			reserveStageLaunch: async () => settledNull,
		} as unknown as WorkflowBackend;
		const ctx = { models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" } } as unknown as ExtensionContext;

		const resultNull = await dispatchNativeStage(ctx, backendNull, undefined, {
			workKey: "OMP-1", role: "implement", taskBody: "edit", toolCallId: "call-replay-null", grantId,
		});
		expect(resultNull.run.usage).toBeUndefined();
		expect(resultNull.run.requests).toBeUndefined();

		const settledMalformed = {
			...launch("settled"),
			outcome: {
				started: true,
				payload: "{\"replayed\":true}",
				usage: { input: "invalid", output: 10 },
				requests: -5,
			},
		};
		const backendMalformed = {
			workspaceId,
			workClient: { workItem: async () => item() },
			reserveStageLaunch: async () => settledMalformed,
		} as unknown as WorkflowBackend;

		const resultMalformed = await dispatchNativeStage(ctx, backendMalformed, undefined, {
			workKey: "OMP-1", role: "implement", taskBody: "edit", toolCallId: "call-replay-bad", grantId,
		});
		expect(resultMalformed.run.usage).toBeUndefined();
		expect(resultMalformed.run.requests).toBeUndefined();
	});

	test("replays settled launch rejecting negative, fractional, and non-finite usage counters or requests", async () => {
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async () => async () => {
			throw new Error("unexpected runner call");
		});
		prepareRestore = () => prepare.mockRestore();
		const ctx = { models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" } } as unknown as ExtensionContext;

		const invalidCases: Array<{ usage?: unknown; requests?: unknown }> = [
			{ usage: { input: -1, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 10 }, requests: 1 },
			{ usage: { input: 1.5, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 10 }, requests: 1 },
			{ usage: { input: Number.NaN, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 10 }, requests: 1 },
			{ usage: { input: Number.POSITIVE_INFINITY, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 10 }, requests: 1 },
			{ usage: { input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20 }, requests: 2.5 },
			{ usage: { input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20 }, requests: -1 },
			{ usage: { input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20 }, requests: Number.NaN },
			{ usage: { input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20 }, requests: Number.POSITIVE_INFINITY },
		];

		for (const [index, testCase] of invalidCases.entries()) {
			const settled = {
				...launch("settled"),
				outcome: {
					started: true,
					payload: "{\"replayed\":true}",
					usage: testCase.usage,
					requests: testCase.requests,
				},
			};
			const backend = {
				workspaceId,
				workClient: { workItem: async () => item() },
				reserveStageLaunch: async () => settled,
			} as unknown as WorkflowBackend;

			const result = await dispatchNativeStage(ctx, backend, undefined, {
				workKey: "OMP-1", role: "implement", taskBody: "edit", toolCallId: `call-replay-invalid-${index}`, grantId,
			});

			if (testCase.usage && (
				(testCase.usage as Record<string, number>).input === -1 ||
				(testCase.usage as Record<string, number>).input === 1.5 ||
				Number.isNaN((testCase.usage as Record<string, number>).input) ||
				!Number.isFinite((testCase.usage as Record<string, number>).input)
			)) {
				expect(result.run.usage).toBeUndefined();
			}
			if (typeof testCase.requests === "number" && (!Number.isInteger(testCase.requests) || testCase.requests < 0 || !Number.isFinite(testCase.requests))) {
				expect(result.run.requests).toBeUndefined();
			}
		}
	});

	test("replays settled launch omitting complete usage when optional scalar or nested member is invalid", async () => {
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async () => async () => {
			throw new Error("unexpected runner call");
		});
		prepareRestore = () => prepare.mockRestore();
		const ctx = { models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" } } as unknown as ExtensionContext;

		const corruptUsageCases: unknown[] = [
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, reasoningTokens: -5 },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, premiumRequests: 1.5 },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, contextTokens: Number.NaN },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, orchestration: "not-an-object" },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, orchestration: { input: -2 } },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, orchestration: { cacheRead: 1.1 } },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, cttl: { ephemeral5m: -1 } },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, cttl: { ephemeral1h: "bad" } },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, server: { webSearch: -1 } },
			{ input: 10, output: 10, cacheRead: 0, cacheWrite: 0, totalTokens: 20, server: { webFetch: 2.7 } },
		];

		for (const [index, corruptUsage] of corruptUsageCases.entries()) {
			const settled = {
				...launch("settled"),
				outcome: {
					started: true,
					payload: "{\"replayed\":true}",
					usage: corruptUsage,
					requests: 1,
				},
			};
			const backend = {
				workspaceId,
				workClient: { workItem: async () => item() },
				reserveStageLaunch: async () => settled,
			} as unknown as WorkflowBackend;

			const result = await dispatchNativeStage(ctx, backend, undefined, {
				workKey: "OMP-1", role: "implement", taskBody: "edit", toolCallId: `call-replay-corrupt-${index}`, grantId,
			});

			expect(result.run.usage).toBeUndefined();
			expect(result.run.requests).toBe(1);
		}
	});

	test("replays settled launch preserving known nested fields and stripping unknown keys and cost", async () => {
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async () => async () => {
			throw new Error("unexpected runner call");
		});
		prepareRestore = () => prepare.mockRestore();
		const ctx = { models: { resolve: () => model(), list: () => [model()], current: () => model(), family: () => "openai-codex/gpt-5.6-luna" } } as unknown as ExtensionContext;

		const persistedWithExtras = {
			input: 100,
			output: 50,
			cacheRead: 10,
			cacheWrite: 5,
			totalTokens: 165,
			contextTokens: 200,
			premiumRequests: 1,
			reasoningTokens: 25,
			orchestration: { input: 12, cacheRead: 4, output: 8, extraOrchKey: "dropped" },
			cttl: { ephemeral5m: 5, ephemeral1h: 0 },
			server: { webSearch: 2, webFetch: 1 },
			cost: { input: 0.01, output: 0.02, cacheRead: 0, cacheWrite: 0, total: 0.03 },
			unknownTopLevelKey: "dropped-too",
		};

		const settled = {
			...launch("settled"),
			outcome: {
				started: true,
				payload: "{\"replayed\":true}",
				usage: persistedWithExtras,
				requests: 3,
			},
		};
		const backend = {
			workspaceId,
			workClient: { workItem: async () => item() },
			reserveStageLaunch: async () => settled,
		} as unknown as WorkflowBackend;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1", role: "implement", taskBody: "edit", toolCallId: "call-replay-valid-nested", grantId,
		});

		expect(result.run.requests).toBe(3);
		expect(result.run.usage).toEqual({
			input: 100,
			output: 50,
			cacheRead: 10,
			cacheWrite: 5,
			totalTokens: 165,
			contextTokens: 200,
			premiumRequests: 1,
			reasoningTokens: 25,
			orchestration: { input: 12, cacheRead: 4, output: 8 },
			cttl: { ephemeral5m: 5, ephemeral1h: 0 },
			server: { webSearch: 2, webFetch: 1 },
		});
		expect("cost" in (result.run.usage as Record<string, unknown>)).toBe(false);
		expect("unknownTopLevelKey" in (result.run.usage as Record<string, unknown>)).toBe(false);
		expect("extraOrchKey" in ((result.run.usage as Record<string, unknown>).orchestration as Record<string, unknown>)).toBe(false);
	});

	test("records ordered preflight attempts with authoritative identities before reservation", async () => {
		const events: string[] = [];
		const preflightPayloads: RecordStagePreflightPayload[] = [];
		let reservationInput: Record<string, unknown> | undefined;
		const selectedModel = model();
		const prepared = launch();
		const settled = launch("settled");

		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => {
			await (options.preflight?.record ?? options.onPreflightAttempt)?.({
				transportAttemptId: "11111111-1111-4111-8111-111111111111",
				ordinal: 0,
				route: {
					requestedSelector: "google-antigravity/gemini-3.8-flash:high",
					model: { id: "gemini-3.8-flash", provider: "google-antigravity", api: "google-gemini-cli" } as Model,
					effort: "high",
					isFallback: false,
				},
				probeSha256: "f".repeat(64),
				outcome: "failed",
				stopReason: null,
				error: "503 unavailable",
				requests: null,
				usage: null,
				providerRequestId: null,
			});
			await (options.preflight?.record ?? options.onPreflightAttempt)?.({
				transportAttemptId: "22222222-2222-4222-8222-222222222222",
				ordinal: 1,
				route: {
					requestedSelector: "openai-codex/gpt-5.6-luna:high",
					model: selectedModel,
					effort: "high",
					isFallback: true,
				},
				probeSha256: "f".repeat(64),
				outcome: "selected",
				stopReason: "stop",
				error: null,
				requests: null,
				usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15 },
				providerRequestId: "resp-prov-2",
			});
			options.onRouteSelected?.({
				requestedSelector: "openai-codex/gpt-5.6-luna:high",
				model: selectedModel,
				effort: "high",
				isFallback: true,
			});
			return async (_task, _id) => {
				events.push("runner");
				await options.onHandoff?.();
				events.push("provider");
				return { started: true, payload: "{\"ok\":true}", resolvedModel: "openai-codex/gpt-5.6-luna:high" };
			};
		});
		prepareRestore = () => prepare.mockRestore();

		const fakeWorkClient = {
			workItem: async () => item(),
			workflow: async () => ({ stage_launches: [settled] }),
		};
		const backend = {
			workspaceId,
			workClient: fakeWorkClient,
			recordStagePreflight: async payload => {
				events.push("preflight");
				preflightPayloads.push(payload);
				return {
					preflight_id: "preflight-1",
					workspace_id: workspaceId,
					observed_at: new Date().toISOString(),
					...payload,
				} as StagePreflight;
			},
			reserveStageLaunch: async input => {
				events.push("reserve");
				reservationInput = input;
				return prepared;
			},
			handoffStageLaunch: async () => {
				events.push("handoff");
				return { ...prepared, status: "handed_off" as const };
			},
			settleStageLaunch: async () => {
				events.push("settle");
				return settled;
			},
		} as unknown as WorkflowBackend;

		const ctx = {
			models: {
				resolve: () => selectedModel,
				list: () => [selectedModel],
				current: () => selectedModel,
				family: () => "openai-codex/gpt-5.6-luna",
			},
			sessionManager: {
				getSessionId: () => "session-1",
			},
		} as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1",
			role: "implement",
			taskBody: "edit sealed file",
			toolCallId: "call-1",
			grantId,
		});

		expect(events).toEqual(["preflight", "preflight", "reserve", "runner", "handoff", "provider", "settle"]);
		expect(result.launch.status).toBe("settled");
		expect(preflightPayloads).toHaveLength(2);

		expect(preflightPayloads[0]).toEqual({
			work_id: workId,
			revision_id: revisionId,
			candidate_id: null,
			grant_id: grantId,
			attempt_id: null,
			session_id: "session-1",
			role: "implement",
			tool_call_id: "call-1",
			task_sha256: reservationInput?.taskSha256 as string,
			probe_sha256: "f".repeat(64),
			transport_attempt_id: "11111111-1111-4111-8111-111111111111",
			ordinal: 0,
			requested_selector: "google-antigravity/gemini-3.8-flash:high",
			requested_provider: "google-antigravity",
			requested_model: "gemini-3.8-flash",
			requested_api: "google-gemini-cli",
			requested_effort: "high",
			requested_wire_model: "gemini-3.8-flash",
			is_fallback: false,
			outcome: "failed",
			stop_reason: null,
			error: "503 unavailable",
			requests: null,
			usage: null,
			provider_request_id: null,
		});

		expect(preflightPayloads[1]).toEqual({
			work_id: workId,
			revision_id: revisionId,
			candidate_id: null,
			grant_id: grantId,
			attempt_id: null,
			session_id: "session-1",
			role: "implement",
			tool_call_id: "call-1",
			task_sha256: reservationInput?.taskSha256 as string,
			probe_sha256: "f".repeat(64),
			transport_attempt_id: "22222222-2222-4222-8222-222222222222",
			ordinal: 1,
			requested_selector: "openai-codex/gpt-5.6-luna:high",
			requested_provider: "openai-codex",
			requested_model: "gpt-5.6-luna",
			requested_api: "openai-codex-responses",
			requested_effort: "high",
			requested_wire_model: "gpt-5.6-luna",
			is_fallback: true,
			outcome: "selected",
			stop_reason: "stop",
			error: null,
			requests: null,
			usage: { input: 10, output: 5, cacheRead: 0, cacheWrite: 0, totalTokens: 15 },
			provider_request_id: "resp-prov-2",
		});
		expect("cost" in (preflightPayloads[1].usage as Record<string, unknown>)).toBe(false);
	});

	test("dispatch record failure prevents reservation, handoff, and runner execution", async () => {
		const events: string[] = [];
		const selectedModel = model();
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => {
			await (options.preflight?.record ?? options.onPreflightAttempt)?.({
				transportAttemptId: "33333333-3333-4333-8333-333333333333",
				ordinal: 0,
				route: {
					requestedSelector: "openai-codex/gpt-5.6-luna:high",
					model: selectedModel,
					effort: "high",
					isFallback: false,
				},
				probeSha256: "f".repeat(64),
				outcome: "selected",
				stopReason: "stop",
				error: null,
				requests: null,
				usage: null,
				providerRequestId: "resp-1",
			});
			return async (_task, _id) => {
				events.push("runner");
				await options.onHandoff?.();
				events.push("provider");
				return { started: true, payload: "{\"ok\":true}", resolvedModel: "openai-codex/gpt-5.6-luna:high" };
			};
		});
		prepareRestore = () => prepare.mockRestore();

		const backend = {
			workspaceId,
			workClient: {
				workItem: async () => item(),
			},
			recordStagePreflight: async () => {
				throw new Error("WorkService preflight storage failed");
			},
			reserveStageLaunch: async () => {
				events.push("reserve");
				return launch();
			},
			handoffStageLaunch: async () => {
				events.push("handoff");
				return launch("handed_off");
			},
		} as unknown as WorkflowBackend;

		const ctx = {
			models: {
				resolve: () => selectedModel,
				list: () => [selectedModel],
				current: () => selectedModel,
				family: () => "openai-codex/gpt-5.6-luna",
			},
		} as unknown as ExtensionContext;

		await expect(dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1",
			role: "implement",
			taskBody: "edit",
			toolCallId: "call-1",
			grantId,
		})).rejects.toThrow("WorkService preflight storage failed");

		expect(events).toEqual([]);
	});

	test("dispatch begin and record identities match and service-returned transport attempt ID is used", async () => {
		const beginPayloads: BeginStagePreflightPayload[] = [];
		const recordPayloads: RecordStagePreflightPayload[] = [];
		const serviceTransportAttemptId = "44444444-4444-4444-8444-444444444444";
		const selectedModel = model();
		const prepared = launch();
		const settled = launch("settled");

		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => {
			const route = {
				requestedSelector: "openai-codex/gpt-5.6-luna:high",
				model: selectedModel,
				effort: "high",
				isFallback: false,
			};
			const beginResult = await options.preflight!.begin({
				route,
				ordinal: 0,
				probeSha256: "f".repeat(64),
			});
			await options.preflight!.record({
				transportAttemptId: beginResult.intent.transport_attempt_id,
				ordinal: 0,
				route,
				probeSha256: "f".repeat(64),
				outcome: "selected",
				stopReason: "stop",
				error: null,
				requests: null,
				usage: null,
				providerRequestId: "resp-1",
			});
			options.onRouteSelected?.(route);
			return async (_task, _id) => {
				await options.onHandoff?.();
				return { started: true, payload: "{\"ok\":true}", resolvedModel: "openai-codex/gpt-5.6-luna:high" };
			};
		});
		prepareRestore = () => prepare.mockRestore();

		const fakeWorkClient = {
			workItem: async () => item(),
			workflow: async () => ({ stage_launches: [settled] }),
		};

		const backend = {
			workspaceId,
			workClient: fakeWorkClient,
			beginStagePreflight: async (payload: BeginStagePreflightPayload) => {
				beginPayloads.push(payload);
				return {
					type: "begin_stage_preflight",
					status: "applied",
					intent: {
						intent_id: "00000000-0000-4000-8000-000000000001",
						workspace_id: workspaceId,
						work_id: payload.work_id,
						revision_id: payload.revision_id,
						candidate_id: payload.candidate_id ?? null,
						attempt_id: payload.attempt_id ?? null,
						grant_id: payload.grant_id ?? null,
						role: payload.role,
						tool_call_id: payload.tool_call_id,
						task_sha256: payload.task_sha256,
						probe_sha256: payload.probe_sha256,
						transport_attempt_id: serviceTransportAttemptId,
						ordinal: payload.ordinal,
						requested_selector: payload.requested_selector,
						requested_provider: payload.requested_provider,
						requested_model: payload.requested_model,
						requested_api: payload.requested_api,
						requested_effort: payload.requested_effort ?? null,
						requested_wire_model: payload.requested_wire_model,
						is_fallback: payload.is_fallback,
						logical_sha256: "0".repeat(64),
						group_sha256: "0".repeat(64),
						host_owner_id: "owner-1",
						status: "begun",
						created_at: new Date().toISOString(),
						settled_at: null,
					},
					preflight: undefined,
				};
			},
			recordStagePreflight: async (payload: RecordStagePreflightPayload) => {
				recordPayloads.push(payload);
				return {
					preflight_id: "preflight-1",
					workspace_id: workspaceId,
					observed_at: new Date().toISOString(),
					...payload,
				} as StagePreflight;
			},
			reserveStageLaunch: async () => prepared,
			handoffStageLaunch: async () => ({ ...prepared, status: "handed_off" as const }),
			settleStageLaunch: async () => settled,
		} as unknown as WorkflowBackend;

		const ctx = {
			models: {
				resolve: () => selectedModel,
				list: () => [selectedModel],
				current: () => selectedModel,
				family: () => "openai-codex/gpt-5.6-luna",
			},
			sessionManager: {
				getSessionId: () => "session-1",
			},
		} as unknown as ExtensionContext;

		const result = await dispatchNativeStage(ctx, backend, undefined, {
			workKey: "OMP-1",
			role: "implement",
			taskBody: "edit sealed file",
			toolCallId: "call-1",
			grantId,
		});

		expect(result.launch.status).toBe("settled");
		expect(beginPayloads).toHaveLength(1);
		expect(recordPayloads).toHaveLength(1);

		const begin = beginPayloads[0];
		const record = recordPayloads[0];

		// Identities match
		expect(begin.work_id).toBe(record.work_id);
		expect(begin.revision_id).toBe(record.revision_id);
		expect(begin.candidate_id).toBe(record.candidate_id);
		expect(begin.grant_id).toBe(record.grant_id);
		expect(begin.attempt_id).toBe(record.attempt_id);
		// session_id is deliberately excluded from begin intent payload
		expect((begin as Record<string, unknown>).session_id).toBeUndefined();
		expect(record.session_id).toBe("session-1");
		expect(begin.role).toBe(record.role);
		expect(begin.tool_call_id).toBe(record.tool_call_id);
		expect(begin.task_sha256).toBe(record.task_sha256);
		expect(begin.probe_sha256).toBe(record.probe_sha256);
		expect(begin.ordinal).toBe(record.ordinal);
		expect(begin.requested_selector).toBe(record.requested_selector);
		expect(begin.requested_provider).toBe(record.requested_provider);
		expect(begin.requested_model).toBe(record.requested_model);
		expect(begin.requested_api).toBe(record.requested_api);
		expect(begin.requested_effort).toBe(record.requested_effort);
		expect(begin.requested_wire_model).toBe(record.requested_wire_model);
		expect(begin.is_fallback).toBe(record.is_fallback);

		// Record used the service-minted transportAttemptId
		expect(record.transport_attempt_id).toBe(serviceTransportAttemptId);
	});

	test("dispatch fails closed when beginStagePreflight capability is absent", async () => {
		const selectedModel = model();
		const workItem = item();
		const prepare = spyOn(auditorRunner, "prepareNativeStageRunner").mockImplementation(async (_ctx, options) => {
			const route = {
				requestedSelector: "openai-codex/gpt-5.6-luna:high",
				model: selectedModel,
				effort: "high",
				isFallback: false,
			};
			await options.preflight!.begin({
				route,
				ordinal: 0,
				probeSha256: "f".repeat(64),
			});
			return async () => ({ started: true, payload: "{}", resolvedModel: null });
		});
		prepareRestore = () => prepare.mockRestore();

		const backend = {
			workspaceId,
			workClient: {
				workItem: async () => workItem,
			},
			// beginStagePreflight is absent
		} as unknown as WorkflowBackend;

		const ctx = {
			models: {
				resolve: () => selectedModel,
				list: () => [selectedModel],
				current: () => selectedModel,
				family: () => "openai-codex/gpt-5.6-luna",
			},
			sessionManager: {
				getSessionId: () => "session-1",
			},
		} as unknown as ExtensionContext;

		await expect(
			dispatchNativeStage(ctx, backend, undefined, {
				workKey: "OMP-1",
				role: "implement",
				taskBody: "edit sealed file",
				toolCallId: "call-1",
				grantId,
			}),
		).rejects.toThrow("native stage dispatch requires WorkService beginStagePreflight capability");
	});
});
