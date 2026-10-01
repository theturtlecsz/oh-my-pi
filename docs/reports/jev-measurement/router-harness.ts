/**
 * Router-mode measurement: the three OMP-298 decisions measured through
 * OpenRouter's Jev Router (`typesafe/jev-router`).
 *
 * What this replaces: the OMP-298 harness asks TypeSafe's typed decision API
 * (`POST /v1/systemone {model, state, questions}`) for probability maps over
 * Choice/Noul questions. That endpoint rejects the owner's OpenRouter key and no
 * TypeSafe key is being sought, so the router — a chat-completions endpoint — is
 * the only Jev surface available. Each decision is therefore reduced to one
 * single-label classification whose output is the router's own text: the
 * router's *choice* is measurable; its *calibration* (probability maps, the 0.70
 * threshold, off-list/off-options buckets) is not.
 *
 * What stays comparable with the OMP-298 report: per-decision accuracy against
 * the dataset label, p50/p95 latency, and cost per 1000 calls from actual
 * usage. The current-side (smol) numbers are produced the same way OMP-298
 * produced them, so accuracy and cost stay comparable column-for-column.
 *
 * Safeguards (unchanged from OMP-358): the caller passes the transport, and in
 * the owner slice that transport is `makeOpenRouterFetch()` — origin-locked to
 * `https://openrouter.ai`. The key comes from `readJevEnvKey()` at run time and
 * is never logged. Tests inject the fake transport and never read the env file.
 */

import type { Model } from "@oh-my-pi/pi-ai";
import { classifyDifficulty } from "@oh-my-pi/pi-coding-agent/auto-thinking/classifier";
import { classifyUnexpectedStop } from "@oh-my-pi/pi-coding-agent/session/unexpected-stop-classifier";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import type { FetchImpl } from "@oh-my-pi/pi-utils";
import type { CurrentSmolHarness } from "./harness";
import routerEffortPrompt from "./prompts/router-effort.md" with { type: "text" };
import routerStopPrompt from "./prompts/router-stop.md" with { type: "text" };
import {
	OPENROUTER_JEV_ROUTER_MODEL,
	parseFirstLabel,
	parseYesNo,
	routeCompletion,
	type RouterUsageEntry,
} from "./router-transport";

export { OPENROUTER_JEV_ROUTER_MODEL };

/**
 * The router measurement is a measurement, not an adoption of a per-request
 * generative router, so WP5's router rule is not triggered. Emitted as the
 * report's single verdict line.
 */
export const WP5_ROUTER_DOES_NOT_APPLY =
	"WP5 does not apply: the router is measured, not adopted — no per-request generative router is introduced.";

/** Router transport failures above this share abort a route. */
export const ROUTER_TRANSPORT_FAILURE_LIMIT = 0.05;

export class RouterRouteError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "RouterRouteError";
	}
}

export const ROUTER_EFFORT_LABELS = ["low", "medium", "high", "xhigh"] as const;

/**
 * Dataset effort labels outside {@link ROUTER_EFFORT_LABELS} that map onto a
 * scored label. The session-derived sets record the resolved thinking level,
 * which on a model exposing the `max` tier can be `max`; the router prompt only
 * ever offers low|medium|high|xhigh. Scoring `max` verbatim could never succeed
 * on either side, so it is aliased to the router's top rung instead of leaving
 * an unwinnable item in the denominator. `minimal` is likewise below the
 * router's floor.
 */
const EFFORT_LABEL_ALIASES: Readonly<Record<string, string>> = {
	max: "xhigh",
	minimal: "low",
};

/**
 * Normalize a dataset effort label to a scored one. Returns the label when it
 * is already in {@link ROUTER_EFFORT_LABELS}, an alias target when it is a
 * known out-of-vocabulary tier, and `undefined` when the label cannot be scored
 * — the caller then excludes and reports the item instead of scoring it.
 */
export function scoreableEffortLabel(raw: string | undefined): string | undefined {
	const label = (raw ?? "").trim().toLowerCase();
	if ((ROUTER_EFFORT_LABELS as readonly string[]).includes(label)) return label;
	return EFFORT_LABEL_ALIASES[label];
}

/** The excluded-label key a report shows; empty labels get a stable bucket. */
function excludedLabelKey(raw: string | undefined): string {
	return (raw ?? "").trim().toLowerCase() || "(empty)";
}

