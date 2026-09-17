import type { Effort, Model } from "@oh-my-pi/pi-ai";
import type { ExtensionModelQuery } from "@oh-my-pi/pi-coding-agent";
import { canonicalJson, sha256Hex } from "@oh-my-pi/pi-work-client";

export interface AuditPolicy {
	policy_version: 1;
	alias: "@audit";
	matched_pattern: string;
	provider: string;
	model_id: string;
	api: string;
	requested_effort: string;
	effective_effort: string;
	wire_model_id: string;
}

export interface AuditRoute {
	role: "audit";
	requestedSelector: string;
	model: Model;
	isFallback: false;
	profileVersion: 1;
	routeVersion: 1;
	effort: Effort;
	boundPolicySha256: string;
}

export interface ResolvedAuditPolicy {
	policy: AuditPolicy;
	policySha256: string;
	route: AuditRoute;
}

export function resolveAuditPolicy(models: ExtensionModelQuery): ResolvedAuditPolicy {
	const selection = models.resolveSelection("@audit");

	if (!selection.model) {
		throw new Error("Audit policy failed: no model resolved for @audit");
	}

	if (selection.warning) {
		throw new Error(`Audit policy failed: ${selection.warning}`);
	}

	if (selection.matchedPatternIndex !== 0) {
		throw new Error(
			`Audit policy failed: matched pattern index ${selection.matchedPatternIndex}, expected primary pattern (substitution disallowed)`,
		);
	}

	if (selection.explicitThinkingLevel !== true) {
		throw new Error("Audit policy failed: explicit thinking level required for @audit");
	}

	if (selection.requestedThinkingLevel === undefined || selection.requestedThinkingLevel === "auto") {
		throw new Error("Audit policy failed: @audit requested thinking level cannot be auto or undefined");
	}

	if (selection.thinkingLevel === undefined || selection.thinkingLevel === "auto") {
		throw new Error("Audit policy failed: @audit effective thinking level cannot be auto or undefined");
	}

	if (selection.requestedThinkingLevel !== selection.thinkingLevel) {
		throw new Error(`Audit policy failed: thinking level clamped from ${selection.requestedThinkingLevel} to ${selection.thinkingLevel}`);
	}

	if (!selection.matchedPattern) {
		throw new Error("Audit policy failed: missing matchedPattern metadata");
	}

	if (
		selection.model.provider !== "openai-codex" ||
		selection.model.id !== "gpt-5.6-sol" ||
		selection.thinkingLevel !== "medium"
	) {
		throw new Error(
			`Audit policy failed: native auditor obligation requires openai-codex/gpt-5.6-sol:medium, got ${selection.model.provider}/${selection.model.id}:${selection.thinkingLevel}`,
		);
	}

	const modelSnapshot = Object.freeze(structuredClone(selection.model));
	const effort = selection.thinkingLevel as Effort;
	const wireModelId = modelSnapshot.thinking?.effortRouting?.[effort] ?? modelSnapshot.requestModelId ?? modelSnapshot.id;

	const policy: AuditPolicy = Object.freeze({
		policy_version: 1,
		alias: "@audit",
		matched_pattern: selection.matchedPattern,
		provider: modelSnapshot.provider,
		model_id: modelSnapshot.id,
		api: modelSnapshot.api,
		requested_effort: String(selection.requestedThinkingLevel),
		effective_effort: String(selection.thinkingLevel),
		wire_model_id: wireModelId,
	});

	const policySha256 = sha256Hex(canonicalJson(policy));
	const requestedSelector = `${modelSnapshot.provider}/${modelSnapshot.id}:${effort}`;

	const route: AuditRoute = Object.freeze({
		role: "audit",
		requestedSelector,
		model: modelSnapshot,
		isFallback: false,
		profileVersion: 1,
		routeVersion: 1,
		effort,
		boundPolicySha256: policySha256,
	});

	return {
		policy,
		policySha256,
		route,
	};
}
