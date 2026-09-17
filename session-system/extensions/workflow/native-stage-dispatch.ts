import { canonicalJson, payloadHash, sha256Hex, type StageLaunch, type StagePreflightUsage } from "@oh-my-pi/pi-work-client";
import { withFileLock } from "@oh-my-pi/pi-utils";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { join } from "node:path";
import {
	nativeStageTaskInput,
	prepareNativeStageRunner,
	stripUsageCost,
	type NativeAuditRunResult,
	type NativeAuditUsage,
	type NativeStagePreflightAttempt,
} from "./auditor-runner";
import type { KnowledgeBridge, KnowledgeExecutionIdentity } from "./knowledge-bridge";
import { nativeStageRouteCandidates, resolveNativeStageRoute, type NativeStageRole, type NativeStageRoute } from "./native-stage-profile";
import type { NativeStageLaunchInput, WorkflowBackend } from "./backend";

export interface NativeStageDispatchInput {
	workKey: string;
	role: NativeStageRole;
	taskBody: string;
	toolCallId: string;
	grantId?: string | null;
	attemptId?: string | null;
	candidateId?: string | null;
	candidateSha256?: string | null;
	sourceRevision?: string | null;
	writeRoots?: readonly string[];
	boundAuditRoute?: NativeStageRoute;
}

export interface NativeStageDispatchResult {
	launch: StageLaunch;
	run: NativeAuditRunResult;
	contextBundleId?: string;
	contextBundleSha256?: string;
}