/** Robomp primary labels, as a router prompt. The typed API's question set is not reproducible. */
export const ROUTER_ROBOMP_PRIMARY_LABELS = [
	"bug",
	"enhancement",
	"question",
	"proposal",
	"documentation",
	"wontfix",
	"invalid",
	"duplicate",
] as const;

/** Labels whose typed prefilter would answer without opening a session. */
export const ROUTER_ROBOMP_SKIP_LABELS = ["invalid", "question"] as const;

const EFFORT_PROMPT = `${routerEffortPrompt.trim()}\n\nCoding request:\n`;
const STOP_PROMPT = `${routerStopPrompt.trim()}\n\nAssistant message:\n`;
const ROBOMP_PROMPT = `What is the primary classification of this issue? Reply with exactly one lowercase word from: ${ROUTER_ROBOMP_PRIMARY_LABELS.join(", ")}.\n\nIssue:\n`;

export interface RouterPromptItem {
	prompt: string;
	effort: string;
	label?: string;
	text?: string;
}

export interface RouterTurnEndItem {
	text: string;
	label: "continue" | "stop";
}

export interface RouterIssueItem {
	key: string;
	repo: string;
	number: number;
	title: string;
	body: string;
	label: string;
}

export interface RouterRouteMetrics {
	sampleSize: number;
	answerRate: number | null;
	accuracy: number | null;
	precision?: number | null;
	recall?: number | null;
	p50LatencyMs: number | null;
	p95LatencyMs: number | null;
	/**
	 * Cost of 1000 calls in USD: the mean of the calls that reported a cost,
	 * times 1000. An unread call is left out of that mean, not counted as zero.
	 * `null` when no call yielded a cost — the report renders it as not measured
	 * rather than a fabricated zero. A current side that ran reports `0` when
	 * the provider charged nothing; `null` there means that side did not run.
	 */
	costPer1000Usd: number | null;
	/**
	 * Where costPer1000Usd came from. Router rows read the completion's own
	 * `usage.cost` (`completion-usage`) or the OpenRouter generation record's
	 * `total_cost` (`generation-record`); the current side reads the
	 * provider-reported completion cost (`provider-usage`). `unavailable` means
	 * the row ran and read no cost: a router row then has costPer1000Usd null,
	 * and a current side that ran and was charged nothing has 0.
	 */
	costSource: "completion-usage" | "generation-record" | "provider-usage" | "unavailable";
	/** Routed answers that carried no allowed label. There is no separate off-list bucket in router mode. */
	unparseableRate: number | null;
	/**
	 * Routed calls that hit the completion-token cap (`finish_reason=length`)
	 * and returned no content. A truncated call is counted on its own, not folded
	 * into {@link unparseableRate}: it measured the harness's budget, not the
	 * router.
	 */
	truncatedRate: number | null;
	/**
	 * Dataset labels outside the scored vocabulary that were excluded from
	 * scoring, with how many items carried each. An excluded item is not in
	 * either side's denominator; the report shows the bucket so the exclusion is
	 * visible rather than silent. Empty for the unexpected-stop and robomp
	 * routes, whose label sets are fixed.
	 */
	excludedLabels: Record<string, number>;
	transportFailureRate: number | null;
	/** Histogram of routed model ids, from the response / generation record. */
	routedModels: Record<string, number>;
	/** Histogram of reasoning effort values the generation records reported. */
	routedEffort: Record<string, number>;
	/** Share of issues routed away from a full session (robomp only). */
	skipSessionShare?: number | null;
}

export interface RouterMeasurementResults {
	features: {
		auto_thinking_route: RouterRouteMetrics;
		unexpected_stop_route: RouterRouteMetrics;
		robomp_route: RouterRouteMetrics;
	};
	/** Current-side metrics over the same datasets, so accuracy stays comparable. */
	current: {
		auto_thinking: RouterRouteMetrics | null;
		unexpected_stop: RouterRouteMetrics | null;
	};
	verdict: string;
	routerModel: string;
	sampleSizes: { auto_thinking: number; unexpected_stop: number; robomp: number };
}

