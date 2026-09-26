/**
 * Jev (typesafe) decision client — a thin `fetch` wrapper over the
 * `/v1/systemone` endpoint. No SDK: direct POST keeps the dependency surface at zero.
 *
 * The client is deliberately fail-open: failure modes (disabled, missing
 * key, timeout, HTTP error, malformed body, off-options) resolve to `undefined`
 * so callers can fall back without a try/catch. Each real attempt is reported
 * through {@link JevDeps.recordUsage} for session-level accounting.
 */

import { $pickenv, type FetchImpl, logger } from "@oh-my-pi/pi-utils";
import { getDefault, type SettingPath, type SettingValue, settings } from "../config/settings";

/** Model id sent on every request. */
export const JEV_MODEL = "jev-latest";
/** Maximum state characters sent; longer states are head-truncated with a marker. */
export const JEV_STATE_CAP = 8000;
/** Whole-call budget shared across attempts. */
export const JEV_BUDGET_MS = 3000;
/** At most one retry per call. */
export const JEV_MAX_ATTEMPTS = 2;
/** Auth-storage provider id for the typesafe credential. */
export const JEV_PROVIDER = "typesafe";
/** Environment fallback when no stored credential exists. */
export const JEV_ENV_KEY = "TYPESAFE_API_KEY";
export const JEV_DEFAULT_BASE_URL = "https://api.typesafe.ai";

/** Choice question sent to the decision service. */
export type JevChoiceQuestion = {
	type: "choice";
	instructions: string;
	options: string[];
};

/** Yes/no (noul) question sent to the decision service. */
export type JevNoulQuestion = {
	type: "noul";
	instructions: string;
};

/** A question sent to the decision service. */
export type JevQuestion = JevChoiceQuestion | JevNoulQuestion;

/** Questions dictionary keyed by question name. */
export type JevQuestions = Record<string, JevQuestion>;

/** Choice answer distribution over options. */
export type JevChoiceAnswer = {
	probabilities: Record<string, number>;
};

/** Probability answer for yes/no question. */
export type JevNoulAnswer = {
	probability: number;
};

/** A single answer: distribution over options, or a single probability. */
export type JevAnswer = JevChoiceAnswer | JevNoulAnswer;

/** Answers keyed by question name. */
export type JevAnswers = Record<string, JevAnswer>;

/** Terminal outcome of one attempt. */
export type JevOutcome = "ok" | "http_error" | "network_error" | "timeout" | "malformed" | "off_list" | "off_options";

/** One accounting entry, emitted per attempt. */
export interface JevUsageEntry {
	requestId: string;
	serverId?: string;
	feature: string;
	outcome: JevOutcome;
	latencyMs: number;
	stateChars: number;
	truncated: boolean;
	attempt: number;
}

/** Function signature for reading settings in JevDeps. */
export type JevSettingsGetter = (<P extends SettingPath>(path: P) => SettingValue<P>) | ((path: string) => unknown);

/** Dependencies for {@link decide}. */
export interface JevDeps {
	recordUsage: (entry: JevUsageEntry) => void;
	getSetting?: JevSettingsGetter;
	getApiKey?: (provider?: string) => Promise<string | undefined> | string | undefined;
	fetch?: FetchImpl;
	now?: () => number;
	feature?: string;
	uuid?: () => string;
	budgetMs?: number;
}

// ── Helpers ────────────────────────────────────────────────────────────────

function readSetting<P extends SettingPath>(deps: JevDeps, path: P): SettingValue<P> {
	if (deps.getSetting) {
		try {
			const val = deps.getSetting(path);
			if (val !== undefined) {
				return val as SettingValue<P>;
			}
			return getDefault(path);
		} catch {
			return getDefault(path);
		}
	}
	try {
		return settings.get(path);
	} catch {
		return getDefault(path);
	}
}

async function resolveApiKey(deps: JevDeps): Promise<string | undefined> {
	if (deps.getApiKey) {
		try {
			const key = await deps.getApiKey(JEV_PROVIDER);
			if (key) return key;
		} catch {
			// Fall through to environment
		}
	}
	return $pickenv(JEV_ENV_KEY);
}

/** Truncate state to `cap` chars with ellipsis marker if needed. */
export function truncateState(state: string, cap = JEV_STATE_CAP): { state: string; truncated: boolean } {
	if (state.length <= cap) {
		return { state, truncated: false };
	}
	const removed = state.length - cap;
	return {
		state: `${state.slice(0, cap)}…[truncated ${removed} chars]`,
		truncated: true,
	};
}

function readServerId(json: unknown): string | undefined {
	if (!json || typeof json !== "object") return undefined;
	const id = (json as { id?: unknown }).id;
	return typeof id === "string" ? id : undefined;
}

