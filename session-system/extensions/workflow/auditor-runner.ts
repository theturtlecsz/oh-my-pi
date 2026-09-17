/**
 * workflow/auditor-runner.ts — Native, blocking auditor subprocess runner (OMP-168).
 *
 * Runs the installed `auditor` agent directly via the host's task executor,
 * with no model-transport copy/paste, no agent loop recreation, and no
 * prompt-enforced budget prose.
 */
import { randomUUID } from "node:crypto";
import { completeSimple, type AssistantMessage, type Usage } from "@oh-my-pi/pi-ai";
import { sha256Hex } from "@oh-my-pi/pi-work-client";
import { getAgentDir, Settings, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { formatModelSelectorValue, formatModelStringWithRouting } from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import { resolveAuditPolicy } from "./audit-policy";
import { discoverAgents, getAgent } from "@oh-my-pi/pi-coding-agent/task";
import { runSubprocess } from "@oh-my-pi/pi-coding-agent/task/executor";
import { nativeStageRouteCandidates, type NativeStageRole, type NativeStageRoute } from "./native-stage-profile";
import nativePreflightProbePrompt from "./native-preflight-probe.md" with { type: "text" };

export type NativeAuditUsage = Omit<Usage, "cost">;

export function stripUsageCost(usage: Usage | NativeAuditUsage): NativeAuditUsage {
	const { cost: _cost, ...measured } = usage as unknown as { cost?: unknown } & NativeAuditUsage;
	return measured;
}

export interface NativeStagePreflightAttempt {
	transportAttemptId: string;
	ordinal: number;
	route: NativeStageRoute;
	probeSha256: string;
	outcome: "selected" | "failed" | "cancelled";
	stopReason?: string | null;
	error?: string | null;
	requests: null;
	usage?: NativeAuditUsage | null;
	providerRequestId?: string | null;
}

export interface NativeAuditRunResult {
	started: boolean;
	payload?: string;
	error?: string;
	resolvedModel?: string;
	resolvedModelIsFallback?: boolean;
	usage?: NativeAuditUsage;
	requests?: number;
}

export type NativeAuditRunner = (
	taskBody: string,
	attemptId: string,
	signal?: AbortSignal,
) => Promise<NativeAuditRunResult>;

export type NativeStageRunnerOptions = {
	role: NativeStageRole;
	agentName?: string;
	/** Sealed implementation paths supplied by WorkService for implementer runs. */
	writeRoots?: readonly string[];
	/** Precompiled stage context. Host persists this before dispatch. */
	context?: string;
	/** Called at the provider dispatch boundary, before the first request leaves the host. */
	onHandoff?: () => void | Promise<void>;
	/** Ordered qualified routes; first credentialed/healthy route wins. */
	routes?: readonly NativeStageRoute[];
	/** Bound immutable route passed from host TCB sealing. */
	boundRoute?: NativeStageRoute;
	/** Reports each actual transport preflight probe attempt. */
	onPreflightAttempt?: (attempt: NativeStagePreflightAttempt) => void | Promise<void>;
	/** Reports the route that survived credential and transport preflight. */
	onRouteSelected?: (route: NativeStageRoute) => void;
	legacyAudit?: boolean;
};

const nativeStageAgentNames: Readonly<Record<NativeStageRole, string>> = {
	plan: "planner",
	implement: "implementer",
	frontier: "frontier",
	audit: "auditor",
};

export function nativeStageTaskInput(taskBody: string, context?: string): string {
	return context?.trim()
		? `${taskBody}\n\n<stage_context_data>\n${context.trim()}\n</stage_context_data>`
		: taskBody;
}

/**
 * Prepares a native stage runner for current extension context.
 *
 * Fails before a ledger launch reservation unless all preconditions exist:
 * 1. Discovers installed role-specific agent definition.
 * 2. Resolves exact allowlisted route through `ctx.models`.
 * 3. Verifies provider credentials and live transport for selected model
 *    (OMP-251) — a minimal probe completion must not error, so transport
 *    failures surface here instead of burning a reserved launch.
 * 4. Loads effective settings for `ctx.cwd` / `getAgentDir()` and disables
 *    inherited retry routes.
 */
export async function prepareNativeStageRunner(
	ctx: ExtensionContext,
	options: NativeStageRunnerOptions,
	signal?: AbortSignal,
): Promise<NativeAuditRunner> {
	if (signal?.aborted) throw new DOMException("Native stage preflight cancelled", "AbortError");
	const discovery = await discoverAgents(ctx.cwd);
	const agentName = options.agentName ?? nativeStageAgentNames[options.role];
	const agent = getAgent(discovery.agents, agentName);
	if (!agent) {
		throw new Error(`Installed "${agentName}" agent definition not found`);
	}
	if (!agent.output) {
		throw new Error(`Installed "${agentName}" agent definition is missing required output schema`);
	}

	let routes = options.routes;
	if (options.role === "audit") {
		const targetRoute = options.boundRoute;
		if (!targetRoute) {
			throw new Error("Immutable bound route required for native audit stage");
		}
		if (!targetRoute.boundPolicySha256) {
			throw new Error("Bound audit route missing boundPolicySha256");
		}
		const currentPolicyPre = resolveAuditPolicy(ctx.models);
		if (currentPolicyPre.policySha256 !== targetRoute.boundPolicySha256) {
			throw new Error(
				`Audit policy drift before preflight: current hash "${currentPolicyPre.policySha256}" does not match bound hash "${targetRoute.boundPolicySha256}"`,
			);
		}
		routes = [targetRoute];
	} else {
		routes = routes ?? nativeStageRouteCandidates(ctx.models, options.role);
	}
	const probeSha256 = sha256Hex(nativePreflightProbePrompt);
	let route: NativeStageRoute | undefined;
	let preflightError: Error | undefined;
	let ordinal = 0;

	// OMP-251: auditor transport preflight. Launch reservations are budgeted
	// (3 per attempt); prove credentials + endpoint connectivity BEFORE the
	// caller reserves one, so auth/network outages deny instead of burning
	// the audit budget with transport_failed settlements.
	for (const candidate of routes) {
		if (signal?.aborted) throw new DOMException("Native stage preflight cancelled", "AbortError");
		const stageModel = candidate.model;
		const apiKey = await ctx.modelRegistry.getApiKey(stageModel, undefined, { signal });
		if (apiKey === undefined) {
			preflightError = new Error(
				options.legacyAudit
					? `No provider credentials configured for @audit model ${stageModel.provider}/${stageModel.id} — authenticate the provider and retry`
					: `No provider credentials configured for native ${options.role} model ${stageModel.provider}/${stageModel.id} — authenticate the provider and retry`,
			);
			continue;
		}
		if (options.role === "audit") {
			const currentPolicyPostCreds = resolveAuditPolicy(ctx.models);
			if (currentPolicyPostCreds.policySha256 !== candidate.boundPolicySha256) {
				throw new Error(
					`Audit policy drift after credentials: current hash "${currentPolicyPostCreds.policySha256}" does not match bound hash "${candidate.boundPolicySha256}"`,
				);
			}
		}

		const transportAttemptId = randomUUID();
		const currentOrdinal = ordinal++;
		let probe: AssistantMessage | undefined;
		let thrownError: unknown;
		try {
			probe = await completeSimple(
				stageModel,
				{ messages: [{ role: "user", content: nativePreflightProbePrompt, timestamp: Date.now() }] },
				{ apiKey, maxTokens: 256, temperature: 0, disableReasoning: true, signal },
			);
		} catch (error: unknown) {
			thrownError = error;
		}

		if (thrownError !== undefined) {
			const isAbort =
				signal?.aborted ||
				(thrownError instanceof DOMException && thrownError.name === "AbortError") ||
				(thrownError instanceof Error && thrownError.name === "AbortError");
			if (isAbort) {
				const cancelText = options.legacyAudit
					? `@audit transport preflight cancelled for ${stageModel.provider}/${stageModel.id}`
					: `native ${options.role} transport preflight cancelled for ${stageModel.provider}/${stageModel.id}`;
				await options.onPreflightAttempt?.({
					transportAttemptId,
					ordinal: currentOrdinal,
					route: candidate,
					probeSha256,
					outcome: "cancelled",
					stopReason: null,
					error: cancelText,
					requests: null,
					usage: null,
					providerRequestId: null,
				});
				throw thrownError;
			}
			const failText = options.legacyAudit
				? `@audit transport preflight failed for ${stageModel.provider}/${stageModel.id}: ${thrownError instanceof Error ? thrownError.message : String(thrownError)}`
				: `native ${options.role} transport preflight failed for ${stageModel.provider}/${stageModel.id}: ${thrownError instanceof Error ? thrownError.message : String(thrownError)}`;
			preflightError = new Error(failText);
			await options.onPreflightAttempt?.({
				transportAttemptId,
				ordinal: currentOrdinal,
				route: candidate,
				probeSha256,
				outcome: "failed",
				stopReason: null,
				error: failText,
				requests: null,
				usage: null,
				providerRequestId: null,
			});
			continue;
		}

		if (!probe || probe.stopReason === "error") {
			const detail = probe?.errorMessage || "provider returned no detail";
			const failText = options.legacyAudit
				? `@audit transport preflight error for ${stageModel.provider}/${stageModel.id}: ${detail}`
				: `native ${options.role} transport preflight error for ${stageModel.provider}/${stageModel.id}: ${detail}`;
			preflightError = new Error(failText);
			await options.onPreflightAttempt?.({
				transportAttemptId,
				ordinal: currentOrdinal,
				route: candidate,
				probeSha256,
				outcome: "failed",
				stopReason: probe?.stopReason ?? "error",
				error: failText,
				requests: null,
				usage: probe?.usage ? stripUsageCost(probe.usage) : null,
				providerRequestId: probe?.responseId ?? null,
			});
			continue;
		}

		if (probe.stopReason === "aborted") {
			const detail = probe.errorMessage || "provider returned no detail";
			const abortText = options.legacyAudit
				? `@audit transport preflight aborted for ${stageModel.provider}/${stageModel.id}: ${detail}`
				: `native ${options.role} transport preflight aborted for ${stageModel.provider}/${stageModel.id}: ${detail}`;
			preflightError = new Error(abortText);
			await options.onPreflightAttempt?.({
				transportAttemptId,
				ordinal: currentOrdinal,
				route: candidate,
				probeSha256,
				outcome: "cancelled",
				stopReason: "aborted",
				error: abortText,
				requests: null,
				usage: probe.usage ? stripUsageCost(probe.usage) : null,
				providerRequestId: probe.responseId ?? null,
			});
			if (signal?.aborted) {
				throw new DOMException("Native stage preflight cancelled", "AbortError");
			}
			continue;
		}

		await options.onPreflightAttempt?.({
			transportAttemptId,
			ordinal: currentOrdinal,
			route: candidate,
			probeSha256,
			outcome: "selected",
			stopReason: probe.stopReason ?? null,
			error: null,
			requests: null,
			usage: probe.usage ? stripUsageCost(probe.usage) : null,
			providerRequestId: probe.responseId ?? null,
		});
		route = candidate;
		break;
	}
	if (!route) throw preflightError ?? new Error(`No qualified native ${options.role} route: primary unavailable or mismatched; allowed fallback routes exhausted`);
	options.onRouteSelected?.(route);

	const settings = await Settings.loadReadOnly({
		cwd: ctx.cwd,
		agentDir: getAgentDir(),
	});
	// Native stages own their route allowlist. Do not let ordinary session retry
	// chains silently switch this child to a role, parent, or provider outside
	// that allowlist after preflight has selected its route.
	settings.override("retry.modelFallback", false);
	const existingFallbackChains = settings.get("retry.fallbackChains") ?? {};
	settings.override(
		"retry.fallbackChains",
		Object.fromEntries(Object.keys(existingFallbackChains).map(role => [role, []])),
	);

	return async (
		taskBody: string,
		attemptId: string,
		signal?: AbortSignal,
	): Promise<NativeAuditRunResult> => {
		let started = false;
		try {
			if (options.role === "audit") {
				const currentPolicyPreExec = resolveAuditPolicy(ctx.models);
				if (currentPolicyPreExec.policySha256 !== route.boundPolicySha256) {
					return {
						started: false,
						error: `Audit policy drift before execution: current hash "${currentPolicyPreExec.policySha256}" does not match bound hash "${route.boundPolicySha256}"`,
					};
				}
			}
			const taskInput = nativeStageTaskInput(taskBody, options.context);
			const isAudit = options.role === "audit";
			const result = await runSubprocess({
				index: 0,
				cwd: ctx.cwd,
				nativeStageWriteRoots: options.writeRoots,
				onNativeStageHandoff: options.onHandoff,
				agent,
				task: taskInput,
				modelOverride: route.requestedSelector,
				resolvedModel: isAudit ? route.model : undefined,
				thinkingLevel: isAudit ? route.effort : undefined,
				modelRegistry: ctx.modelRegistry,
				authStorage: ctx.modelRegistry?.authStorage,
				getApiKey: ctx.modelRegistry?.resolver
					? requestModel => ctx.modelRegistry.resolver(requestModel, attemptId)
					: undefined,
				modelRole: options.legacyAudit ? "audit" : options.role,
				outputSchema: agent.output,
				outputSchemaSource: "agent",
				outputSchemaMode: "strict",
				taskDepth: ctx.taskDepth,
				restrictToolNames: true,
				enableMCP: false,
				enableIrc: false,
				enableLsp: true,
				keepAlive: false,
				id: attemptId,
				signal,
				settings,
			});

			started = Boolean(result.requests && result.requests > 0);
			const usage = result.usage ? stripUsageCost(result.usage) : undefined;
			const requests = typeof result.requests === "number" ? result.requests : undefined;
			if (result.resolvedModel && !nativeResolvedModelMatchesRoute(result.resolvedModel, route)) {
				return {
					started,
					error: `native ${options.role} execution served disallowed model ${result.resolvedModel}; expected ${route.requestedSelector}`,
					resolvedModel: result.resolvedModel,
					resolvedModelIsFallback: result.resolvedModelIsFallback,
					...(usage ? { usage } : {}),
					...(requests !== undefined ? { requests } : {}),
				};
			}
			const payload =
				typeof result.output === "string" && result.output.trim().length > 0
					? result.output
					: undefined;

			return {
				started,
				payload,
				error: result.error || result.stderr || undefined,
				resolvedModel: result.resolvedModel,
				resolvedModelIsFallback: result.resolvedModelIsFallback,
				...(usage ? { usage } : {}),
				...(requests !== undefined ? { requests } : {}),
			};
		} catch (error) {
			return {
				started,
				error: error instanceof Error ? error.message : String(error),
			};
		}
	};
}

function nativeResolvedModelMatchesRoute(resolvedModel: string, route: NativeStageRoute): boolean {
	const expected = formatModelSelectorValue(formatModelStringWithRouting(route.model), route.effort);
	return resolvedModel === expected || resolvedModel === route.requestedSelector;
}

export async function prepareNativeAuditRunner(
	ctx: ExtensionContext,
	signal?: AbortSignal,
	boundRoute?: NativeStageRoute,
): Promise<NativeAuditRunner> {
	const resolvedBoundRoute = boundRoute ?? resolveAuditPolicy(ctx.models).route;
	return prepareNativeStageRunner(
		ctx,
		{
			role: "audit",
			agentName: "auditor",
			legacyAudit: true,
			boundRoute: resolvedBoundRoute,
		},
		signal,
	);
}
