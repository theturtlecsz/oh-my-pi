/**
 * workflow/intake-runner.ts — Native NSI-6 drafting runner (OMP-630).
 *
 * Each case runs the shipped intake skill's existing-plan seam in its own
 * coding-agent SDK session (real model registry and auth, no tools, in-memory
 * transcript) and turns the case's source text into a BoundedIntakeDraft plus
 * unresolved findings. One finite run cap is enforced before every provider
 * request by wrapping the session's stream function; usage is settled from
 * the catalog prices through the shared cost helper.
 *
 * CLI: JSON request on stdin, JSON report on stdout.
 */
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { StreamFn } from "@oh-my-pi/pi-agent-core";
import {
	type AssistantMessage,
	type Context,
	type Model,
	type SimpleStreamOptions,
	type Usage,
} from "@oh-my-pi/pi-ai";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { calculateUsageCost } from "@oh-my-pi/pi-catalog/models";
import {
	createAgentSession,
	discoverAuthStorage,
	ModelRegistry,
	SessionManager,
	Settings,
} from "@oh-my-pi/pi-coding-agent";
import { type ResolveCliModelResult, resolveCliModel } from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import { getAgentDir, prompt } from "@oh-my-pi/pi-utils";
import { type BoundedIntakeDraft, sha256Hex } from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import skillText from "../../skills/intake/SKILL.md" with { type: "text" };
import draftInstructions from "./nsi6-draft.md" with { type: "text" };
import repairTemplate from "./nsi6-repair.md" with { type: "text" };
import sourceTemplate from "./nsi6-source.md" with { type: "text" };
import typedCompleteness from "../../skills/intake/references/typed-completeness.md" with { type: "text" };

export type IntakeArm = "current" | "typed";

/** The evaluator's label-free input: raw bounded input and proof recipe only. */
export interface IntakeEvaluatorInput {
	bounded: { draft: { source: { text: string } } & Record<string, unknown> } & Record<string, unknown>;
	nsi2_proof_scenario?: unknown;
}

export interface IntakeRunCase {
	id: string;
	arm: IntakeArm;
	input: IntakeEvaluatorInput;
}

export interface IntakeRunRequest {
	model: string;
	max_usd: number;
	cases: IntakeRunCase[];
}

export type IntakeCaseStatus = "ok" | "unmeasured" | "invalid" | "failed" | "cap_denied";

export interface IntakeFinding {
	kind: "contradiction" | "missing_oracle" | "unresolved_authority" | "ambiguity" | "other";
	statement: string;
	claim_ids: string[];
}

export interface IntakeTokenUsage {
	input: number;
	output: number;
	cache_read: number;
	cache_write: number;
	total: number;
}

export interface IntakeCaseResult {
	id: string;
	arm: IntakeArm;
	status: IntakeCaseStatus;
	completions: string[];
	draft: BoundedIntakeDraft | null;
	findings: IntakeFinding[];
	diagnostics: string[];
	model: { requested: string; resolved: string; effort: string | null };
	hashes: {
		skill_sha256: string;
		draft_instructions_sha256: string;
		typed_completeness_sha256: string | null;
		system_prompt_sha256: string;
		user_prompt_sha256: string;
	};
	usage: {
		requests: number;
		denied_requests: number;
		measured: boolean;
		tokens: IntakeTokenUsage;
		usd: number;
	};
}

export interface IntakeRunReport {
	model: string;
	cap_usd: number;
	spent_usd: number;
	requests: number;
	denied_requests: number;
	cases: IntakeCaseResult[];
}

export interface IntakeRunnerOptions {
	/** Registry used to resolve the model and credentials. Default: the installed registry for `agentDir`. */
	modelRegistry?: ModelRegistry;
	agentDir?: string;
	/** Replaces the provider transport beneath the cap gate (tests inject replies here). */
	transport?: StreamFn;
}

const MAX_REPAIRS = 1;
const CASE_INPUT_KEYS = new Set(["bounded", "nsi2_proof_scenario"]);
const ARMS = new Set<string>(["current", "typed"]);

