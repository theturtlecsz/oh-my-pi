import type { ExtensionModelQuery } from "@oh-my-pi/pi-coding-agent";
import type { Api, Effort, Model, Provider, ThinkingControlMode } from "@oh-my-pi/pi-ai";

export type NativeStageRole = "plan" | "implement" | "frontier" | "audit";

type NativeRouteSpec = {
	provider: Provider;
	modelId: string;
	api: Api;
	effort: Effort;
	wireModelId: string;
	thinkingMode: ThinkingControlMode;
};

import { resolveAuditPolicy } from "./audit-policy";

export type NativeStageRoute = {
	role: NativeStageRole;
	requestedSelector: string;
	model: Model;
	isFallback: boolean;
	profileVersion: 1;
	routeVersion: 1;
	effort: Effort;
	boundPolicySha256?: string;
};

const high = "high" as Effort;

type NonAuditStageRole = Exclude<NativeStageRole, "audit">;

const ROUTES: Record<NonAuditStageRole, { primary: NativeRouteSpec; fallbacks: readonly NativeRouteSpec[] }> = {
	plan: {
		primary: { provider: "anthropic", modelId: "claude-fable-5-1", api: "anthropic-messages", effort: high, wireModelId: "claude-fable-5-1", thinkingMode: "anthropic-adaptive" },
		fallbacks: [],
	},
	implement: {
		primary: { provider: "google-antigravity", modelId: "gemini-3.8-flash", api: "google-gemini-cli", effort: high, wireModelId: "gemini-3.8-flash-high", thinkingMode: "google-level" },
		fallbacks: [{ provider: "openai-codex", modelId: "gpt-5.6-luna", api: "openai-codex-responses", effort: high, wireModelId: "gpt-5.6-luna", thinkingMode: "effort" }],
	},
	frontier: {
		primary: { provider: "anthropic", modelId: "claude-fable-5-1", api: "anthropic-messages", effort: high, wireModelId: "claude-fable-5-1", thinkingMode: "anthropic-adaptive" },
		fallbacks: [],
	},
};

function routeMatches(model: Model, route: NativeRouteSpec): boolean {
	if (model.provider !== route.provider || model.id !== route.modelId || model.api !== route.api) return false;
	const thinking = model.thinking;
	if (!thinking || thinking.mode !== route.thinkingMode || !thinking.efforts.includes(route.effort)) return false;
	const wireModelId = thinking.effortRouting?.[route.effort] ?? model.requestModelId ?? model.id;
	return wireModelId === route.wireModelId;
}

function selector(route: NativeRouteSpec): string {
	return `${route.provider}/${route.modelId}:${route.effort}`;
}

export function nativeStageRouteSpecs(): Readonly<Record<NonAuditStageRole, { primary: NativeRouteSpec; fallbacks: readonly NativeRouteSpec[] }>> {
	return ROUTES;
}

export function nativeStageRouteCandidates(models: ExtensionModelQuery, role: NativeStageRole): NativeStageRoute[] {
	if (role === "audit") {
		return [resolveAuditPolicy(models).route];
	}
	const profile = ROUTES[role];
	const candidates: NativeStageRoute[] = [];
	for (const [index, spec] of [profile.primary, ...profile.fallbacks].entries()) {
		const model = models.resolve(selector(spec));
		if (model && routeMatches(model, spec)) {
			candidates.push({ role, requestedSelector: selector(spec), model, isFallback: index > 0, profileVersion: 1, routeVersion: 1, effort: spec.effort });
		}
	}
	return candidates;
}

export function resolveNativeStageRoute(models: ExtensionModelQuery, role: NativeStageRole): NativeStageRoute {
	if (role === "audit") {
		return resolveAuditPolicy(models).route;
	}
	const candidates = nativeStageRouteCandidates(models, role);
	if (candidates[0]) return candidates[0];
	throw new Error(`No qualified native ${role} route: primary ${selector(ROUTES[role].primary)} unavailable or mismatched; allowed fallback routes exhausted`);
}
