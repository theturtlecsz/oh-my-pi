/**
 * One headless orchestrator-worker turn.
 *
 * The CLI jail (private cwd, HOME, and agent dir) is in place before this
 * module is require-loaded, so importing it cannot read a candidate `.env`.
 * The turn reuses Agent and pi-ai `streamSimple` with the scoped read/write
 * tools rooted at `/work`. It does not discover sessions, plugins, hooks, MCP
 * servers, fallbacks, or a shell.
 */
import * as fs from "node:fs";
import type { AgentEvent, StreamFn } from "@oh-my-pi/pi-agent-core";
import { Agent } from "@oh-my-pi/pi-agent-core";
import type { FetchImpl, Model, ResolvedGoogleCompat } from "@oh-my-pi/pi-ai";
import { streamSimple } from "@oh-my-pi/pi-ai";
import { prompt } from "@oh-my-pi/pi-utils";
import workerPrompt from "./prompts/orchestrator-worker.md" with { type: "text" };
import { createScopedTools } from "./scoped-tools";

const WORK_ROOT = "/work";
const PROVIDER = "omp-proxy";
const GRANTED_APIS = ["google-generative-ai", "ollama-chat"] as const;
/**
 * `setTimeout` and `AbortSignal.timeout` store the delay in a signed 32-bit
 * integer. A later lease (the usual case) overflows and fires immediately,
 * which would abort a still-valid turn. Past that span the pre-model gate
 * compares the real lease; a single turn cannot outlive the timer limit.
 */
const MAX_ABORT_TIMER_MS = 2_147_483_647;

type GrantedApi = (typeof GRANTED_APIS)[number];

interface WorkerLimits {
	max_output_tokens: number;
	context_window: number;
}

interface WorkerManifest {
	version: 1;
	instruction: string;
	approach: string[];
	allowed_paths: string[];
	candidate_id: string;
	lease_expires_at: string;
	model: {
		api: GrantedApi;
		id: string;
		limits: WorkerLimits;
		proxy: { baseUrl: string };
	};
}

interface LoadedManifest {
	manifest: WorkerManifest;
	deadline: number;
	proxyUrl: string;
	token: string;
}

const GOOGLE_COMPAT = {
	supportsFunctionPartId: false,
	requiresSkipThoughtSignature: false,
	requiresSkipThoughtSignatureOnFirstFunctionCall: false,
	dropUnsignedThinking: false,
	ccaLegacyParametersSchema: false,
	multimodalFunctionResponse: false,
	flashStreamLeakWorkaround: false,
	claudeThinkingBetaHeader: false,
	antigravityClaudeToolMode: false,
	stripImageInput: false,
} as const satisfies ResolvedGoogleCompat;

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): value is string {
	return typeof value === "string" && value.trim().length > 0;
}

function stringList(value: unknown): value is string[] {
	return Array.isArray(value) && value.every(item => typeof item === "string" && item.trim().length > 0);
}

function positiveInt(value: unknown): value is number {
	return typeof value === "number" && Number.isInteger(value) && value > 0;
}

/** Trailing-slash-insensitive proxy base. The provider appends its own path. */
function normalizeProxyBase(value: string): string {
	return value.trim().replace(/\/+$/, "");
}

function proxyBase(value: string): string | undefined {
	const normalized = normalizeProxyBase(value);
	if (!/^https?:\/\/[^/\s]+(?:\/[^\s]*)?$/.test(normalized)) return undefined;
	return normalized;
}

function invalid(message: string): { ok: false; message: string } {
	return { ok: false, message: `invalid manifest: ${message}` };
}