export interface RouterRouteDeps {
	apiKey: string;
	fetch: FetchImpl;
	generationRecords?: boolean;
	/** Bounded wait for a generation record that has not appeared yet. */
	generationRecordWaitMs?: number;
	/** First backoff delay for generation-record polling. */
	generationRecordRetryDelayMs?: number;
	/** Sleep seam for generation-record backoff; tests inject an instant one. */
	sleep?: (ms: number) => Promise<void>;
	/**
	 * How many generation-record reads may be in flight while later router
	 * calls continue. Defaults to {@link GENERATION_RECORD_CONCURRENCY}.
	 */
	generationRecordConcurrency?: number;
	/**
	 * Names the route in usage entries and abort errors. Callers that share the
	 * OpenRouter transport deps (where this field is optional) may omit it;
	 * those calls are recorded as `router_measurement`.
	 */
	feature?: string;
	recordUsage?: (entry: RouterUsageEntry) => void;
}

export interface RouterMeasurementOptions {
	prompts?: RouterPromptItem[];
	turnEnds?: RouterTurnEndItem[];
	issues?: RouterIssueItem[];
	/** Owner's OpenRouter key, read from the env file at run time by the caller. */
	apiKey: string;
	/** Origin-locked in live runs (`makeOpenRouterFetch`); the fake transport in tests. */
	fetch: FetchImpl;
	/** Read the OpenRouter generation record for each call (actual cost and routed reasoning). */
	generationRecords?: boolean;
	/** Bounded wait for a generation record that has not appeared yet. */
	generationRecordWaitMs?: number;
	/** First backoff delay for generation-record polling. */
	generationRecordRetryDelayMs?: number;
	/** Sleep seam for generation-record backoff; tests inject an instant one. */
	sleep?: (ms: number) => Promise<void>;
	/** How many generation-record reads may be in flight. Defaults to {@link GENERATION_RECORD_CONCURRENCY}. */
	generationRecordConcurrency?: number;
	/** Injected current-side settings/registry. Absent means the current side is not measured. */
	current?: CurrentSmolHarness;
	/** Test-only current-side handler, mirroring OMP-298's `--fake-smol`. */
	fakeCurrent?: RouterCurrentHandler;
	robompRunner?: (issues: RouterIssueItem[], deps: RouterRouteDeps) => Promise<Partial<RouterRouteMetrics> | null>;
}

/** Test-only current-side handler, mirroring OMP-298's `--fake-smol`. */
export interface RouterCurrentHandler {
	classifyDifficulty?: (prompt: string) => Promise<{ effort?: string; cost?: number; latencyMs?: number }>;
	classifyUnexpectedStop?: (text: string) => Promise<{ unexpectedStop?: boolean; cost?: number; latencyMs?: number }>;
}

function percentile(values: number[], p: number): number {
	if (values.length === 0) return 0;
	const sorted = [...values].sort((a, b) => a - b);
	const idx = Math.min(Math.max(Math.ceil(p * sorted.length) - 1, 0), sorted.length - 1);
	return sorted[idx];
}

interface RouterCall {
	entries: RouterUsageEntry[];
	values: (string | undefined)[];
	latencies: number[];
}

/** Generation-record reads in flight during a route. The rest wait their turn. */
export const GENERATION_RECORD_CONCURRENCY = 8;

function withConcurrencyLimit(limit: number): <T>(task: () => Promise<T>) => Promise<T> {
	let active = 0;
	const waiting: Array<() => void> = [];
	return function run<T>(task: () => Promise<T>): Promise<T> {
		return new Promise<T>((resolve, reject) => {
			const start = () => {
				active += 1;
				Promise.resolve()
					.then(task)
					.then(resolve, reject)
					.finally(() => {
						active -= 1;
						waiting.shift()?.();
					});
			};
			if (active < limit) start();
			else waiting.push(start);
		});
	};
}

/**
 * Run one routed single-label decision per item. Router calls stay serial.
 * Generation-record reads run beside later calls, and this function waits for
 * every read before it returns the entries the report is built from.
 */