const claimBase = { id: z.string().min(1), statement: z.string().min(1), source_span_ids: z.array(z.string()).default([]) };
const replySchema = z
	.object({
		draft: z
			.object({
				archetype: z.literal("small_code_change").default("small_code_change"),
				spans: z.array(z.object({ id: z.string().min(1), quote: z.string().min(1) }).strict()),
				goal: z.object(claimBase).strict(),
				constraints: z
					.array(
						z
							.object({
								...claimBase,
								key: z.string().min(1),
								value: z.union([
									z.object({ kind: z.literal("known"), value: z.union([z.string(), z.number(), z.boolean()]) }).strict(),
									z.object({ kind: z.literal("unknown") }).strict(),
								]),
								polarity: z.enum(["positive", "negative"]),
							})
							.strict(),
					)
					.default([]),
				unknowns: z
					.array(
						z
							.object({
								...claimBase,
								kind: z.enum(["authority_or_dependency", "routine_choice"]),
								material: z.boolean(),
							})
							.strict(),
					)
					.default([]),
				acceptance_criteria: z
					.array(
						z
							.object({
								...claimBase,
								observable_outcome: z.string().min(1),
								oracle: z
									.enum(["automated_test", "static_check", "manual_inspection", "external_receipt"])
									.nullable()
									.default(null),
							})
							.strict(),
					)
					.default([]),
			})
			.strict(),
		findings: z
			.array(
				z
					.object({
						kind: z.enum(["contradiction", "missing_oracle", "unresolved_authority", "ambiguity", "other"]),
						statement: z.string().min(1),
						claim_ids: z.array(z.string()).default([]),
					})
					.strict(),
			)
			.default([]),
	})
	.strict();

type Reply = z.infer<typeof replySchema>;

/** Throws on a request that is malformed or carries anything beyond evaluator_input fields. */
export function parseIntakeRunRequest(value: unknown): IntakeRunRequest {
	if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("request must be an object");
	const request = value as Record<string, unknown>;
	for (const key of Object.keys(request)) {
		if (!["model", "max_usd", "cases"].includes(key)) throw new Error(`unknown request field: ${key}`);
	}
	if (typeof request.model !== "string" || request.model.trim() === "") throw new Error("model must be a non-empty string");
	const cap = request.max_usd;
	if (typeof cap !== "number" || !Number.isFinite(cap) || cap <= 0) throw new Error("max_usd must be a finite positive number");
	if (!Array.isArray(request.cases) || request.cases.length === 0) throw new Error("cases must be a non-empty array");
	const seen = new Set<string>();
	const cases = request.cases.map((raw: unknown, index): IntakeRunCase => {
		const where = `cases[${index}]`;
		if (typeof raw !== "object" || raw === null || Array.isArray(raw)) throw new Error(`${where} must be an object`);
		const row = raw as Record<string, unknown>;
		for (const key of Object.keys(row)) {
			if (!["id", "arm", "input"].includes(key)) throw new Error(`${where}: unknown field ${key} (labels are never accepted)`);
		}
		if (typeof row.id !== "string" || row.id === "") throw new Error(`${where}.id must be a non-empty string`);
		if (seen.has(row.id)) throw new Error(`duplicate case id: ${row.id}`);
		seen.add(row.id);
		if (typeof row.arm !== "string" || !ARMS.has(row.arm)) throw new Error(`${where}.arm must be current or typed`);
		const input = row.input as Record<string, unknown> | null;
		if (typeof input !== "object" || input === null || Array.isArray(input)) throw new Error(`${where}.input must be an object`);
		for (const key of Object.keys(input)) {
			if (!CASE_INPUT_KEYS.has(key)) throw new Error(`${where}.input: ${key} is not an evaluator_input field`);
		}
		const text = ((input.bounded as { draft?: { source?: { text?: unknown } } } | undefined)?.draft?.source ?? {}).text;
		if (typeof text !== "string" || text === "") throw new Error(`${where}.input.bounded.draft.source.text must be a non-empty string`);
		return { id: row.id, arm: row.arm as IntakeArm, input: input as unknown as IntakeEvaluatorInput };
	});
	return { model: request.model, max_usd: cap, cases };
}

/** The run-wide cap: reservations are taken before transport and settled with measured spend. */
class RunCap {
	readonly cap: number;
	spent = 0;
	#reserved = 0;
	requests = 0;
	denied = 0;

	constructor(cap: number) {
		this.cap = cap;
	}

	reserve(bound: number): boolean {
		if (!Number.isFinite(bound) || this.spent + this.#reserved + bound > this.cap) {
			this.denied++;
			return false;
		}
		this.#reserved += bound;
		this.requests++;
		return true;
	}

	settle(reserved: number, actual: number): void {
		this.#reserved -= reserved;
		this.spent += actual;
	}
}

class CaseAccount {
	requests = 0;
	denied = 0;
	measured = true;
	usd = 0;
	tokens: IntakeTokenUsage = { input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0 };
	readonly pending: Promise<void>[] = [];
	transportFailure: string | undefined;
}

