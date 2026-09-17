/**
 * workflow/provider-observation.ts — Pure typed capability classifier for
 * native provider request observation and authoritative outcome lookup.
 *
 * Encodes pre-effect correlation class and available post-effect identities
 * per configured native route. Today every configured and unconfigured route
 * evaluates to an explicit unsupported variant because no configured native
 * provider exposes both caller-bound durable correlation and authoritative
 * retrieval for dispatched preflight intents.
 */

export type PreEffectCorrelationClass = "none" | "transport_internal" | "session_scoped";

export type PostEffectIdentity = "provider_response_id" | "http_request_id";

export interface UnsupportedProviderObservationCapability {
	readonly lookup: "unsupported";
	readonly provider: string;
	readonly api: string;
	readonly preEffectCorrelation: PreEffectCorrelationClass;
	readonly postEffectIdentity: readonly PostEffectIdentity[];
	readonly reason: string;
}

export type ProviderObservationCapability = UnsupportedProviderObservationCapability;

export interface ProviderObservationTarget {
	readonly provider: string;
	readonly api: string;
}

interface CapabilityRecord {
	readonly provider: string;
	readonly api: string;
	readonly preEffectCorrelation: PreEffectCorrelationClass;
	readonly postEffectIdentity: readonly PostEffectIdentity[];
	readonly reason: string;
}

const CONFIGURED_RECORDS: readonly CapabilityRecord[] = Object.freeze([
	Object.freeze({
		provider: "google-antigravity",
		api: "google-gemini-cli",
		preEffectCorrelation: "none" as const,
		postEffectIdentity: Object.freeze([]) as readonly PostEffectIdentity[],
		reason: "google-antigravity/google-gemini-cli does not support authoritative outcome lookup; pre-effect correlation is none and post-effect identity is unavailable",
	}),
	Object.freeze({
		provider: "openai-codex",
		api: "openai-codex-responses",
		preEffectCorrelation: "session_scoped" as const,
		postEffectIdentity: Object.freeze(["provider_response_id"] as PostEffectIdentity[]),
		reason: "openai-codex/openai-codex-responses does not support authoritative outcome lookup; pre-effect correlation is session-scoped and lacks durable retrieval",
	}),
	Object.freeze({
		provider: "anthropic",
		api: "anthropic-messages",
		preEffectCorrelation: "transport_internal" as const,
		postEffectIdentity: Object.freeze(["provider_response_id", "http_request_id"] as PostEffectIdentity[]),
		reason: "anthropic/anthropic-messages does not support authoritative outcome lookup; pre-effect correlation is transport-internal and Messages API exposes no retrieval",
	}),
]);

/**
 * Pure typed capability classifier for provider observation and outcome lookup.
 *
 * Accepts provider/api identity directly or extracts from target/model.
 * Takes NO credentials, secrets, or I/O options.
 * Unknown provider/api pairs fail closed as unsupported without inferring support.
 */
export function providerObservationCapability(
	target: ProviderObservationTarget | { model: ProviderObservationTarget } | string,
	apiArg?: string,
): ProviderObservationCapability {
	let provider: string;
	let api: string;

	if (typeof target === "string") {
		provider = target;
		api = apiArg ?? "";
	} else if ("model" in target) {
		provider = target.model.provider;
		api = target.model.api;
	} else {
		provider = target.provider;
		api = target.api;
	}

	for (const record of CONFIGURED_RECORDS) {
		if (record.provider === provider && record.api === api) {
			return Object.freeze({
				lookup: "unsupported",
				provider: record.provider,
				api: record.api,
				preEffectCorrelation: record.preEffectCorrelation,
				postEffectIdentity: record.postEffectIdentity,
				reason: record.reason,
			});
		}
	}

	return Object.freeze({
		lookup: "unsupported",
		provider,
		api,
		preEffectCorrelation: "none",
		postEffectIdentity: Object.freeze([]),
		reason: `unsupported native provider/api pair ${provider}/${api}: no authoritative observation path configured`,
	});
}