function loadManifest(): { ok: true; loaded: LoadedManifest } | { ok: false; message: string } {
	const manifestPath = process.env.OMP_WORKER_MANIFEST;
	if (!nonEmptyString(manifestPath)) return invalid("OMP_WORKER_MANIFEST is not set");
	let text: string;
	try {
		text = fs.readFileSync(manifestPath, "utf8");
	} catch {
		return invalid("OMP_WORKER_MANIFEST is not readable");
	}
	let parsed: unknown;
	try {
		parsed = JSON.parse(text) as unknown;
	} catch {
		return invalid("OMP_WORKER_MANIFEST is not JSON");
	}
	if (!isRecord(parsed)) return invalid("manifest must be an object");
	if (parsed.version !== 1) return invalid("version must be 1");
	const instruction = parsed.instruction;
	const approach = parsed.approach;
	const allowedPaths = parsed.allowed_paths;
	const candidateId = parsed.candidate_id;
	const leaseExpiresAt = parsed.lease_expires_at;
	if (!nonEmptyString(instruction)) return invalid("instruction must be a non-empty string");
	if (!Array.isArray(approach) || !approach.every((item): item is string => typeof item === "string")) {
		return invalid("approach must be an array of strings");
	}
	if (!stringList(allowedPaths)) return invalid("allowed_paths must be a non-empty string array");
	if (!nonEmptyString(candidateId)) return invalid("candidate_id must be a non-empty string");
	if (!nonEmptyString(leaseExpiresAt)) return invalid("lease_expires_at must be a timestamp");
	const deadline = Date.parse(leaseExpiresAt);
	if (Number.isNaN(deadline)) return invalid("lease_expires_at is not a timestamp");
	if (!isRecord(parsed.model)) return invalid("model must be an object");
	const model = parsed.model;
	const api = model.api;
	if (api !== "google-generative-ai" && api !== "ollama-chat") return invalid("model api refused");
	if (!nonEmptyString(model.id)) return invalid("model id must be a non-empty string");
	if (!isRecord(model.limits)) return invalid("model limits must be an object");
	const limits = model.limits;
	if (!positiveInt(limits.max_output_tokens)) return invalid("max_output_tokens must be a positive integer");
	if (!positiveInt(limits.context_window)) return invalid("context_window must be a positive integer");
	const proxy = model.proxy;
	if (!isRecord(proxy) || !nonEmptyString(proxy.baseUrl)) {
		return invalid("model proxy baseUrl must be a string");
	}
	const envProxy = process.env.OMP_MODEL_PROXY_URL;
	const token = process.env.OMP_MODEL_TOKEN;
	if (!nonEmptyString(envProxy)) return invalid("OMP_MODEL_PROXY_URL is not set");
	if (!nonEmptyString(token)) return invalid("OMP_MODEL_TOKEN is not set");
	const proxyUrl = proxyBase(envProxy);
	const manifestProxy = proxyBase(proxy.baseUrl);
	if (!proxyUrl || !manifestProxy || proxyUrl !== manifestProxy) {
		return invalid("proxy baseUrl does not match OMP_MODEL_PROXY_URL");
	}
	const manifest: WorkerManifest = {
		version: 1,
		instruction,
		approach,
		allowed_paths: allowedPaths,
		candidate_id: candidateId,
		lease_expires_at: leaseExpiresAt,
		model: {
			api,
			id: model.id,
			limits: {
				max_output_tokens: limits.max_output_tokens,
				context_window: limits.context_window,
			},
			proxy: { baseUrl: manifestProxy },
		},
	};
	return { ok: true, loaded: { manifest, deadline, proxyUrl, token } };
}

function grantedModel(manifest: WorkerManifest, baseUrl: string): Model<GrantedApi> {
	const shared = {
		id: manifest.model.id,
		name: manifest.model.id,
		provider: PROVIDER,
		baseUrl,
		reasoning: false,
		identity: { class: "unknown" },
		input: ["text"] as ("text" | "image")[],
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: manifest.model.limits.context_window,
		maxTokens: manifest.model.limits.max_output_tokens,
	};
	if (manifest.model.api === "google-generative-ai") {
		return { ...shared, api: "google-generative-ai", compat: GOOGLE_COMPAT };
	}
	return { ...shared, api: "ollama-chat", compat: undefined };
}

function requestUrl(input: string | URL | Request): string {
	if (typeof input === "string") return input;
	if (input instanceof URL) return input.toString();
	return input.url;
}

function endpointAllowed(url: string, pinnedBase: string): boolean {
	return url === pinnedBase || url.startsWith(`${pinnedBase}/`);
}

/** Agent deadline, or none when the lease is farther out than the abort timer can count. */
function boundedAgentDeadline(deadline: number): number | undefined {
	const remaining = deadline - Date.now();
	if (remaining <= 0 || remaining > MAX_ABORT_TIMER_MS) return undefined;
	return deadline;
}

function drain(stream: NodeJS.WriteStream): Promise<void> {
	if (!stream.writable || stream.writableEnded || !stream.writableNeedDrain) return Promise.resolve();
	return new Promise(resolve => {
		stream.once("drain", () => resolve());
	});
}

function writeLine(line: unknown): void {
	process.stdout.write(`${JSON.stringify(line)}\n`);
}

/**
 * Run the single granted turn. Returns 0 only when it completed with no HTTP,
 * model, or tool refusal and the lease was still in the future. A later model
 * call cannot clear a refusal: the flag stays set and the pre-model gate stops
 * the loop.
 */
