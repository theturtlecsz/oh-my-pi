import {
	canonicalJson,
	payloadHash,
	sha256Hex,
	type StageLaunch,
	type StagePreflightUsage,
	type RateCard,
} from "@oh-my-pi/pi-work-client";
import { withFileLock } from "@oh-my-pi/pi-utils";
import { getAgentDir, Settings, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { Model } from "@oh-my-pi/pi-ai";
import { join } from "node:path";
import {
	nativeStageMaxRuntimeMs,
	nativeStageRequestCeiling,
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

export function nativeStageUsageCeiling(
	model: Model,
	card: RateCard,
	requests: number,
): Record<string, number> {
	if (!Number.isInteger(requests) || requests <= 0) {
		throw new Error(`requests must be a positive integer (got ${requests})`);
	}
	const ceiling: Record<string, number> = {};
	for (const category of Object.keys(card.unit_prices)) {
		if (category === "input" || category === "cacheRead" || category === "cacheWrite") {
			if (model.contextWindow === undefined || model.contextWindow === null || model.contextWindow <= 0) {
				throw new Error(`model ${model.provider}/${model.id} is missing positive contextWindow limit for category "${category}"`);
			}
			ceiling[category] = model.contextWindow * requests;
		} else if (category === "output") {
			if (model.compat?.omitMaxOutputTokens) {
				throw new Error(`model ${model.provider}/${model.id} specifies omitMaxOutputTokens; output ceiling cannot be bounded`);
			}
			if (model.maxTokens === undefined || model.maxTokens === null || model.maxTokens <= 0) {
				throw new Error(`model ${model.provider}/${model.id} is missing positive maxTokens limit for category "output"`);
			}
			ceiling[category] = model.maxTokens * requests;
		} else if (category === "premiumRequests") {
			ceiling[category] = 1 * requests;
		} else {
			throw new Error(`rate card contains unmapped category "${category}" without an authoritative ceiling`);
		}
	}
	return ceiling;
}

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

function deterministicUuid(seed: string): string {
	const hash = sha256Hex(seed);
	return [
		hash.slice(0, 8),
		hash.slice(8, 12),
		`4${hash.slice(13, 16)}`,
		`a${hash.slice(17, 20)}`,
		hash.slice(20, 32),
	].join("-");
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
	let resolvedRoute = requestedRoute;
	let launch: StageLaunch | undefined;
	let handoffAttempted = false;
	let handoffAcknowledged = false;
	let stageCeiling: { hardCeiling: number; softBudget: number; maxRuntimeMs: number } | undefined;
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
		onRequestCeiling: ceiling => {
			stageCeiling = ceiling;
		},
		onRouteSelected: selected => {
			resolvedRoute = selected;
			resolvedRouteFields = modelRouteFields(selected);
		},
		onHandoff: async () => {
			if (!launch) throw new Error("native stage handoff attempted before WorkService reservation");
			handoffAttempted = true;
			await backend.handoffStageLaunch(launch.launch_id, taskSha256);
			handoffAcknowledged = true;
		},
	}, signal);
	if (!stageCeiling) {
		try {
			const settings = await Settings.loadReadOnly({ cwd: ctx.cwd, agentDir: getAgentDir() });
			const ceiling = nativeStageRequestCeiling(settings, input.role);
			let maxRuntimeMs = 0;
			try {
				maxRuntimeMs = nativeStageMaxRuntimeMs(settings);
			} catch {
				maxRuntimeMs = 0;
			}
			stageCeiling = {
				hardCeiling: ceiling.hardCeiling,
				softBudget: ceiling.softBudget,
				maxRuntimeMs,
			};
		} catch {
			// will fail closed below
		}
	}
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

	try {
		if (signal?.aborted) {
			throw new DOMException("The operation was aborted", "AbortError");
		}
		if (!stageCeiling || stageCeiling.hardCeiling <= 0) {
			throw new Error("stage request hard ceiling is non-positive; execution refused");
		}
		if (stageCeiling.maxRuntimeMs <= 0) {
			throw new Error("task.maxRuntimeMs is missing or non-positive; native stage execution requires a positive runtime lease");
		}

		// 1. Query provider accounts and rate cards
		const [accountsView, rateCardsView] = await Promise.all([
			backend.workClient.providerAccounts(),
			backend.workClient.rateCards(),
		]);

		const nowIso = new Date().toISOString();
		const provider = resolvedRouteFields.resolvedProvider;

		// Select exactly one enabled, classified provider account compatible with resolved provider
		// and a currently effective qualified card/currency. Fail on zero or multiple eligible candidates.
		type EligibleCandidate = {
			account: NonNullable<typeof accountsView.accounts>[number];
			card: NonNullable<typeof rateCardsView.rate_cards>[number];
		};
		const candidates: EligibleCandidate[] = [];

		for (const acct of accountsView.accounts ?? []) {
			if (acct.provider !== provider) continue;
			if (acct.is_disabled) continue;
			if (!acct.budget_resource) continue;
			if (!acct.rate_card_version) continue;

			for (const c of rateCardsView.rate_cards ?? []) {
				if (c.provider !== acct.provider || c.version !== acct.rate_card_version) continue;
				if (c.qualification !== "qualified") continue;
				if (c.effective_from && nowIso < c.effective_from) continue;
				if (c.effective_until && nowIso >= c.effective_until) continue;
				if (Array.isArray(c.billing_modes) && !c.billing_modes.includes(acct.billing_mode)) continue;
				candidates.push({ account: acct, card: c });
			}
		}

		if (candidates.length === 0) {
			throw new Error(`no eligible candidate provider account and qualified rate card found for "${provider}"`);
		}
		if (candidates.length > 1) {
			throw new Error(`account_selection_ambiguous: multiple eligible candidate accounts found for "${provider}"`);
		}
		const { account, card } = candidates[0];

		// 3. Compute conservative usage ceiling
		const usageCeiling = nativeStageUsageCeiling(resolvedRoute.model, card, stageCeiling.hardCeiling);

		// 4. Quote budget
		const quote = await backend.quoteBudget({
			work_id: item.work_id,
			revision_id: item.revision.revision_id,
			candidate_id: identity.candidateId,
			attempt_id: input.attemptId ?? null,
			grant_id: input.grantId ?? null,
			role: input.role,
			launch_id: launch.launch_id,
			account_id: account.account_id,
			provider: resolvedRouteFields.resolvedProvider,
			model: resolvedRouteFields.resolvedModel,
			effort: resolvedRouteFields.requestedEffort,
			currency: card.currency,
			usage_ceiling: usageCeiling,
		});

		if (signal?.aborted) {
			throw new DOMException("The operation was aborted", "AbortError");
		}

		// 5. Reserve budget with deterministic replay identity
		const reservedAtMs = launch.reserved_at ? new Date(launch.reserved_at).getTime() : Date.now();
		const expiresAt = new Date(reservedAtMs + stageCeiling.maxRuntimeMs).toISOString();
		const transportAttemptId = deterministicUuid(`stage-reservation:${launch.launch_id}:${quote.quote_id}`);
		await backend.reserveBudget({
			scope_id: quote.scope_id,
			account_id: account.account_id,
			logical_call_id: launch.launch_id,
			transport_attempt_id: transportAttemptId,
			provider: resolvedRouteFields.resolvedProvider,
			model: resolvedRouteFields.resolvedModel,
			effort: resolvedRouteFields.requestedEffort,
			resource: quote.resource,
			worst_case_drawdown: quote.worst_case_amount,
			context_limit: resolvedRoute.model.contextWindow ?? 0,
			output_limit: resolvedRoute.model.maxTokens ?? 0,
			expires_at: expiresAt,
			launch_id: launch.launch_id,
			quote_id: quote.quote_id,
		});

		if (signal?.aborted) {
			throw new DOMException("The operation was aborted", "AbortError");
		}
	} catch (error) {
		const reason = error instanceof Error ? error.message : String(error);
		try {
			if (typeof backend.cancelStageLaunch === "function") {
				await backend.cancelStageLaunch(launch.launch_id, reason);
			}
		} catch (cancelErr) {
			const cancelMsg = cancelErr instanceof Error ? cancelErr.message : String(cancelErr);
			throw new Error(`${reason} (subsequent cancelStageLaunch failed: ${cancelMsg})`);
		}
		throw error;
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