async function runRoute<T extends string>(
	items: { prompt: string; expected: T }[],
	deps: RouterRouteDeps,
	parse: (text: string) => T | undefined,
): Promise<RouterCall> {
	const entries: RouterUsageEntry[] = [];
	const values: (string | undefined)[] = [];
	const latencies: number[] = [];
	const recordReads: Promise<void>[] = [];
	const runRecord = withConcurrencyLimit(deps.generationRecordConcurrency ?? GENERATION_RECORD_CONCURRENCY);
	const feature = deps.feature ?? "router_measurement";
	for (const item of items) {
		const started = performance.now();
		const decision = await routeCompletion(item.prompt, {
			apiKey: deps.apiKey,
			fetch: deps.fetch,
			feature,
			generationRecords: deps.generationRecords,
			generationRecordWaitMs: deps.generationRecordWaitMs,
			generationRecordRetryDelayMs: deps.generationRecordRetryDelayMs,
			sleep: deps.sleep,
			// Record reads start here and run beside later calls. latencyMs on
			// the usage entry was already taken, and the window below ends
			// when this completion returns.
			scheduleGenerationRecord: read => {
				recordReads.push(runRecord(read));
			},
			recordUsage: entry => {
				entries.push(entry);
				deps.recordUsage?.(entry);
			},
		});
		latencies.push(performance.now() - started);
		const value = decision.text === undefined ? undefined : parse(decision.text);
		values.push(value);
	}
	await Promise.all(recordReads);
	assertTransportHealthy(entries, feature);
	return { entries, values, latencies };
}

function summarise(
	call: RouterCall,
	correct: number,
	extras: Partial<RouterRouteMetrics> = {},
): RouterRouteMetrics {
	if (call.latencies.length === 0) {
		const metrics: RouterRouteMetrics = {
			sampleSize: 0,
			answerRate: null,
			accuracy: null,
			p50LatencyMs: null,
			p95LatencyMs: null,
			costPer1000Usd: null,
			costSource: "unavailable",
			unparseableRate: null,
			truncatedRate: null,
			excludedLabels: extras.excludedLabels ?? {},
			transportFailureRate: null,
			routedModels: {},
			routedEffort: {},
			...extras,
		};
		metrics.answerRate = null;
		metrics.accuracy = null;
		metrics.p50LatencyMs = null;
		metrics.p95LatencyMs = null;
		metrics.costPer1000Usd = null;
		metrics.costSource = "unavailable";
		metrics.unparseableRate = null;
		metrics.truncatedRate = null;
		metrics.transportFailureRate = null;
		if ("precision" in extras) metrics.precision = null;
		if ("recall" in extras) metrics.recall = null;
		if ("skipSessionShare" in extras) metrics.skipSessionShare = null;
		return metrics;
	}
	const total = call.latencies.length;
	const transportFailures = call.entries.filter(
		e => e.outcome === "http_error" || e.outcome === "network_error" || e.outcome === "timeout",
	).length;
	// Truncated calls returned no content because the completion hit its token
	// budget. They push an undefined value like an unparseable answer, so they
	// are subtracted back out: the cap gets its own count.
	const truncated = call.entries.filter(e => e.outcome === "truncated").length;
	const undefinedValues = call.values.filter(v => v === undefined).length;
	const unparseable = Math.max(undefinedValues - truncated, 0);
	const answered = call.values.filter(v => v !== undefined).length;
	const routedModels: Record<string, number> = {};
	const routedEffort: Record<string, number> = {};
	let measuredCost = 0;
	let measuredCalls = 0;
	// Which surface supplied the measured cost: the completion's own usage.cost,
	// or the generation record. Only costs actually read are summed.
	let fromCompletionUsage = false;
	let fromGenerationRecord = false;
	for (const entry of call.entries) {
		if (entry.routedModel) routedModels[entry.routedModel] = (routedModels[entry.routedModel] ?? 0) + 1;
		if (entry.routedReasoningEffort) {
			routedEffort[entry.routedReasoningEffort] = (routedEffort[entry.routedReasoningEffort] ?? 0) + 1;
		}
		if (entry.costUsd === undefined) continue;
		measuredCost += entry.costUsd;
		measuredCalls += 1;
		if (entry.costSource === "completion-usage") fromCompletionUsage = true;
		else fromGenerationRecord = true;
	}
	const costSource: RouterRouteMetrics["costSource"] =
		measuredCalls === 0
			? "unavailable"
			: fromCompletionUsage
				? "completion-usage"
				: fromGenerationRecord
					? "generation-record"
					: "unavailable";
	// Mean of the calls that reported a cost. Dividing by the full sample would
	// count every unread call as $0 inside an otherwise measured row.
	const costPer1000Usd = measuredCalls === 0 ? null : (measuredCost / measuredCalls) * 1000;

	return {
		sampleSize: call.latencies.length,
		answerRate: answered / total,
		accuracy: correct / total,
		p50LatencyMs: percentile(call.latencies, 0.5),
		p95LatencyMs: percentile(call.latencies, 0.95),
		costPer1000Usd,
		costSource,
		unparseableRate: unparseable / total,
		truncatedRate: truncated / total,
		excludedLabels: {},
		transportFailureRate: transportFailures / total,
		routedModels,
		routedEffort,
		...extras,
	};
}