function validateAnswers(
	json: unknown,
	questions: JevQuestions,
): { ok: true; answers: JevAnswers } | { ok: false; outcome: "malformed" | "off_list" } {
	if (!json || typeof json !== "object") return { ok: false, outcome: "malformed" };
	const rawAnswers = (json as { answers?: unknown }).answers;
	if (!rawAnswers || typeof rawAnswers !== "object") return { ok: false, outcome: "malformed" };

	const answers: JevAnswers = {};
	for (const [name, question] of Object.entries(questions)) {
		const raw =
			(rawAnswers as Record<string, unknown>)[name] ??
			(rawAnswers as Record<string, unknown>)[question.instructions];
		if (!raw || typeof raw !== "object") return { ok: false, outcome: "malformed" };

		if (question.type === "choice") {
			const rawProbs = (raw as { probabilities?: unknown }).probabilities;
			if (!rawProbs || typeof rawProbs !== "object") return { ok: false, outcome: "malformed" };

			const allowed = new Set(question.options);
			const normalized: Record<string, number> = {};
			for (const [option, value] of Object.entries(rawProbs as Record<string, unknown>)) {
				if (typeof value !== "number" || !Number.isFinite(value)) {
					return { ok: false, outcome: "malformed" };
				}
				if (!allowed.has(option)) {
					return { ok: false, outcome: "off_list" };
				}
				normalized[option] = value;
			}
			answers[name] = { probabilities: normalized };
		} else {
			const prob =
				typeof (raw as { probability?: unknown }).probability === "number"
					? (raw as { probability: number }).probability
					: typeof raw === "number"
						? raw
						: undefined;
			if (prob === undefined || !Number.isFinite(prob)) {
				return { ok: false, outcome: "malformed" };
			}
			answers[name] = { probability: prob };
		}
	}
	return { ok: true, answers };
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * Ask the decision service one batch of questions. Returns the answers keyed by
 * each question's name, or `undefined` on any failure. At most one retry is
 * made, inside the whole-call budget, and never after a timeout.
 */
export async function decide(state: string, questions: JevQuestions, deps: JevDeps): Promise<JevAnswers | undefined> {
	const now = deps.now ?? Date.now;
	const feature = deps.feature ?? "jev";

	if (!readSetting(deps, "jev.enabled")) return undefined;

	const apiKey = await resolveApiKey(deps);
	if (!apiKey) return undefined;

	const baseUrl = ((readSetting(deps, "jev.baseUrl") as string | undefined) ?? JEV_DEFAULT_BASE_URL).replace(
		/\/+$/,
		"",
	);
	const url = `${baseUrl}/v1/systemone`;
	const { state: sentState, truncated } = truncateState(state);
	const stateChars = state.length;
	const body = { model: JEV_MODEL, state: sentState, questions };
	const fetchImpl: FetchImpl = deps.fetch ?? globalThis.fetch;
	const uuid = deps.uuid ?? (() => crypto.randomUUID());
	const budgetMs = deps.budgetMs ?? JEV_BUDGET_MS;
	const deadline = now() + budgetMs;

	let attempt = 0;
	let lastOutcome: JevOutcome = "http_error";
	let lastRequestId = "";
	let lastLatencyMs = 0;

	while (attempt < JEV_MAX_ATTEMPTS) {
		const remaining = deadline - now();
		if (remaining <= 0) break;
		attempt += 1;
		const requestId = uuid();
		lastRequestId = requestId;
		const started = now();
		const controller = new AbortController();
		const timer = setTimeout(
			() => controller.abort(new DOMException("The operation timed out.", "TimeoutError")),
			remaining,
		);
		let outcome: JevOutcome;
		let status: number | undefined;
		let serverId: string | undefined;
		let answers: JevAnswers | undefined;

		try {
			const response = await fetchImpl(url, {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Request-Id": requestId,
					Authorization: `Bearer ${apiKey}`,
				},
				body: JSON.stringify(body),
				signal: controller.signal,
			});
			if (!response.ok) {
				outcome = "http_error";
				status = response.status;
			} else {
				let json: unknown;
				try {
					json = await response.json();
				} catch {
					json = undefined;
				}
				const validated = validateAnswers(json, questions);
				serverId = readServerId(json);
				if (validated.ok) {
					outcome = "ok";
					answers = validated.answers;
				} else {
					outcome = validated.outcome;
				}
			}
		} catch {
			outcome = controller.signal.aborted ? "timeout" : "network_error";
		} finally {
			clearTimeout(timer);
		}

		lastLatencyMs = now() - started;
		lastOutcome = outcome;

		deps.recordUsage({
			requestId,
			serverId,
			feature,
			outcome,
			latencyMs: lastLatencyMs,
			stateChars,
			truncated,
			attempt,
		});

		if (outcome === "ok") {
			return answers;
		}

		// A timeout consumes the budget; never retry after one.
		if (outcome === "timeout") break;
		// Client errors (4xx) are not transient; malformed/off-list are 2xx bodies.
		if (outcome === "http_error" && status !== undefined && status < 500) break;
		if (outcome === "malformed" || outcome === "off_list") break;
	}

	if (attempt === 0) return undefined;

	logger.warn("jev: decision call failed", {
		feature,
		outcome: lastOutcome,
		attempts: attempt,
		requestId: lastRequestId,
		latencyMs: lastLatencyMs,
	});
	return undefined;
}
