/**
 * Router-mode transport for the Jev measurement harness.
 *
 * The OMP-298 harness talks to TypeSafe's typed decision API (POST
 * /v1/systemone {model, state, questions}). That endpoint rejects the owner's
 * OpenRouter key, and no TypeSafe key is being sought, so the measurement moves
 * to OpenRouter's chat-completions router `typesafe/jev-router` — the only Jev
 * surface OpenRouter exposes. A router returns a routed completion, not a
 * probability map, so every decision here is reduced to a single-label
 * classification whose output is the router's own text.
 *
 * Safeguards kept from OMP-358:
 *   - the transport may only reach `https://openrouter.ai` (`makeOpenRouterFetch`
 *     throws on any other origin), and the routed model id is pinned, not
 *     caller-selectable;
 *   - the key is read at run time from `~/.config/omp/jev.env` only
 *     (`readJevEnvKey`), never from the repo and never logged. Tests never call
 *     the default path: they pass `apiKey`, a `configDir`, or inject `fetch`, so
 *     no test reads that file.
 */

import * as os from "node:os";
import * as path from "node:path";
import { parseEnvFile, type FetchImpl } from "@oh-my-pi/pi-utils";

/** OpenRouter API origin. The router transport may reach no other host. */
export const OPENROUTER_BASE_URL = "https://openrouter.ai";
/** OpenRouter chat-completions path. */
export const OPENROUTER_CHAT_PATH = "/api/v1/chat/completions";
/** OpenRouter per-generation record path (usage, routed model, reasoning effort). */
export const OPENROUTER_GENERATION_PATH = "/api/v1/generation";
/** The only model this transport sends: OpenRouter's Jev Router. */
export const OPENROUTER_JEV_ROUTER_MODEL = "typesafe/jev-router";
/** Environment key holding the owner's OpenRouter key (`~/.config/omp/jev.env`). */
export const ROUTER_ENV_KEY = "OPENROUTER_API_KEY";
/** Directory name under the config home that holds the owner's jev env file. */
export const ROUTER_ENV_DIR = "omp";
/** Env file name inside {@link ROUTER_ENV_DIR}. */
export const ROUTER_ENV_FILE = "jev.env";

export type RouterOutcome = "ok" | "http_error" | "network_error" | "timeout" | "malformed";

/** One terminal attempt, in the shape `JevUsageEntry` already has. */
export interface RouterUsageEntry {
	requestId: string;
	serverId?: string;
	feature: string;
	outcome: RouterOutcome;
	status?: number;
	latencyMs: number;
	stateChars: number;
	truncated: boolean;
	attempt: number;
	/** Tokens the OpenRouter response reported for this call. */
	promptTokens?: number;
	completionTokens?: number;
	/** Routed model id reported on the response (`model` field). */
	routedModel?: string;
	/**
	 * Reasoning effort the router picked, read from the OpenRouter generation
	 * record. `undefined` when the record was not fetched.
	 */
	routedReasoningEffort?: string;
	/**
	 * Actual USD charged for this call, read from the OpenRouter generation
	 * record (`total_cost`). `undefined` when the record was not fetched, in
	 * which case cost is reported from tokens and the routed model's price.
	 */
	costUsd?: number;
}

export class RouterTransportError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "RouterTransportError";
	}
}

export interface OpenRouterRouteDeps {
	recordUsage: (entry: RouterUsageEntry) => void;
	apiKey?: string;
	fetch?: FetchImpl;
	now?: () => number;
	feature?: string;
	uuid?: () => string;
	budgetMs?: number;
	model?: string;
	maxAttempts?: number;
	/**
	 * Fetch the OpenRouter generation record for a completed call, to read the
	 * actual charged cost and the router's reasoning metadata. Off by default:
	 * the owner slice enables it (one extra GET per call), and tests drive it
	 * against the fake transport.
	 */
	generationRecords?: boolean;
}

/** Whole-call budget shared across attempts. */
export const ROUTER_BUDGET_MS = 30_000;
/** At most one retry per call. */
export const ROUTER_MAX_ATTEMPTS = 2;

export interface RouterDecision {
	text?: string;
	routedModel?: string;
	usage?: unknown;
	transport: RouterUsageEntry;
}

/** Config home the owner's jev env file lives under (`$XDG_CONFIG_HOME` or `~/.config`). */
export function defaultConfigHome(): string {
	return process.env.XDG_CONFIG_HOME || path.join(os.homedir(), ".config");
}