function assertTransportHealthy(entries: RouterUsageEntry[], feature: string): void {
	for (const entry of entries) {
		if (entry.outcome === "http_error" && (entry.status === 401 || entry.status === 403)) {
			throw new RouterRouteError(
				`Router call failed with HTTP ${entry.status} in ${feature}: authentication or authorization failure`,
			);
		}
	}
	if (entries.length === 0) return;
	const failures = entries.filter(
		e => e.outcome === "http_error" || e.outcome === "network_error" || e.outcome === "timeout",
	).length;
	if (failures / entries.length > ROUTER_TRANSPORT_FAILURE_LIMIT) {
		throw new RouterRouteError(
			`Router transport failures in ${feature} (${failures}/${entries.length}, ${((failures / entries.length) * 100).toFixed(1)}%) exceeded ${ROUTER_TRANSPORT_FAILURE_LIMIT * 100}% limit`,
		);
	}
}

export interface RouterEffortItems {
	/** Items whose dataset label is scoreable, with the label normalized. */
	included: { prompt: string; expected: string }[];
	/** How many items each excluded dataset label cost, keyed by normalized label. */
	excluded: Record<string, number>;
}

/**
 * Split auto-thinking prompts into the items both sides can be scored on and
 * the items whose dataset label is outside the scored vocabulary. An excluded
 * item is not sent to the router and is not in either side's denominator: an
 * unwinnable label would depress both columns and hide the router behind it.
 * The excluded counts are returned so the report can show the bucket, and the
 * current side uses the same partition.
 */
export function partitionEffortPrompts(prompts: RouterPromptItem[]): RouterEffortItems {
	const included: RouterEffortItems["included"] = [];
	const excluded: Record<string, number> = {};
	for (const item of prompts) {
		const raw = item.effort || item.label;
		const expected = scoreableEffortLabel(raw);
		if (expected === undefined) {
			const key = excludedLabelKey(raw);
			excluded[key] = (excluded[key] ?? 0) + 1;
			continue;
		}
		included.push({ prompt: item.prompt, expected });
	}
	return { included, excluded };
}

export async function measureRouterEffort(
	prompts: RouterPromptItem[],
	deps: RouterRouteDeps,
): Promise<RouterRouteMetrics> {
	const { included, excluded } = partitionEffortPrompts(prompts);
	const items = included.map(item => ({ prompt: `${EFFORT_PROMPT}${item.prompt}`, expected: item.expected }));
	const call = await runRoute(items, deps, text => parseFirstLabel(text, ROUTER_EFFORT_LABELS));
	const correct = call.values.filter((value, i) => value !== undefined && value === included[i].expected).length;
	return summarise(call, correct, { excludedLabels: excluded });
}

export async function measureRouterStop(
	turnEnds: RouterTurnEndItem[],
	deps: RouterRouteDeps,
): Promise<RouterRouteMetrics> {
	const items = turnEnds.map(item => ({
		prompt: `${STOP_PROMPT}${item.text}`,
		expected: item.label,
	}));
	// The statement asks whether the message IS an unexpected stop, so a "yes"
	// maps to the continue label.
	const call = await runRoute(items, deps, text => {
		const yesNo = parseYesNo(text);
		if (yesNo === undefined) return undefined;
		return yesNo ? "continue" : "stop";
	});
	const correct = call.values.filter((value, i) => value !== undefined && value === items[i].expected).length;
	const tp = call.values.filter((v, i) => v === "continue" && items[i].expected === "continue").length;
	const fp = call.values.filter((v, i) => v === "continue" && items[i].expected !== "continue").length;
	const fn = call.values.filter((v, i) => v !== "continue" && items[i].expected === "continue").length;
	const precision = tp + fp > 0 ? tp / (tp + fp) : 1;
	const recall = tp + fn > 0 ? tp / (tp + fn) : 1;
	return summarise(call, correct, { precision, recall });
}