function zeroUsage(): Usage {
	return {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 0,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
}

function isPriced(model: Model): boolean {
	const cost = model.cost;
	if (!cost) return false;
	const rates = [cost.input, cost.output, cost.cacheRead, cost.cacheWrite];
	return rates.every(rate => Number.isFinite(rate) && rate >= 0) && (cost.input > 0 || cost.output > 0);
}

/** Highest cost one request can incur: whole context as the dearest input class plus the full output limit. */
function reservationBound(model: Model, context: Context, options: SimpleStreamOptions | undefined): number | undefined {
	const { contextWindow, maxTokens } = model;
	if (!isPriced(model) || !contextWindow || contextWindow <= 0 || !maxTokens || maxTokens <= 0) return undefined;
	// A token is at least one UTF-8 byte of its prompt text in practice, so bytes bound tokens from above.
	const promptTokens = Math.min(Buffer.byteLength(JSON.stringify(context)), contextWindow);
	const outputTokens = Math.min(options?.maxTokens ?? maxTokens, maxTokens);
	let bound = 0;
	for (const klass of ["input", "cacheRead", "cacheWrite"] as const) {
		const usage = zeroUsage();
		usage[klass] = promptTokens;
		usage.output = outputTokens;
		bound = Math.max(bound, calculateUsageCost(model.cost, usage).total);
	}
	return bound;
}

function failedStream(model: Model, message: string): AssistantMessageEventStream {
	const stream = new AssistantMessageEventStream();
	const error: AssistantMessage = {
		role: "assistant",
		content: [],
		api: model.api,
		provider: model.provider,
		model: model.id,
		usage: zeroUsage(),
		stopReason: "error",
		errorMessage: message,
		timestamp: Date.now(),
	};
	queueMicrotask(() => stream.push({ type: "error", reason: "error", error }));
	return stream;
}

/** Measured usage of a settled message, or undefined when the provider reported none. */
function measureUsage(model: Model, message: AssistantMessage): { tokens: IntakeTokenUsage; usd: number } | undefined {
	const usage = message.usage;
	if (!usage) return undefined;
	const counts = [usage.input, usage.output, usage.cacheRead, usage.cacheWrite];
	if (!counts.every(count => Number.isFinite(count) && count >= 0)) return undefined;
	const total = counts.reduce((sum, count) => sum + count, 0);
	if (total === 0) return undefined;
	const priced = calculateUsageCost(model.cost, { ...usage, cost: { ...zeroUsage().cost } }).total;
	const reported = Number.isFinite(usage.cost?.total) ? usage.cost.total : 0;
	return {
		tokens: {
			input: usage.input,
			output: usage.output,
			cache_read: usage.cacheRead,
			cache_write: usage.cacheWrite,
			total,
		},
		usd: Math.max(priced, reported),
	};
}

function meter(base: StreamFn, run: RunCap, account: CaseAccount): StreamFn {
	return (model, context, options) => {
		const bound = reservationBound(model, context, options);
		if (bound === undefined) {
			account.denied++;
			run.denied++;
			account.measured = false;
			return failedStream(model, `cap_denied: ${model.provider}/${model.id} has no usable catalog price or limits`);
		}
		if (!run.reserve(bound)) {
			account.denied++;
			return failedStream(model, `cap_denied: request bound $${bound.toFixed(6)} exceeds the remaining run cap`);
		}
		account.requests++;
		const settleUnknown = (reason: string) => {
			// No usage to measure: charge the full bound and keep the case out of measured evidence.
			run.settle(bound, bound);
			account.usd += bound;
			account.measured = false;
			account.transportFailure = reason;
		};
		let started: AssistantMessageEventStream | Promise<AssistantMessageEventStream>;
		try {
			started = base(model, context, options);
		} catch (error) {
			settleUnknown(error instanceof Error ? error.message : String(error));
			return failedStream(model, `transport_failed: ${error instanceof Error ? error.message : String(error)}`);
		}
		account.pending.push(
			Promise.resolve(started)
				.then(inner => inner.result())
				.then(
					message => {
						const measured = measureUsage(model, message);
						if (!measured) {
							settleUnknown(message.errorMessage ?? "provider reported no usage");
							return;
						}
						run.settle(bound, measured.usd);
						account.usd += measured.usd;
						for (const key of Object.keys(account.tokens) as (keyof IntakeTokenUsage)[]) {
							account.tokens[key] += measured.tokens[key];
						}
						if (message.stopReason === "error" || message.stopReason === "aborted") {
							account.transportFailure = message.errorMessage ?? message.stopReason;
						}
					},
					error => settleUnknown(error instanceof Error ? error.message : String(error)),
				),
		);
		return started;
	};
}

function systemPromptFor(arm: IntakeArm): string {
	const parts = [skillText.trim(), draftInstructions.trim()];
	if (arm === "typed") parts.push(typedCompleteness.trim());
	return parts.join("\n\n---\n\n");
}

function completionText(message: AssistantMessage | undefined): string {
	if (!message) return "";
	return message.content
		.flatMap(block => (block.type === "text" ? [block.text] : []))
		.join("")
		.trim();
}

function stripFence(text: string): string {
	const match = /^```(?:json)?\s*\n([\s\S]*?)\n```$/.exec(text.trim());
	return match ? match[1]! : text;
}

/** Parse one reply and derive source offsets/hashes from the runner-held source text. */
function buildDraft(
	raw: string,
	sourceText: string,
): { ok: true; draft: BoundedIntakeDraft; findings: IntakeFinding[] } | { ok: false; reason: string } {
	let json: unknown;
	try {
		json = JSON.parse(stripFence(raw));
	} catch (error) {
		return { ok: false, reason: `reply is not valid JSON: ${error instanceof Error ? error.message : String(error)}` };
	}
	const parsed = replySchema.safeParse(json);
	if (!parsed.success) {
		return { ok: false, reason: parsed.error.issues.map(issue => `${issue.path.join(".")}: ${issue.message}`).join("; ") };
	}
	const reply: Reply = parsed.data;
	const bytes = Buffer.from(sourceText, "utf8");
	const spans = [];
	for (const span of reply.draft.spans) {
		const start = bytes.indexOf(Buffer.from(span.quote, "utf8"));
		if (start < 0) return { ok: false, reason: `span ${span.id}: quote is not an exact substring of the source text` };
		const end = start + Buffer.byteLength(span.quote, "utf8");
		spans.push({ id: span.id, start, end, exact_text_sha256: sha256Hex(span.quote) });
	}
	const { goal, constraints, unknowns, acceptance_criteria } = reply.draft;
	const claims = [goal, ...constraints, ...unknowns, ...acceptance_criteria];
	const claimIds = new Set<string>();
	const spanIds = new Set(spans.map(span => span.id));
	if (spanIds.size !== spans.length) return { ok: false, reason: "duplicate span id" };
	for (const claim of claims) {
		if (claimIds.has(claim.id)) return { ok: false, reason: `duplicate claim id: ${claim.id}` };
		claimIds.add(claim.id);
		for (const ref of claim.source_span_ids) {
			if (!spanIds.has(ref)) return { ok: false, reason: `claim ${claim.id}: unknown span ref ${ref}` };
		}
	}
	for (const finding of reply.findings) {
		for (const id of finding.claim_ids) {
			if (!claimIds.has(id)) return { ok: false, reason: `finding names unknown claim id: ${id}` };
		}
	}
	const draft: BoundedIntakeDraft = {
		archetype: reply.draft.archetype,
		source: { text: sourceText, sha256: sha256Hex(sourceText), spans },
		goal,
		constraints,
		unknowns,
		acceptance_criteria,
	};
	return { ok: true, draft, findings: reply.findings };
}

/** Runs every case once, in order, under one shared cap. Per-case failures never abort the run. */
export async function runIntake(request: IntakeRunRequest, options: IntakeRunnerOptions = {}): Promise<IntakeRunReport> {
	const checked = parseIntakeRunRequest(request);
	const agentDir = options.agentDir ?? getAgentDir();
	const registrySettings = Settings.isolated();
	const modelRegistry =
		options.modelRegistry ??
		new ModelRegistry(await discoverAuthStorage(agentDir, { settings: registrySettings }), path.join(agentDir, "models.yml"), {
			settings: registrySettings,
		});
	const resolved = resolveCliModel({ cliModel: checked.model, modelRegistry, settings: registrySettings });
	if (resolved.error || !resolved.model) throw new Error(resolved.error ?? `model not found: ${checked.model}`);
	const model = resolved.model;

	const run = new RunCap(checked.max_usd);
	const scratch = await fs.mkdtemp(path.join(os.tmpdir(), "omp-intake-runner-"));
	try {
		const cases: IntakeCaseResult[] = [];
		for (const row of checked.cases) {
			cases.push(await runCase(row, checked.model, model, modelRegistry, resolved.thinkingLevel, run, scratch, agentDir, options));
		}
		return {
			model: `${model.provider}/${model.id}`,
			cap_usd: run.cap,
			spent_usd: run.spent,
			requests: run.requests,
			denied_requests: run.denied,
			cases,
		};
	} finally {
		await fs.rm(scratch, { recursive: true, force: true });
	}
}

async function runCase(
	row: IntakeRunCase,
	requestedModel: string,
	model: Model,
	modelRegistry: ModelRegistry,
	thinkingLevel: ResolveCliModelResult["thinkingLevel"],
	run: RunCap,
	scratch: string,
	agentDir: string,
	options: IntakeRunnerOptions,
): Promise<IntakeCaseResult> {
	const sourceText = row.input.bounded.draft.source.text;
	const system = systemPromptFor(row.arm);
	const userPrompt = prompt.render(sourceTemplate, { source: sourceText });
	const account = new CaseAccount();
	const completions: string[] = [];
	const diagnostics: string[] = [];
	let draft: BoundedIntakeDraft | null = null;
	let findings: IntakeFinding[] = [];
	let effort: string | null = null;

	// A fresh settings instance, session, transcript and working directory per case: nothing carries across arms.
	const settings = Settings.isolated({
		"compaction.enabled": false,
		"retry.enabled": false,
		"memory.backend": "off",
		"providers.cacheWarming": "off",
		"advisor.enabled": false,
	});
	const cwd = await fs.mkdtemp(path.join(scratch, "case-"));
	const { session } = await createAgentSession({
		cwd,
		agentDir,
		modelRegistry,
		authStorage: modelRegistry.authStorage,
		settings,
		model,
		thinkingLevel,
		sessionManager: SessionManager.inMemory(),
		systemPrompt: system,
		toolNames: [],
		restrictToolNames: true,
		disableExtensionDiscovery: true,
		skills: [],
		rules: [],
		contextFiles: [],
		promptTemplates: [],
		slashCommands: [],
		customTools: [],
		enableMCP: false,
		enableLsp: false,
		skipPythonPreflight: true,
		cacheWarming: false,
		bindProcessState: false,
		preloadedCustomToolPaths: [],
	});
	try {
		session.agent.streamFn = meter(options.transport ?? session.agent.streamFn, run, account);
		effort = session.thinkingLevel ?? null;
		let message = userPrompt;
		for (let attempt = 0; attempt <= MAX_REPAIRS; attempt++) {
			const before = session.agent.state.messages.length;
			await session.prompt(message);
			await Promise.all(account.pending.splice(0));
			const reply = session.agent.state.messages
				.slice(before)
				.reverse()
				.find((entry): entry is AssistantMessage => entry.role === "assistant");
			const text = completionText(reply);
			if (reply?.stopReason === "error" || reply?.stopReason === "aborted" || text === "") {
				diagnostics.push(reply?.errorMessage ?? "provider returned no completion");
				if (text !== "") completions.push(text);
				break;
			}
			completions.push(text);
			const built = buildDraft(text, sourceText);
			if (built.ok) {
				draft = built.draft;
				findings = built.findings;
				break;
			}
			diagnostics.push(built.reason);
			message = prompt.render(repairTemplate, { reason: built.reason });
		}
	} finally {
		await Promise.all(account.pending.splice(0));
		await session.dispose();
		await fs.rm(cwd, { recursive: true, force: true });
	}

	let status: IntakeCaseStatus;
	if (draft) status = account.measured ? "ok" : "unmeasured";
	else if (account.denied > 0) status = "cap_denied";
	else if (completions.length === 0 || account.transportFailure !== undefined) status = "failed";
	else status = "invalid";

	return {
		id: row.id,
		arm: row.arm,
		status,
		completions,
		draft,
		findings,
		diagnostics,
		model: { requested: requestedModel, resolved: `${model.provider}/${model.id}`, effort },
		hashes: {
			skill_sha256: sha256Hex(skillText),
			draft_instructions_sha256: sha256Hex(draftInstructions),
			typed_completeness_sha256: row.arm === "typed" ? sha256Hex(typedCompleteness) : null,
			system_prompt_sha256: sha256Hex(system),
			user_prompt_sha256: sha256Hex(userPrompt),
		},
		usage: {
			requests: account.requests,
			denied_requests: account.denied,
			measured: account.measured && account.requests > 0,
			tokens: account.tokens,
			usd: account.usd,
		},
	};
}

if (import.meta.main) {
	try {
		const request = parseIntakeRunRequest(JSON.parse(await Bun.stdin.text()));
		process.stdout.write(`${JSON.stringify(await runIntake(request))}\n`);
	} catch (error) {
		process.stdout.write(`${JSON.stringify({ error: error instanceof Error ? error.message : String(error) })}\n`);
		process.exitCode = 2;
	}
}