export async function runOrchestratorWorker(): Promise<number> {
	const loaded = loadManifest();
	if (!loaded.ok) {
		process.stderr.write(`orchestrator-worker: ${loaded.message}\n`);
		return 1;
	}
	const { manifest, deadline, proxyUrl, token } = loaded.loaded;
	if (Date.now() > deadline) {
		process.stderr.write("orchestrator-worker: deadline exceeded\n");
		return 1;
	}

	let failure: string | undefined;
	const fail = (reason: string): void => {
		if (failure === undefined) failure = reason;
	};
	let agent: Agent | undefined;
	let unsubscribe = (): void => {};
	let lastStop: string | undefined;

	try {
		const scoped = createScopedTools({
			root: WORK_ROOT,
			allowedPaths: manifest.allowed_paths,
			abort: reason => {
				fail(reason);
				agent?.abort();
			},
		});
		const maxOutputTokens = manifest.model.limits.max_output_tokens;
		const fetchOverride: FetchImpl = async (input, init) => {
			// A non-success aborts the run for good. Provider retry loops call this
			// again; refuse before opening another socket.
			if (failure !== undefined) {
				agent?.abort();
				throw new Error(`orchestrator-worker: ${failure}`);
			}
			const url = requestUrl(input);
			if (!endpointAllowed(url, proxyUrl)) {
				fail("endpoint refused");
				agent?.abort();
				throw new Error("orchestrator-worker: endpoint refused");
			}
			const response = await globalThis.fetch(input, init);
			if (!response.ok) {
				// Abort before the provider's retry loop observes the response.
				// fetchWithRetry retries 5xx unless the caller's signal is already
				// aborted; throwing after abort makes that check fail the attempt.
				fail(`proxy refusal HTTP ${response.status}`);
				await response.body?.cancel().catch(() => {});
				agent?.abort();
				throw new Error(`orchestrator-worker: proxy refusal HTTP ${response.status}`);
			}
			return response;
		};
		const streamFn: StreamFn = (model, context, options) =>
			streamSimple(model, context, {
				...options,
				apiKey: token,
				maxTokens: maxOutputTokens,
				fetch: fetchOverride,
				fallbacks: undefined,
			});
		agent = new Agent({
			initialState: {
				model: grantedModel(manifest, proxyUrl),
				systemPrompt: [
					prompt.render(workerPrompt, {
						candidateId: manifest.candidate_id,
						approach: manifest.approach,
						allowedPaths: manifest.allowed_paths,
					}),
				],
				tools: scoped.tools,
				disableReasoning: true,
			},
			streamFn,
			deadline: boundedAgentDeadline(deadline),
			getApiKey: () => token,
		});
		unsubscribe = agent.subscribe((event: AgentEvent) => {
			if (event.type === "tool_execution_start") {
				writeLine({ type: event.type, toolName: event.toolName, args: event.args });
				return;
			}
			if (event.type === "tool_execution_end") {
				if (event.isError) fail("tool error");
				writeLine({ type: event.type, toolName: event.toolName, isError: event.isError === true });
				return;
			}
			if (event.type === "message_end") {
				const message = event.message;
				if (message.role === "assistant") {
					lastStop = message.stopReason;
					if (
						message.stopReason === "error" ||
						message.stopReason === "aborted" ||
						message.stopReason === "length"
					) {
						fail(`model ${message.stopReason}`);
					}
					writeLine({ type: event.type, role: message.role, stopReason: message.stopReason });
					return;
				}
				if (message.role === "toolResult" && message.isError) fail("tool error");
				writeLine({
					type: event.type,
					role: message.role,
					isError: message.role === "toolResult" ? message.isError === true : undefined,
				});
				return;
			}
			if (event.type === "agent_end") writeLine({ type: "agent_end" });
		});
		agent.setBeforeModelCall(() => {
			if (Date.now() > deadline) {
				fail("deadline exceeded");
				return { stop: true, reason: "deadline exceeded" };
			}
			if (failure !== undefined || scoped.state.failed) {
				return { stop: true, reason: failure ?? scoped.state.reason ?? "refused" };
			}
			return undefined;
		});
		await agent.prompt(manifest.instruction);
		if (scoped.state.failed) fail(scoped.state.reason ?? "tool refusal");
		if (Date.now() > deadline) fail("deadline exceeded");
		const completed = failure === undefined && lastStop === "stop";
		if (!completed) {
			process.stderr.write(`orchestrator-worker: ${failure ?? "turn did not complete"}\n`);
		}
		writeLine({
			type: "orchestrator-worker",
			result: completed ? "completed" : "failed",
			candidate_id: manifest.candidate_id,
		});
		return completed ? 0 : 1;
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		fail(message);
		process.stderr.write(`orchestrator-worker: ${message}\n`);
		return 1;
	} finally {
		unsubscribe();
		agent?.abort();
		agent = undefined;
		await drain(process.stdout);
		await drain(process.stderr);
	}
}