export async function measureRouterRobomp(
	issues: RouterIssueItem[],
	deps: RouterRouteDeps,
): Promise<RouterRouteMetrics> {
	const items = issues.map(issue => ({
		prompt: `${ROBOMP_PROMPT}${issue.title}\n\n${issue.body}`,
		expected: issue.label,
	}));
	const call = await runRoute(items, deps, text => parseFirstLabel(text, ROUTER_ROBOMP_PRIMARY_LABELS));
	const correct = call.values.filter((value, i) => value !== undefined && value === items[i].expected).length;
	const skipped = call.values.filter(v => v !== undefined && ROUTER_ROBOMP_SKIP_LABELS.includes(v as never)).length;
	const skipSessionShare = issues.length ? skipped / issues.length : 0;
	return summarise(call, correct, { skipSessionShare });
}

/** The dummy model both classifiers accept; the current side's real model comes from settings. */
const CURRENT_MODEL: Model =
	getBundledModel("anthropic", "claude-sonnet-4-5") ??
	({
		id: "claude-sonnet-4-5",
		provider: "anthropic",
		name: "Claude Sonnet 4.5",
		reasoning: false,
		contextWindow: 200_000,
		maxTokens: 8192,
	} as Model);

interface RouterCurrentItem {
	text: string;
	expected: string;
}

/** Current-side route metrics over the same dataset, produced the OMP-298 way. */
async function measureCurrent(
	items: RouterCurrentItem[],
	mode: "effort" | "stop",
	options: { current?: CurrentSmolHarness; fakeCurrent?: RouterCurrentHandler },
): Promise<RouterRouteMetrics | null> {
	if (items.length === 0) return null;
	const { current, fakeCurrent } = options;
	if (!current && !fakeCurrent) return null;

	const latencies: number[] = [];
	const values: (string | undefined)[] = [];
	let costTotal = 0;

	for (const item of items) {
		let value: string | undefined;
		let cost = 0;
		const started = performance.now();
		if (fakeCurrent && mode === "effort" && fakeCurrent.classifyDifficulty) {
			const res = await fakeCurrent.classifyDifficulty(item.text);
			// Normalize the classifier's own label the same way the dataset
			// label was: the current side can return `max`/`minimal`, which are
			// not in the scored router vocabulary.
			value = scoreableEffortLabel(res.effort);
			cost = res.cost ?? 0;
			latencies.push(res.latencyMs ?? performance.now() - started);
		} else if (fakeCurrent && mode === "stop" && fakeCurrent.classifyUnexpectedStop) {
			const res = await fakeCurrent.classifyUnexpectedStop(item.text);
			value = res.unexpectedStop === undefined ? undefined : res.unexpectedStop ? "continue" : "stop";
			cost = res.cost ?? 0;
			latencies.push(res.latencyMs ?? performance.now() - started);
		} else if (current) {
			const usageCost = { total: 0 };
			try {
				if (mode === "effort") {
					const effort = await classifyDifficulty(
						{ request: item.text },
						{
							settings: withoutJevSettings(current.settings),
							registry: current.registry,
							model: CURRENT_MODEL,
							onUsage: judgment => {
								usageCost.total += judgment.usage.cost.total;
							},
						},
					);
					value = scoreableEffortLabel(effort);
				} else {
					const stopped = await classifyUnexpectedStop(item.text, {
						settings: withoutJevSettings(current.settings),
						registry: current.registry,
						sessionId: "router-measurement-current",
						onUsage: judgment => {
							usageCost.total += judgment.usage.cost.total;
						},
					});
					value = stopped === undefined ? undefined : stopped ? "continue" : "stop";
				}
			} catch {
				value = undefined;
			}
			cost = usageCost.total;
			latencies.push(performance.now() - started);
		} else {
			return null;
		}
		values.push(value);
		costTotal += cost;
	}

	const total = items.length || 1;
	const correct = values.filter((v, i) => v !== undefined && v === items[i].expected).length;
	const unparseable = values.filter(v => v === undefined).length;
	let precision: number | undefined;
	let recall: number | undefined;
	if (mode === "stop") {
		const tp = values.filter((v, i) => v === "continue" && items[i].expected === "continue").length;
		const fp = values.filter((v, i) => v === "continue" && items[i].expected !== "continue").length;
		const fn = values.filter((v, i) => v !== "continue" && items[i].expected === "continue").length;
		precision = tp + fp > 0 ? tp / (tp + fp) : 1;
		recall = tp + fn > 0 ? tp / (tp + fn) : 1;
	}
	return {
		sampleSize: items.length,
		answerRate: (items.length - unparseable) / total,
		accuracy: correct / total,
		precision,
		recall,
		p50LatencyMs: percentile(latencies, 0.5),
		p95LatencyMs: percentile(latencies, 0.95),
		// A side that ran and was charged nothing is a real zero. `null` is reserved
		// for a side that did not run, which this function signals by returning null.
		costPer1000Usd: (costTotal / total) * 1000,
		costSource: costTotal > 0 ? "provider-usage" : "unavailable",
		unparseableRate: unparseable / total,
		truncatedRate: 0,
		excludedLabels: {},
		transportFailureRate: 0,
		routedModels: { [CURRENT_MODEL.id]: items.length },
		routedEffort: {},
	};
}