/** Absolute path of the owner's jev env file under a config home. */
export function jevEnvPath(configHome = defaultConfigHome()): string {
	return path.join(configHome, ROUTER_ENV_DIR, ROUTER_ENV_FILE);
}

/**
 * Read the owner's OpenRouter key from `~/.config/omp/jev.env` at run time.
 *
 * `configHome` defaults to `$XDG_CONFIG_HOME` or `~/.config` (tests pass a
 * TempDir). A missing file or key returns `undefined`; the value is never
 * logged and is never written to the repo.
 */
export function readJevEnvKey(configHome = defaultConfigHome()): string | undefined {
	const value = parseEnvFile(jevEnvPath(configHome))[ROUTER_ENV_KEY];
	return value && value.trim() ? value.trim() : undefined;
}

/**
 * Guarded fetch: any request whose origin is not the OpenRouter origin throws,
 * so a misconfigured run cannot leak the key to another host.
 */
export function makeOpenRouterFetch(): FetchImpl {
	const allowedOrigin = new URL(OPENROUTER_BASE_URL).origin;
	return async (input, init) => {
		const urlStr =
			typeof input === "string" ? input : input instanceof URL ? input.toString() : (input as Request).url;
		const reqOrigin = new URL(urlStr).origin;
		if (reqOrigin !== allowedOrigin) {
			throw new RouterTransportError(
				`Router side must never reach a host other than ${allowedOrigin}: attempted ${urlStr}`,
			);
		}
		return globalThis.fetch(input, init);
	};
}

/** Extract the first text content of an OpenAI-wire assistant message. */
function firstMessageText(json: unknown): string | undefined {
	if (!json || typeof json !== "object") return undefined;
	const choices = (json as { choices?: unknown }).choices;
	if (!Array.isArray(choices) || choices.length === 0) return undefined;
	const message = (choices[0] as { message?: unknown }).message;
	if (!message || typeof message !== "object") return undefined;
	const content = (message as { content?: unknown }).content;
	if (typeof content === "string") return content;
	if (Array.isArray(content)) {
		const parts: string[] = [];
		for (const part of content) {
			if (part && typeof part === "object" && typeof (part as { text?: unknown }).text === "string") {
				parts.push((part as { text: string }).text);
			}
		}
		return parts.join("");
	}
	return undefined;
}