function isNonNegativeInteger(value: unknown): value is number {
	return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function parsePersistedUsage(value: unknown): NativeAuditUsage | undefined {
	if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
	const candidate = value as Record<string, unknown>;
	if (
		!isNonNegativeInteger(candidate.input) ||
		!isNonNegativeInteger(candidate.output) ||
		!isNonNegativeInteger(candidate.cacheRead) ||
		!isNonNegativeInteger(candidate.cacheWrite) ||
		!isNonNegativeInteger(candidate.totalTokens)
	) {
		return undefined;
	}

	let contextTokens: number | undefined;
	if (candidate.contextTokens !== undefined) {
		if (!isNonNegativeInteger(candidate.contextTokens)) return undefined;
		contextTokens = candidate.contextTokens;
	}

	let premiumRequests: number | undefined;
	if (candidate.premiumRequests !== undefined) {
		if (!isNonNegativeInteger(candidate.premiumRequests)) return undefined;
		premiumRequests = candidate.premiumRequests;
	}

	let reasoningTokens: number | undefined;
	if (candidate.reasoningTokens !== undefined) {
		if (!isNonNegativeInteger(candidate.reasoningTokens)) return undefined;
		reasoningTokens = candidate.reasoningTokens;
	}

	let orchestration: NativeAuditUsage["orchestration"];
	if (candidate.orchestration !== undefined) {
		if (typeof candidate.orchestration !== "object" || candidate.orchestration === null || Array.isArray(candidate.orchestration)) {
			return undefined;
		}
		const raw = candidate.orchestration as Record<string, unknown>;
		const parsed: NonNullable<NativeAuditUsage["orchestration"]> = {};
		if (raw.input !== undefined) {
			if (!isNonNegativeInteger(raw.input)) return undefined;
			parsed.input = raw.input;
		}
		if (raw.cacheRead !== undefined) {
			if (!isNonNegativeInteger(raw.cacheRead)) return undefined;
			parsed.cacheRead = raw.cacheRead;
		}
		if (raw.output !== undefined) {
			if (!isNonNegativeInteger(raw.output)) return undefined;
			parsed.output = raw.output;
		}
		orchestration = parsed;
	}

	let cttl: NativeAuditUsage["cttl"];
	if (candidate.cttl !== undefined) {
		if (typeof candidate.cttl !== "object" || candidate.cttl === null || Array.isArray(candidate.cttl)) {
			return undefined;
		}
		const raw = candidate.cttl as Record<string, unknown>;
		const parsed: NonNullable<NativeAuditUsage["cttl"]> = {};
		if (raw.ephemeral5m !== undefined) {
			if (!isNonNegativeInteger(raw.ephemeral5m)) return undefined;
			parsed.ephemeral5m = raw.ephemeral5m;
		}
		if (raw.ephemeral1h !== undefined) {
			if (!isNonNegativeInteger(raw.ephemeral1h)) return undefined;
			parsed.ephemeral1h = raw.ephemeral1h;
		}
		cttl = parsed;
	}

	let server: NativeAuditUsage["server"];
	if (candidate.server !== undefined) {
		if (typeof candidate.server !== "object" || candidate.server === null || Array.isArray(candidate.server)) {
			return undefined;
		}
		const raw = candidate.server as Record<string, unknown>;
		const parsed: NonNullable<NativeAuditUsage["server"]> = {};
		if (raw.webSearch !== undefined) {
			if (!isNonNegativeInteger(raw.webSearch)) return undefined;
			parsed.webSearch = raw.webSearch;
		}
		if (raw.webFetch !== undefined) {
			if (!isNonNegativeInteger(raw.webFetch)) return undefined;
			parsed.webFetch = raw.webFetch;
		}
		server = parsed;
	}

	return {
		input: candidate.input,
		output: candidate.output,
		cacheRead: candidate.cacheRead,
		cacheWrite: candidate.cacheWrite,
		totalTokens: candidate.totalTokens,
		...(contextTokens !== undefined ? { contextTokens } : {}),
		...(premiumRequests !== undefined ? { premiumRequests } : {}),
		...(reasoningTokens !== undefined ? { reasoningTokens } : {}),
		...(orchestration !== undefined ? { orchestration } : {}),
		...(cttl !== undefined ? { cttl } : {}),
		...(server !== undefined ? { server } : {}),
	};
}

function parsePersistedRequests(value: unknown): number | undefined {
	if (!isNonNegativeInteger(value)) return undefined;
	return value;
}

function runFromSettledLaunch(launch: StageLaunch): NativeAuditRunResult {
	const outcome = launch.outcome;
	const usage = parsePersistedUsage(outcome?.usage);
	const requests = parsePersistedRequests(outcome?.requests);
	return {
		started: outcome?.started === true,
		...(typeof outcome?.payload === "string" ? { payload: outcome.payload } : {}),
		...(typeof outcome?.error === "string" ? { error: outcome.error } : {}),
		...(typeof outcome?.resolved_model === "string" ? { resolvedModel: outcome.resolved_model } : {}),
		...(typeof outcome?.resolved_model_is_fallback === "boolean" ? { resolvedModelIsFallback: outcome.resolved_model_is_fallback } : {}),
		...(usage !== undefined ? { usage } : {}),
		...(requests !== undefined ? { requests } : {}),
	};
}

function modelRouteFields(route: NativeStageRoute): Pick<NativeStageLaunchInput, "requestedSelector" | "requestedProvider" | "requestedModel" | "requestedApi" | "requestedEffort" | "requestedWireModel" | "resolvedSelector" | "resolvedProvider" | "resolvedModel" | "isFallback" | "fallbackReason"> {
	const effort = route.effort;
	return {
		requestedSelector: route.requestedSelector,
		requestedProvider: route.model.provider,
		requestedModel: route.model.id,
		requestedApi: route.model.api,
		requestedEffort: effort,
		requestedWireModel: (effort ? route.model.thinking?.effortRouting?.[effort] : undefined) ?? route.model.requestModelId ?? route.model.id,
		resolvedSelector: route.requestedSelector,
		resolvedProvider: route.model.provider,
		resolvedModel: route.model.id,
		isFallback: route.isFallback,
		fallbackReason: route.isFallback ? "primary route unavailable or native transport preflight failed" : null,
	};
}

/**
 * One native dispatch boundary. WorkService owns launch state; this function
 * only assembles trusted identity and drives the existing executor. Handoff is
 * awaited by the SDK callback immediately before the first provider request.
 */
export async function dispatchNativeStage(
	ctx: ExtensionContext,
	backend: WorkflowBackend,
	bridge: KnowledgeBridge | undefined,
	input: NativeStageDispatchInput,
	signal?: AbortSignal,
): Promise<NativeStageDispatchResult> {
	// NativeFileLock is process-owned and released when the host exits. The
	// lock spans reservation through executor completion, so a second local
	// host cannot mistake a live handed-off row for a crashed owner. Hosts on
	// different filesystems still require WorkService owner fencing.
	if (ctx.cwd && await Bun.file(join(ctx.cwd, ".git", "HEAD")).exists()) {
		return withFileLock(
			join(ctx.cwd, ".git", "omp-native-stage"),
			() => dispatchNativeStageLocked(ctx, backend, bridge, input, signal),
			{ retries: 1, retryDelayMs: 0 },
		);
	}
	return dispatchNativeStageLocked(ctx, backend, bridge, input, signal);
}

async function dispatchNativeStageLocked(
	ctx: ExtensionContext,
	backend: WorkflowBackend,
	bridge: KnowledgeBridge | undefined,
	input: NativeStageDispatchInput,
	signal?: AbortSignal,
): Promise<NativeStageDispatchResult> {
	if (!backend.workClient) throw new Error("native stage dispatch requires WorkService");
	if (!input.taskBody.trim()) throw new Error("native stage task is empty");
	if (input.role === "audit" && !input.boundAuditRoute) {
		throw new Error("native stage dispatch requires boundAuditRoute for audit role");
	}
	const item = await backend.workClient.workItem(input.workKey);
	const routes = input.role === "audit"
		? [input.boundAuditRoute!]
		: nativeStageRouteCandidates(ctx.models, input.role);
	const requestedRoute = input.role === "audit"
		? input.boundAuditRoute!
		: resolveNativeStageRoute(ctx.models, input.role);
	const identity: KnowledgeExecutionIdentity = {
		workspaceId: backend.workspaceId,
		repositoryId: item.repository_id ?? "",
		workId: item.work_id,
		revisionId: item.revision.revision_id,
		candidateId: input.candidateId ?? item.candidate?.candidate_id ?? null,
		candidateSha256: input.candidateSha256 ?? item.candidate?.candidate_sha256 ?? null,
		sourceRevision: input.sourceRevision ?? null,
		stage: input.role,
	};
	if (!identity.repositoryId) throw new Error("native stage dispatch requires a WorkService repository identity");

	const compiled = bridge ? await bridge.prepareStageContext(identity, ctx) : null;
	const context = compiled?.content ?? "";
	const deliveredTask = nativeStageTaskInput(input.taskBody, context);
	const taskSha256 = sha256Hex(deliveredTask);
	const preparedContextSha256 = sha256Hex(context);
	const requestedRouteFields = modelRouteFields(requestedRoute);
	let resolvedRouteFields = requestedRouteFields;
	let launch: StageLaunch | undefined;
	let handoffAttempted = false;
	let handoffAcknowledged = false;
	const recordPreflightAttempt = async (attempt: NativeStagePreflightAttempt) => {
		const routeFields = modelRouteFields(attempt.route);
		const sessionId = ctx.sessionManager?.getSessionId?.() ?? null;
		await backend.recordStagePreflight({
			work_id: item.work_id,
			revision_id: item.revision.revision_id,
			candidate_id: identity.candidateId,
			grant_id: input.grantId ?? null,
			attempt_id: input.attemptId ?? null,
			session_id: sessionId,
			role: input.role,
			tool_call_id: input.toolCallId,
			task_sha256: taskSha256,
			probe_sha256: attempt.probeSha256,
			transport_attempt_id: attempt.transportAttemptId,
			ordinal: attempt.ordinal,
			requested_selector: routeFields.requestedSelector,
			requested_provider: routeFields.requestedProvider,
			requested_model: routeFields.requestedModel,
			requested_api: routeFields.requestedApi,
			requested_effort: routeFields.requestedEffort,
			requested_wire_model: routeFields.requestedWireModel,
			is_fallback: routeFields.isFallback,
			outcome: attempt.outcome,
			stop_reason: attempt.stopReason ?? null,
			error: attempt.error ?? null,
			requests: null,
			usage: attempt.usage ? (stripUsageCost(attempt.usage) as StagePreflightUsage) : null,
			provider_request_id: attempt.providerRequestId ?? null,
		});
	};
	const runner = await prepareNativeStageRunner(ctx, {
		role: input.role,
		context,
		writeRoots: input.writeRoots,
		routes,
		boundRoute: input.boundAuditRoute,
		preflight: {
			begin: async ({ route, ordinal, probeSha256 }) => {
				const routeFields = modelRouteFields(route);
				if (typeof backend.beginStagePreflight !== "function") {
					throw new Error("native stage dispatch requires WorkService beginStagePreflight capability");
				}
				return backend.beginStagePreflight({
					work_id: item.work_id,
					revision_id: item.revision.revision_id,
					candidate_id: identity.candidateId,
					grant_id: input.grantId ?? null,
					attempt_id: input.attemptId ?? null,
					role: input.role,
					tool_call_id: input.toolCallId,
					task_sha256: taskSha256,
					probe_sha256: probeSha256,
					ordinal,
					requested_selector: routeFields.requestedSelector,
					requested_provider: routeFields.requestedProvider,
					requested_model: routeFields.requestedModel,
					requested_api: routeFields.requestedApi,
					requested_effort: routeFields.requestedEffort,
					requested_wire_model: routeFields.requestedWireModel,
					is_fallback: routeFields.isFallback,
				});
			},
			admit: async ({ transportAttemptId, logicalSha256 }) => {
				if (typeof backend.admitStagePreflight !== "function") {
					throw new Error("native stage dispatch requires WorkService admitStagePreflight capability");
				}
				return backend.admitStagePreflight({
					transport_attempt_id: transportAttemptId,
					logical_sha256: logicalSha256,
				});
			},
			cancel: async ({ transportAttemptId, logicalSha256, reason }) => {
				if (typeof backend.cancelStagePreflight !== "function") {
					throw new Error("native stage dispatch requires WorkService cancelStagePreflight capability");
				}
				return backend.cancelStagePreflight({
					transport_attempt_id: transportAttemptId,
					logical_sha256: logicalSha256,
					reason,
				});
			},
			record: recordPreflightAttempt,
		},
		onRouteSelected: selected => {
			resolvedRouteFields = modelRouteFields(selected);
		},
		onHandoff: async () => {
			if (!launch) throw new Error("native stage handoff attempted before WorkService reservation");
			handoffAttempted = true;
			await backend.handoffStageLaunch(launch.launch_id, taskSha256);
			handoffAcknowledged = true;
		},
	}, signal);
	const requestSha256 = payloadHash({
		workspace_id: backend.workspaceId,
		work_id: item.work_id,
		revision_id: item.revision.revision_id,
		candidate_id: identity.candidateId,
		grant_id: input.grantId ?? null,
		attempt_id: input.attemptId ?? null,
		role: input.role,
		task_sha256: taskSha256,
		prepared_context_sha256: preparedContextSha256,
		requested_selector: requestedRouteFields.requestedSelector,
		resolved_selector: resolvedRouteFields.resolvedSelector,
		resolved_provider: resolvedRouteFields.resolvedProvider,
		resolved_model: resolvedRouteFields.resolvedModel,
		is_fallback: resolvedRouteFields.isFallback,
	});
	const reservation = {
		workId: item.work_id,
		revisionId: item.revision.revision_id,
		candidateId: identity.candidateId,
		attemptId: input.attemptId ?? null,
		grantId: input.grantId ?? null,
		role: input.role,
		requestSha256,
		toolCallId: input.toolCallId,
		taskSha256,
		preparedContextSha256,
		...requestedRouteFields,
		resolvedSelector: resolvedRouteFields.requestedSelector,
		resolvedProvider: resolvedRouteFields.requestedProvider,
		resolvedModel: resolvedRouteFields.requestedModel,
		isFallback: resolvedRouteFields.isFallback,
	};
	try {
		launch = await backend.reserveStageLaunch(reservation);
	} catch (error) {
		// WorkService may have committed the reservation before the response was
		// lost. Read its durable workflow identity before allowing any retry; this
		// avoids minting or executing a second launch when the local claim is gone.
		try {
			const existing = (await backend.workClient.workflow(input.workKey)).stage_launches.find(
				row => row.request_sha256 === requestSha256 && row.tool_call_id === input.toolCallId,
			);
			if (!existing) throw error;
			launch = existing;
		} catch {
			throw error;
		}
	}

	if (launch.status === "settled") {
		return {
			launch,
			run: runFromSettledLaunch(launch),
			...(compiled ? { contextBundleId: compiled.bundle.bundle_id, contextBundleSha256: compiled.bundle.bundle_sha256 } : {}),
		};
	}
	if (launch.status === "handed_off") {
		return {
			launch,
			run: { started: true, error: "native stage delivery is uncertain after a committed handoff; provider replay and automatic reconciliation refused" },
			...(compiled ? { contextBundleId: compiled.bundle.bundle_id, contextBundleSha256: compiled.bundle.bundle_sha256 } : {}),
		};
	}
	if (launch.status !== "reserved") {
		return {
			launch,
			run: { started: false, error: `native stage launch is already ${launch.status}; provider replay refused` },
			...(compiled ? { contextBundleId: compiled.bundle.bundle_id, contextBundleSha256: compiled.bundle.bundle_sha256 } : {}),
		};
	}
	const run = await runner(input.taskBody, input.toolCallId, signal);
	if (!handoffAttempted && !handoffAcknowledged && launch.status === "reserved") {
		await backend.cancelStageLaunch(launch.launch_id, run.error ?? "native stage did not dispatch");
	} else if (!handoffAcknowledged && launch.status === "reserved") {
		await backend.reconcileStageLaunch(launch.launch_id, "stage handoff response was lost or provider returned before acknowledgement");
	}
	const outcome: Record<string, unknown> = {
		started: run.started,
		payload: run.payload ?? null,
		error: run.error ?? null,
		resolved_model: run.resolvedModel ?? null,
		resolved_model_is_fallback: run.resolvedModelIsFallback ?? false,
		usage: run.usage ? stripUsageCost(run.usage) : null,
		requests: typeof run.requests === "number" && Number.isFinite(run.requests) && run.requests >= 0 ? run.requests : null,
	};
	// A response loss after handoff is reconciled as interrupted. `run.started`
	// may still be true because the provider request left the host, but that
	// launch is terminal and cannot be settled after reconciliation.
	if (handoffAcknowledged || launch.status === "handed_off") {
		launch = await backend.settleStageLaunch({
			launchId: launch.launch_id,
			outcomeSha256: sha256Hex(canonicalJson(outcome)),
			outcome,
			servedModel: run.resolvedModel ?? null,
			servedSelector: run.resolvedModel ?? null,
		});
	} else {
		launch = (await backend.workClient.workflow(input.workKey)).stage_launches.find(row => row.launch_id === launch?.launch_id) ?? launch;
	}
	return {
		launch,
		run,
		...(compiled ? { contextBundleId: compiled.bundle.bundle_id, contextBundleSha256: compiled.bundle.bundle_sha256 } : {}),
	};
}