/**
 * Settings view forcing the Jev decision path off, so the current side measures
 * the plain smol classifier. Mirrors the harness's own `withoutJev`, kept local
 * so this module adds no export to the existing harness.
 */
export function withoutJevSettings(settings: CurrentSmolHarness["settings"]): CurrentSmolHarness["settings"] {
	return settings.overlay({ "jev.enabled": false, "jev.autoThinking": false, "jev.unexpectedStop": false });
}

function emptyRouteMetrics(sampleSize: number): RouterRouteMetrics {
	return {
		sampleSize,
		answerRate: null,
		accuracy: null,
		p50LatencyMs: null,
		p95LatencyMs: null,
		costPer1000Usd: null,
		costSource: "unavailable",
		unparseableRate: null,
		truncatedRate: null,
		excludedLabels: {},
		transportFailureRate: null,
		routedModels: {},
		routedEffort: {},
		skipSessionShare: null,
	};
}

export async function runRouterMeasurement(options: RouterMeasurementOptions): Promise<RouterMeasurementResults> {
	const prompts = options.prompts ?? [];
	const turnEnds = options.turnEnds ?? [];
	const issues = options.issues ?? [];

	const routeDeps = (feature: string): RouterRouteDeps => ({
		apiKey: options.apiKey,
		fetch: options.fetch,
		generationRecords: options.generationRecords,
		generationRecordWaitMs: options.generationRecordWaitMs,
		generationRecordRetryDelayMs: options.generationRecordRetryDelayMs,
		generationRecordConcurrency: options.generationRecordConcurrency,
		sleep: options.sleep,
		feature,
	});

	const autoThinkingRoute = await measureRouterEffort(prompts, routeDeps("router_auto_thinking"));
	const unexpectedStopRoute = await measureRouterStop(turnEnds, routeDeps("router_unexpected_stop"));
	let robompRoute = emptyRouteMetrics(issues.length);
	if (options.robompRunner) {
		const partial = await options.robompRunner(issues, routeDeps("router_robomp"));
		if (partial) robompRoute = { ...robompRoute, ...partial };
	} else {
		robompRoute = await measureRouterRobomp(issues, routeDeps("router_robomp"));
	}

	const currentAutoThinking = await measureCurrent(
		// Same partition as the router: an item whose dataset label cannot be
		// scored is excluded from the current side's denominator too, so both
		// columns are computed over the identical set.
		partitionEffortPrompts(prompts).included.map(item => ({ text: item.prompt, expected: item.expected })),
		"effort",
		{ current: options.current, fakeCurrent: options.fakeCurrent },
	);
	const currentUnexpectedStop = await measureCurrent(
		turnEnds.map(t => ({ text: t.text, expected: t.label })),
		"stop",
		{ current: options.current, fakeCurrent: options.fakeCurrent },
	);

	return {
		features: {
			auto_thinking_route: autoThinkingRoute,
			unexpected_stop_route: unexpectedStopRoute,
			robomp_route: robompRoute,
		},
		current: { auto_thinking: currentAutoThinking, unexpected_stop: currentUnexpectedStop },
		verdict: WP5_ROUTER_DOES_NOT_APPLY,
		routerModel: OPENROUTER_JEV_ROUTER_MODEL,
		sampleSizes: {
			auto_thinking: prompts.length,
			unexpected_stop: turnEnds.length,
			robomp: issues.length,
		},
	};
}