function numberField(obj: unknown, key: string): number | undefined {
	if (!obj || typeof obj !== "object") return undefined;
	const value = (obj as Record<string, unknown>)[key];
	return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function stringField(obj: unknown, key: string): string | undefined {
	if (!obj || typeof obj !== "object") return undefined;
	const value = (obj as Record<string, unknown>)[key];
	return typeof value === "string" ? value : undefined;
}

/**
 * Send one chat completion through the router. Returns the text the routed
 * model produced (or `undefined` on transport failure), plus the tokens and
 * routed model the response reported. The transport entry is always emitted so
 * the harness can count transport failures and read actual usage.
 */
export async function routeCompletion(prompt: string, deps: OpenRouterRouteDeps): Promise<RouterDecision> {
	const fetchImpl: FetchImpl = deps.fetch ?? globalThis.fetch;
	const now = deps.now ?? Date.now;
	const uuid = deps.uuid ?? (() => crypto.randomUUID());
	const feature = deps.feature ?? "router_measurement";
	const model = deps.model ?? OPENROUTER_JEV_ROUTER_MODEL;
	const budgetMs = deps.budgetMs ?? ROUTER_BUDGET_MS;
	const maxAttempts = deps.maxAttempts ?? ROUTER_MAX_ATTEMPTS;
	if (model !== OPENROUTER_JEV_ROUTER_MODEL) {
		throw new RouterTransportError(`Router side may only send ${OPENROUTER_JEV_ROUTER_MODEL}; refused ${model}`);
	}
	const apiKey = deps.apiKey;
	if (!apiKey) {
		throw new RouterTransportError("Router side has no OpenRouter key (env read must happen at run time)");
	}

	const url = `${OPENROUTER_BASE_URL}${OPENROUTER_CHAT_PATH}`;
	const deadline = now() + budgetMs;
	let attempt = 0;
	let lastEntry: RouterUsageEntry | undefined;

	while (attempt < maxAttempts) {
		const remaining = deadline - now();
		if (remaining <= 0) break;
		attempt += 1;
		const requestId = uuid();
		const started = now();
		const controller = new AbortController();
		const timer = setTimeout(
			() => controller.abort(new DOMException("The operation timed out.", "TimeoutError")),
			remaining,
		);
		let outcome: RouterOutcome;
		let status: number | undefined;
		let serverId: string | undefined;
		let text: string | undefined;
		let routedModel: string | undefined;
		let usage: unknown;

		try {
			const response = await fetchImpl(url, {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					Authorization: `Bearer ${apiKey}`,
					"X-Request-Id": requestId,
					"HTTP-Referer": OPENROUTER_BASE_URL,
					"X-Title": "omp jev router measurement",
				},
				body: JSON.stringify({
					model,
					messages: [{ role: "user", content: prompt }],
					max_tokens: 32,
					temperature: 0,
				}),
				signal: controller.signal,
			});
			status = response.status;
			if (!response.ok) {
				outcome = "http_error";
			} else {
				let json: unknown;
				try {
					json = await response.json();
				} catch {
					json = undefined;
				}
				serverId = stringField(json, "id");
				routedModel = stringField(json, "model");
				usage = (json as { usage?: unknown }).usage;
				text = firstMessageText(json);
				outcome = text === undefined ? "malformed" : "ok";
			}
		} catch {
			outcome = controller.signal.aborted ? "timeout" : "network_error";
		} finally {
			clearTimeout(timer);
		}

		lastEntry = {
			requestId,
			serverId,
			feature,
			outcome,
			status,
			latencyMs: now() - started,
			stateChars: prompt.length,
			truncated: false,
			attempt,
			promptTokens: numberField(usage, "prompt_tokens"),
			completionTokens: numberField(usage, "completion_tokens"),
			routedModel,
		};
		deps.recordUsage(lastEntry);

		if (outcome === "ok") {
			if (deps.generationRecords && serverId) {
				const record = await fetchGenerationRecord(serverId, deps);
				if (record) {
					lastEntry.costUsd = record.totalCostUsd;
					lastEntry.routedReasoningEffort = record.reasoning;
					if (record.model) {
						lastEntry.routedModel = record.model;
						routedModel = record.model;
					}
				}
			}
			return { text, routedModel, usage, transport: lastEntry };
		}
		if (outcome === "timeout") break;
		if (outcome === "http_error" && status !== undefined && status < 500) break;
		if (outcome === "malformed") break;
	}

	if (!lastEntry) {
		lastEntry = {
			requestId: uuid(),
			feature,
			outcome: "timeout",
			latencyMs: 0,
			stateChars: prompt.length,
			truncated: false,
			attempt: 0,
		};
		deps.recordUsage(lastEntry);
	}
	return { transport: lastEntry };
}

/**
 * Read the first label the routed model chose, restricted to `allowed`.
 * Anything else (extra words, punctuation, an off-list label) is `undefined`,
 * which the harness counts as an off-list/uncertain answer rather than a guess.
 */
export function parseFirstLabel(text: string, allowed: readonly string[]): string | undefined {
	const words = text
		.toLowerCase()
		.split(/[^a-z0-9_]+/g)
		.filter(Boolean);
	for (const word of words) {
		if (allowed.includes(word)) return word;
	}
	return undefined;
}

/** Yes/no reading of a routed label. */
export function parseYesNo(text: string): boolean | undefined {
	const word = parseFirstLabel(text, ["yes", "no", "true", "false"]);
	if (word === "yes" || word === "true") return true;
	if (word === "no" || word === "false") return false;
	return undefined;
}

/**
 * Read an OpenRouter generation record: the actual charged cost and the model
 * and reasoning effort the router picked. Returns `undefined` on any failure —
 * cost then falls back to tokens x price, and the report says so.
 */
export async function fetchGenerationRecord(
	generationId: string,
	deps: Pick<OpenRouterRouteDeps, "apiKey" | "fetch" | "now">,
): Promise<{ totalCostUsd?: number; model?: string; reasoning?: string } | undefined> {
	const fetchImpl: FetchImpl = deps.fetch ?? globalThis.fetch;
	const apiKey = deps.apiKey;
	if (!apiKey || !generationId) return undefined;
	try {
		const response = await fetchImpl(`${OPENROUTER_BASE_URL}${OPENROUTER_GENERATION_PATH}?id=${generationId}`, {
			method: "GET",
			headers: { Authorization: `Bearer ${apiKey}` },
		});
		if (!response.ok) return undefined;
		const json: unknown = await response.json();
		const data = (json as { data?: unknown }).data ?? json;
		return {
			totalCostUsd: numberField(data, "total_cost"),
			model: stringField(data, "model"),
			reasoning: stringField(data, "reasoning"),
		};
	} catch {
		return undefined;
	}
}
