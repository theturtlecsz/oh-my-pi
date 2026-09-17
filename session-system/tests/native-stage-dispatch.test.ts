import { afterEach, describe, expect, spyOn, test } from "bun:test";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { Model } from "@oh-my-pi/pi-ai";
import type { StageLaunch, WorkItemView } from "@oh-my-pi/pi-work-client";
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
});
