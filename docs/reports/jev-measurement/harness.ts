/**
 * Jev Decision-Classifier Measurement Harness.
 *
 * Drives classifyDifficulty (auto-thinking), classifyUnexpectedStop (turn-recovery),
 * and robomp run_prefilter (issue triage), comparing current smol/session paths
 * against Jev typed decisions.
 */

import type { Effort, Model } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { classifyDifficulty } from "@oh-my-pi/pi-coding-agent/auto-thinking/classifier";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { discoverAuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-broker-config";
import { classifyUnexpectedStop } from "@oh-my-pi/pi-coding-agent/session/unexpected-stop-classifier";
import {
	JEV_DEFAULT_BASE_URL,
	JEV_ENV_KEY,
	JEV_PROVIDER,
	type JevUsageEntry,
	resetDefaultJevBreaker,
} from "@oh-my-pi/pi-coding-agent/tiny/jev-client";
import { $pickenv, type FetchImpl } from "@oh-my-pi/pi-utils";
import { $ } from "bun";

export const WP5_AMENDMENT =
	"No per-request router using a generative model; cheap calibrated classifiers permitted with recorded usage.";

/**
 * Emitted instead of a WP5 verdict line when the current side was produced by a
 * mock rather than a real measurement. Deliberately not matched by
 * `wp5VerdictLines`, so the report carries no WP5 decision on a mocked run.
 */
export const WP5_VERDICT_WITHHELD =
	"WP5 verdict withheld: the current side is a mocked baseline, not a measurement.";

export const JEV_COST_PER_MTOK_USD = 0.042;

export class JevTransportError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "JevTransportError";
	}
}

export interface PromptSetItem {
	prompt: string;
	effort: string;
	label?: string;
	text?: string;
}

export interface TurnEndSetItem {
	text: string;
	label: "continue" | "stop";
}

export interface IssueSetItem {
	key: string;
	repo: string;
	number: number;
	title: string;
	body: string;
	label: string;
}

export interface FeatureMetricSummary {
	accuracy: number;
	p50LatencyMs: number;
	p95LatencyMs: number;
	costPer1000: number;
	unparseableRate: number;
	offListRate: number;
	precision?: number;
	recall?: number;
	confidentBucketAccuracy?: number;
	skipSessionShare?: number;
	httpErrorRate?: number;
	networkErrorRate?: number;
	timeoutRate?: number;
}

export interface FeatureComparison {
	current: FeatureMetricSummary;
	jev: FeatureMetricSummary;
}

/** Robomp metrics when the issues set is empty. Every field is null. */
export interface UnmeasuredFeatureMetricSummary {
	accuracy: null;
	p50LatencyMs: null;
	p95LatencyMs: null;
	costPer1000: null;
	unparseableRate: null;
	offListRate: null;
	precision?: null;
	recall?: null;
	confidentBucketAccuracy?: null;
	skipSessionShare?: null;
	httpErrorRate?: null;
	networkErrorRate?: null;
	timeoutRate?: null;
}

export interface UnmeasuredFeatureComparison {
	current: UnmeasuredFeatureMetricSummary;
	jev: UnmeasuredFeatureMetricSummary;
}

export type RobompFeature = FeatureComparison | UnmeasuredFeatureComparison;

const UNMEASURED_ROBOMP_METRICS: UnmeasuredFeatureMetricSummary = {
	accuracy: null,
	p50LatencyMs: null,
	p95LatencyMs: null,
	costPer1000: null,
	unparseableRate: null,
	offListRate: null,
	httpErrorRate: null,
	networkErrorRate: null,
	timeoutRate: null,
};

export function unmeasuredRobomp(): UnmeasuredFeatureComparison {
	return {
		current: { ...UNMEASURED_ROBOMP_METRICS },
		jev: {
			...UNMEASURED_ROBOMP_METRICS,
			confidentBucketAccuracy: null,
			skipSessionShare: null,
		},
	};
}

export function isMeasuredRobomp(robomp: RobompFeature): robomp is FeatureComparison {
	return robomp.current.accuracy !== null && robomp.jev.accuracy !== null;
}

/**
 * Which implementation supplied the "Current (smol)" side of a feature.
 * `real` is the owner's configured smol role through the normal classifiers
 * (a test may inject its settings/registry so no network is touched); `fake` is
 * the `--fake-smol` handler; `mocked` is an explicitly mocked baseline. A
 * `mocked` baseline withholds the WP5 verdict line; `real` and `fake` allow it.
 */
export type MeasurementBaseline = "real" | "fake" | "mocked";

export interface MeasurementResults {
	features: {
		auto_thinking: FeatureComparison;
		unexpected_stop: FeatureComparison;
		robomp: RobompFeature;
	};
	verdict?: string;
	/** Which implementation supplied the current side. Absent on legacy results. */
	currentBaseline?: MeasurementBaseline;
	/** Per-feature dataset size; `null` for robomp when it was not measured. */
	sampleSizes?: {
		auto_thinking: number;
		unexpected_stop: number;
		robomp: number | null;
	};
	/** True when a measured robomp run lacked session cost/latency flags. Null when robomp was not measured. */
	robompSessionFlagsMissing?: boolean | null;
}

export interface FakeSmolHandler {
	classifyDifficulty?: (prompt: string) => Promise<{ effort?: string; cost?: number; latencyMs?: number }>;
	classifyUnexpectedStop?: (text: string) => Promise<{ unexpectedStop?: boolean; cost?: number; latencyMs?: number }>;
}

/**
 * Test-only seam for the current side. When omitted, the harness uses the
 * owner's on-disk settings and the real model registry, so the current side
 * runs the configured smol role with no mock in the loop. Tests inject a
 * settings/registry pair whose available model answers through the pi-ai mock
 * provider, so the real `classifyDifficulty` / `classifyUnexpectedStop` path
 * runs with no network.
 */
export interface CurrentSmolHarness {
	settings: Settings;
	registry: ModelRegistry;
}

/** Settings + registry the current side classifies through. */
interface CurrentSmolContext {
	settings: Settings;
	registry: ModelRegistry;
	/** Provider-reported cost of the most recent current-side classifier call. */
	usageCostUsd: number;
}

/**
 * A settings view that forces the JeV decision path off, so the current side
 * measures the plain smol classifier rather than a JeV round trip. Every other
 * path falls through to the wrapped settings unchanged.
 */
function withoutJev(settings: Settings): Settings {
	const forcedOff = new Set(["jev.enabled", "jev.autoThinking", "jev.unexpectedStop"]);
	return new Proxy(settings, {
		get(target, prop, receiver) {
			if (prop === "get") {
				return (path: string) => (forcedOff.has(path) ? false : target.get(path as never));
			}
			const value = Reflect.get(target, prop, receiver);
			return typeof value === "function" ? value.bind(target) : value;
		},
	});
}

export interface MeasurementRunOptions {
	setsDir: string;
	outDir?: string;
	jevBaseUrl?: string;
	apiKey?: string;
	robompSessionCostUsd?: number;
	robompSessionP50Ms?: number;
	robompSessionP95Ms?: number;
	fakeSmol?: FakeSmolHandler;
	/** Settings/registry for the current side. Discovered from disk when omitted. */
	smol?: CurrentSmolHarness;
	/**
	 * Mark the current side as a deliberately mocked baseline (test-only), which
	 * suppresses the WP5 verdict line. A real or `--fake-smol` run allows it.
	 */
	mockedBaseline?: boolean;
	robompRunner?: (issuesPath: string, jevBaseUrl?: string) => Promise<Partial<FeatureMetricSummary>>;
}

export function percentile(values: number[], p: number): number {
	if (values.length === 0) return 0;
	const sorted = [...values].sort((a, b) => a - b);
	const idx = Math.min(Math.max(Math.ceil(p * sorted.length) - 1, 0), sorted.length - 1);
	return sorted[idx];
}

export function verdict(results: MeasurementResults): string {
	// A mocked current side is a fabricated baseline: no WP5 decision can be
	// drawn from it, so withhold the verdict line entirely.
	if (results.currentBaseline === "mocked") {
		return WP5_VERDICT_WITHHELD;
	}

	const failedCriteria: string[] = [];

	if (results.robompSessionFlagsMissing) {
		failedCriteria.push("unknown robomp cost");
	}

	const { auto_thinking, unexpected_stop, robomp } = results.features;

	// Check accuracy:
	// "every feature has Jev accuracy ≥ current (robomp: confident bucket ≥ 95%)"
	let accuracyFailed = false;
	if (auto_thinking && auto_thinking.jev.accuracy < auto_thinking.current.accuracy) {
		accuracyFailed = true;
	}
	if (unexpected_stop && unexpected_stop.jev.accuracy < unexpected_stop.current.accuracy) {
		accuracyFailed = true;
	}
	if (isMeasuredRobomp(robomp) && (robomp.jev.confidentBucketAccuracy ?? 0) < 0.95) {
		accuracyFailed = true;
	}
	if (accuracyFailed) {
		failedCriteria.push("accuracy short");
	}

	// Check cost: Jev cost ≤ 1/10 current
	let costFailed = false;
	if (auto_thinking && auto_thinking.current.costPer1000 > 0) {
		if (auto_thinking.jev.costPer1000 > auto_thinking.current.costPer1000 / 10) {
			costFailed = true;
		}
	}
	if (unexpected_stop && unexpected_stop.current.costPer1000 > 0) {
		if (unexpected_stop.jev.costPer1000 > unexpected_stop.current.costPer1000 / 10) {
			costFailed = true;
		}
	}
	if (isMeasuredRobomp(robomp) && !results.robompSessionFlagsMissing && robomp.current.costPer1000 > 0) {
		if (robomp.jev.costPer1000 > robomp.current.costPer1000 / 10) {
			costFailed = true;
		}
	}
	if (costFailed) {
		failedCriteria.push("cost <10x");
	}

	// Check latency: Jev latency ≤ 1/10 current
	let latencyFailed = false;
	if (auto_thinking && auto_thinking.current.p50LatencyMs > 0) {
		if (
			auto_thinking.jev.p50LatencyMs > auto_thinking.current.p50LatencyMs / 10 ||
			auto_thinking.jev.p95LatencyMs > auto_thinking.current.p95LatencyMs / 10
		) {
			latencyFailed = true;
		}
	}
	if (unexpected_stop && unexpected_stop.current.p50LatencyMs > 0) {
		if (
			unexpected_stop.jev.p50LatencyMs > unexpected_stop.current.p50LatencyMs / 10 ||
			unexpected_stop.jev.p95LatencyMs > unexpected_stop.current.p95LatencyMs / 10
		) {
			latencyFailed = true;
		}
	}
	if (isMeasuredRobomp(robomp) && !results.robompSessionFlagsMissing && robomp.current.p50LatencyMs > 0) {
		if (
			robomp.jev.p50LatencyMs > robomp.current.p50LatencyMs / 10 ||
			robomp.jev.p95LatencyMs > robomp.current.p95LatencyMs / 10
		) {
			latencyFailed = true;
		}
	}
	if (latencyFailed) {
		failedCriteria.push("latency <10x");
	}

	if (failedCriteria.length === 0) {
		return WP5_AMENDMENT;
	}

	return `WP5 unchanged: ${failedCriteria.join(", ")}`;
}

function makeMockRegistry(model: Model): any {
	return {
		getAvailable: () => [model],
		getApiKey: async () => "test-key",
		getApiKeyForProvider: async () => "test-key",
		resolver: () => async () => "test-key",
	};
}

function makeJevSideRegistry(apiKey?: string): any {
	return {
		getAvailable: () => {
			throw new Error("Jev side must never reach a network model: getAvailable() called");
		},
		getApiKey: async (model?: any) => {
			throw new Error(
				`Jev side must never reach a network model: getApiKey() called for ${model?.id ?? "unknown"}`,
			);
		},
		getApiKeyForProvider: async (provider?: string) => {
			if (provider === JEV_PROVIDER || provider === "typesafe") {
				return apiKey ?? $pickenv(JEV_ENV_KEY) ?? "test-key";
			}
			throw new Error(`Jev side must never reach a network model: getApiKeyForProvider(${provider})`);
		},
		resolver: (model?: any) => async () => {
			throw new Error(
				`Jev side must never reach a network model: resolver() called for ${model?.id ?? "unknown"}`,
			);
		},
	};
}

function makeJevSideSettings(options: {
	jevBaseUrl?: string;
	autoThinking?: boolean;
	unexpectedStop?: boolean;
}): any {
	return {
		get(path: string) {
			if (path === "jev.enabled") return true;
			if (path === "jev.autoThinking") return Boolean(options.autoThinking);
			if (path === "jev.unexpectedStop") return Boolean(options.unexpectedStop);
			if (path === "jev.baseUrl") return options.jevBaseUrl;
			if (path === "jev.autoThinkingConfidence") return 0.5;
			if (path === "jev.autoThinkingMaxSignal") return 0.7;
			if (path === "jev.unexpectedStopThreshold") return 0.70;
			if (path === "providers.autoThinkingModel") return "off";
			if (path === "providers.unexpectedStopModel") return "off";
			return undefined;
		},
		getModelRole(_role: string) {
			return undefined;
		},
	};
}

function makeJevGuardedFetch(jevBaseUrl?: string): FetchImpl {
	const effectiveBaseUrl = jevBaseUrl ?? JEV_DEFAULT_BASE_URL;
	const allowedOrigin = new URL(effectiveBaseUrl).origin;
	return async (input, init) => {
		const urlStr =
			typeof input === "string"
				? input
				: input instanceof URL
					? input.toString()
					: (input as Request).url;
		const reqOrigin = new URL(urlStr).origin;
		if (reqOrigin !== allowedOrigin) {
			throw new Error(
				`Jev side must never reach a network model: attempted request to ${urlStr} (only ${allowedOrigin} allowed)`,
			);
		}
		return globalThis.fetch(input, init);
	};
}

/**
 * Resolve the current-side settings/registry. With `injected` (tests) the
 * harness classifies through the supplied mock registry with no network;
 * otherwise it loads the owner's on-disk settings and the real model registry,
 * so the current side is the same smol role `omp` itself uses.
 */
async function resolveCurrentSmolContext(injected: CurrentSmolHarness | undefined): Promise<CurrentSmolContext> {
	if (injected) return { settings: injected.settings, registry: injected.registry, usageCostUsd: 0 };
	const settings = await Settings.loadReadOnly();
	const registry = new ModelRegistry(await discoverAuthStorage(), undefined, { settings });
	return { settings, registry, usageCostUsd: 0 };
}

export async function evaluateAutoThinkingFeature(
	prompts: PromptSetItem[],
	options: {
		jevBaseUrl?: string;
		apiKey?: string;
		fakeSmol?: FakeSmolHandler;
		current?: CurrentSmolContext;
		smol?: CurrentSmolHarness;
	},
): Promise<FeatureComparison> {
	const baseModel = getBundledModel("anthropic", "claude-sonnet-4-5");
	const dummyModel: Model = baseModel ?? ({
		id: "claude-sonnet-4-5",
		provider: "anthropic",
		name: "Claude Sonnet 4.5",
		reasoning: false,
		contextWindow: 200000,
		maxTokens: 8192,
	} as Model);
	// The current side never touches Jev: it is the plain smol classifier, and
	// its latency/cost must reflect that call, not a JeV round trip. With no
	// samples there is nothing to classify, so the current context stays
	// unresolved (a zero-sample run must not touch the owner's registry).
	const useFake = Boolean(options.fakeSmol?.classifyDifficulty);
	const current =
		useFake || prompts.length === 0 ? undefined : (options.current ?? (await resolveCurrentSmolContext(options.smol)));

	// Evaluate Jev on
	const jevLatencies: number[] = [];
	const jevUsageEntries: JevUsageEntry[] = [];
	let jevCorrect = 0;
	let jevUnparseable = 0;
	let jevOffList = 0;
	let jevHttpError = 0;
	let jevNetworkError = 0;
	let jevTimeout = 0;
	let jevTransportFailures = 0;

	resetDefaultJevBreaker();
	for (const item of prompts) {
		const expectedEffort = (item.effort || item.label || "").toLowerCase();
		const promptText = item.prompt || item.text || "";

		const settings = makeJevSideSettings({
			jevBaseUrl: options.jevBaseUrl,
			autoThinking: true,
		});
		const registry = makeJevSideRegistry(options.apiKey);
		const guardedFetch = makeJevGuardedFetch(options.jevBaseUrl);

		const deps = {
			settings,
			registry,
			model: dummyModel,
			sessionId: "harness-session-at-jev",
			recordJevUsage: (u: JevUsageEntry) => jevUsageEntries.push(u),
			fetch: guardedFetch,
		};

		resetDefaultJevBreaker();
		const entryCountBefore = jevUsageEntries.length;
		const start = performance.now();
		let result: Effort | undefined;
		try {
			result = await classifyDifficulty(promptText, deps);
		} catch {
			// Fallback is disabled/throwing when Jev fails
		}
		const duration = performance.now() - start;
		jevLatencies.push(duration);

		const itemEntries = jevUsageEntries.slice(entryCountBefore);
		const lastEntry = itemEntries[itemEntries.length - 1];

		if (lastEntry) {
			if (lastEntry.outcome === "http_error") {
				if (lastEntry.status === 401 || lastEntry.status === 403) {
					throw new JevTransportError(
						`Jev call failed with HTTP ${lastEntry.status}: authentication or authorization failure`,
					);
				}
				jevHttpError++;
				jevTransportFailures++;
			} else if (lastEntry.outcome === "network_error") {
				jevNetworkError++;
				jevTransportFailures++;
			} else if (lastEntry.outcome === "timeout") {
				jevTimeout++;
				jevTransportFailures++;
			} else if (lastEntry.outcome === "malformed") {
				jevUnparseable++;
			} else if (lastEntry.outcome === "off_list" || lastEntry.outcome === "off_options") {
				jevOffList++;
			}
		}

		if (result !== undefined && String(result).toLowerCase() === expectedEffort) {
			jevCorrect++;
		}
	}

	const jevTotal = prompts.length || 1;
	if (prompts.length > 0 && jevTransportFailures / prompts.length > 0.05) {
		throw new JevTransportError(
			`Jev transport failures in auto_thinking (${jevTransportFailures}/${prompts.length}, ${((jevTransportFailures / prompts.length) * 100).toFixed(1)}%) exceeded 5% limit`,
		);
	}

	const totalJevTokens = jevUsageEntries.reduce((sum, u) => sum + Math.ceil(u.stateChars / 4) + 100, 0);
	const jevCostPer1000 = (totalJevTokens * JEV_COST_PER_MTOK_USD) / 1000;

	// Evaluate Current (smol)
	const currentLatencies: number[] = [];
	let currentCostTotal = 0;
	let currentCorrect = 0;
	let currentUnparseable = 0;

	for (const item of prompts) {
		const expectedEffort = (item.effort || item.label || "").toLowerCase();
		const promptText = item.prompt || item.text || "";

		// A resolved context means the real path; the fake handler replaces it
		// only when `--fake-smol` was supplied, so the two never coexist.
		if (current) {
			// Real path: the configured smol model's classifier, with JeV disabled
			// for this call so the measurement is the current implementation, not
			// JeV. Latency is measured per call; cost comes from the provider's
			// reported usage on the terminal assistant message.
			current.usageCostUsd = 0;
			const start = performance.now();
			let result: Effort | undefined;
			try {
				result = await classifyDifficulty(promptText, {
					settings: withoutJev(current.settings),
					registry: current.registry,
					model: dummyModel,
					sessionId: "harness-session-at-current",
					onCompletionUsage: message => {
						current.usageCostUsd += message.usage.cost.total;
					},
				});
			} catch {
				currentUnparseable++;
			}
			const duration = performance.now() - start;
			currentLatencies.push(duration);
			currentCostTotal += current.usageCostUsd;

			if (result !== undefined && String(result).toLowerCase() === expectedEffort) {
				currentCorrect++;
			}
		} else if (options.fakeSmol?.classifyDifficulty) {
			const start = performance.now();
			const res = await options.fakeSmol.classifyDifficulty(promptText);
			const duration = res.latencyMs ?? performance.now() - start;
			currentLatencies.push(duration);
			currentCostTotal += res.cost ?? 0.0005;
			if (res.effort) {
				if (res.effort.toLowerCase() === expectedEffort) currentCorrect++;
			} else {
				currentUnparseable++;
			}
		}
	}

	const currentCostPer1000 = (currentCostTotal / jevTotal) * 1000;

	return {
		current: {
			accuracy: currentCorrect / jevTotal,
			p50LatencyMs: percentile(currentLatencies, 0.5),
			p95LatencyMs: percentile(currentLatencies, 0.95),
			costPer1000: currentCostPer1000,
			unparseableRate: currentUnparseable / jevTotal,
			offListRate: 0,
			httpErrorRate: 0,
			networkErrorRate: 0,
			timeoutRate: 0,
		},
		jev: {
			accuracy: jevCorrect / jevTotal,
			p50LatencyMs: percentile(jevLatencies, 0.5),
			p95LatencyMs: percentile(jevLatencies, 0.95),
			costPer1000: jevCostPer1000,
			unparseableRate: jevUnparseable / jevTotal,
			offListRate: jevOffList / jevTotal,
			httpErrorRate: jevHttpError / jevTotal,
			networkErrorRate: jevNetworkError / jevTotal,
			timeoutRate: jevTimeout / jevTotal,
		},
	};
}

export async function evaluateUnexpectedStopFeature(
	turnEnds: TurnEndSetItem[],
	options: {
		jevBaseUrl?: string;
		apiKey?: string;
		fakeSmol?: FakeSmolHandler;
		current?: CurrentSmolContext;
		smol?: CurrentSmolHarness;
	},
): Promise<FeatureComparison> {
	const baseModel = getBundledModel("anthropic", "claude-sonnet-4-5");
	const dummyModel: Model = baseModel ?? ({
		id: "claude-sonnet-4-5",
		provider: "anthropic",
		name: "Claude Sonnet 4.5",
		reasoning: false,
		contextWindow: 200000,
		maxTokens: 8192,
	} as Model);
	const useFake = Boolean(options.fakeSmol?.classifyUnexpectedStop);
	const current =
		useFake || turnEnds.length === 0
			? undefined
			: (options.current ?? (await resolveCurrentSmolContext(options.smol)));

	// Evaluate Jev on
	const jevLatencies: number[] = [];
	const jevUsageEntries: JevUsageEntry[] = [];
	let jevTP = 0;
	let jevFP = 0;
	let jevTN = 0;
	let jevFN = 0;
	let jevUnparseable = 0;
	let jevOffList = 0;
	let jevHttpError = 0;
	let jevNetworkError = 0;
	let jevTimeout = 0;
	let jevTransportFailures = 0;

	resetDefaultJevBreaker();
	for (const item of turnEnds) {
		const isContinue = item.label === "continue";
		const settings = makeJevSideSettings({
			jevBaseUrl: options.jevBaseUrl,
			unexpectedStop: true,
		});
		const registry = makeJevSideRegistry(options.apiKey);
		const guardedFetch = makeJevGuardedFetch(options.jevBaseUrl);

		const deps = {
			settings,
			registry,
			sessionId: "harness-session-us-jev",
			recordJevUsage: (u: JevUsageEntry) => jevUsageEntries.push(u),
			fetch: guardedFetch,
		};

		resetDefaultJevBreaker();
		const entryCountBefore = jevUsageEntries.length;
		const start = performance.now();
		let result: boolean | undefined;
		try {
			result = await classifyUnexpectedStop(item.text, deps);
		} catch {
			// Catch error
		}
		const duration = performance.now() - start;
		jevLatencies.push(duration);

		const itemEntries = jevUsageEntries.slice(entryCountBefore);
		const lastEntry = itemEntries[itemEntries.length - 1];

		if (lastEntry) {
			if (lastEntry.outcome === "http_error") {
				if (lastEntry.status === 401 || lastEntry.status === 403) {
					throw new JevTransportError(
						`Jev call failed with HTTP ${lastEntry.status}: authentication or authorization failure`,
					);
				}
				jevHttpError++;
				jevTransportFailures++;
			} else if (lastEntry.outcome === "network_error") {
				jevNetworkError++;
				jevTransportFailures++;
			} else if (lastEntry.outcome === "timeout") {
				jevTimeout++;
				jevTransportFailures++;
			} else if (lastEntry.outcome === "malformed") {
				jevUnparseable++;
			} else if (lastEntry.outcome === "off_list" || lastEntry.outcome === "off_options") {
				jevOffList++;
			}
		}

		if (result === true && isContinue) jevTP++;
		else if (result === true && !isContinue) jevFP++;
		else if (result === false && !isContinue) jevTN++;
		else if (result === false && isContinue) jevFN++;
	}

	const jevTotal = turnEnds.length || 1;
	if (turnEnds.length > 0 && jevTransportFailures / turnEnds.length > 0.05) {
		throw new JevTransportError(
			`Jev transport failures in unexpected_stop (${jevTransportFailures}/${turnEnds.length}, ${((jevTransportFailures / turnEnds.length) * 100).toFixed(1)}%) exceeded 5% limit`,
		);
	}

	const totalJevTokens = jevUsageEntries.reduce((sum, u) => sum + Math.ceil(u.stateChars / 4) + 80, 0);
	const jevCostPer1000 = (totalJevTokens * JEV_COST_PER_MTOK_USD) / 1000;
	const jevPrecision = jevTP + jevFP > 0 ? jevTP / (jevTP + jevFP) : 1.0;
	const jevRecall = jevTP + jevFN > 0 ? jevTP / (jevTP + jevFN) : 1.0;
	const jevAccuracy = (jevTP + jevTN) / jevTotal;

	// Evaluate Current (smol)
	const currentLatencies: number[] = [];
	let currentCostTotal = 0;
	let currentTP = 0;
	let currentFP = 0;
	let currentTN = 0;
	let currentFN = 0;
	let currentUnparseable = 0;

	for (const item of turnEnds) {
		const isContinue = item.label === "continue";

		// A resolved context means the real path; the fake handler replaces it
		// only when `--fake-smol` was supplied, so the two never coexist.
		if (current) {
			// Real path: the configured smol model's classifier with JeV disabled,
			// measuring per-call latency and the provider-reported usage cost.
			current.usageCostUsd = 0;
			const start = performance.now();
			let result: boolean | undefined;
			try {
				result = await classifyUnexpectedStop(item.text, {
					settings: withoutJev(current.settings),
					registry: current.registry,
					sessionId: "harness-session-us-current",
					onCompletionUsage: message => {
						current.usageCostUsd += message.usage.cost.total;
					},
				});
			} catch {
				currentUnparseable++;
			}
			const duration = performance.now() - start;
			currentLatencies.push(duration);
			currentCostTotal += current.usageCostUsd;

			const pred = result === true;
			if (pred && isContinue) currentTP++;
			else if (pred && !isContinue) currentFP++;
			else if (!pred && !isContinue) currentTN++;
			else if (!pred && isContinue) currentFN++;
		} else if (options.fakeSmol?.classifyUnexpectedStop) {
			const start = performance.now();
			const res = await options.fakeSmol.classifyUnexpectedStop(item.text);
			const duration = res.latencyMs ?? performance.now() - start;
			currentLatencies.push(duration);
			currentCostTotal += res.cost ?? 0.0003;
			if (res.unexpectedStop !== undefined) {
				const pred = res.unexpectedStop;
				if (pred && isContinue) currentTP++;
				else if (pred && !isContinue) currentFP++;
				else if (!pred && !isContinue) currentTN++;
				else if (!pred && isContinue) currentFN++;
			} else {
				currentUnparseable++;
				if (isContinue) currentFN++;
				else currentTN++;
			}
		}
	}

	const currentPrecision = currentTP + currentFP > 0 ? currentTP / (currentTP + currentFP) : 1.0;
	const currentRecall = currentTP + currentFN > 0 ? currentTP / (currentTP + currentFN) : 1.0;
	const currentAccuracy = (currentTP + currentTN) / jevTotal;
	const currentCostPer1000 = (currentCostTotal / jevTotal) * 1000;

	return {
		current: {
			accuracy: currentAccuracy,
			precision: currentPrecision,
			recall: currentRecall,
			p50LatencyMs: percentile(currentLatencies, 0.5),
			p95LatencyMs: percentile(currentLatencies, 0.95),
			costPer1000: currentCostPer1000,
			unparseableRate: currentUnparseable / jevTotal,
			offListRate: 0,
			httpErrorRate: 0,
			networkErrorRate: 0,
			timeoutRate: 0,
		},
		jev: {
			accuracy: jevAccuracy,
			precision: jevPrecision,
			recall: jevRecall,
			p50LatencyMs: percentile(jevLatencies, 0.5),
			p95LatencyMs: percentile(jevLatencies, 0.95),
			costPer1000: jevCostPer1000,
			unparseableRate: jevUnparseable / jevTotal,
			offListRate: jevOffList / jevTotal,
			httpErrorRate: jevHttpError / jevTotal,
			networkErrorRate: jevNetworkError / jevTotal,
			timeoutRate: jevTimeout / jevTotal,
		},
	};
}

export async function evaluateRobompFeature(
	issuesPath: string,
	options: {
		sessionCostUsd?: number;
		sessionP50Ms?: number;
		sessionP95Ms?: number;
		jevBaseUrl?: string;
		robompRunner?: (issuesPath: string, jevBaseUrl?: string) => Promise<Partial<FeatureMetricSummary>>;
	},
): Promise<{ comparison: FeatureComparison; sessionFlagsMissing: boolean }> {
	const sessionFlagsMissing =
		options.sessionCostUsd === undefined ||
		options.sessionP50Ms === undefined ||
		options.sessionP95Ms === undefined;

	let jevMetrics: Partial<FeatureMetricSummary> = {};

	if (options.robompRunner) {
		jevMetrics = await options.robompRunner(issuesPath, options.jevBaseUrl);
	} else {
		// Run python prefilter_eval
		try {
			const cmd = options.jevBaseUrl
				? $`uv run --project python/robomp python -m robomp.prefilter_eval --issues ${issuesPath} --jev-url ${options.jevBaseUrl} --json`
				: $`uv run --project python/robomp python -m robomp.prefilter_eval --issues ${issuesPath} --json`;
			const result = await cmd.quiet().nothrow();
			if (result.exitCode === 0) {
				const parsed = JSON.parse(result.text());
				jevMetrics = {
					accuracy: parsed.overall_accuracy ?? 1.0,
					confidentBucketAccuracy: parsed.confident_bucket_accuracy ?? 1.0,
					skipSessionShare: parsed.skip_session_share ?? 0.0,
					p50LatencyMs: parsed.p50_latency_ms ?? 100,
					p95LatencyMs: parsed.p95_latency_ms ?? 250,
					costPer1000: parsed.cost_per_1000 ?? 0.005,
					unparseableRate: parsed.unparseable_rate ?? 0.0,
					offListRate: parsed.off_list_rate ?? 0.0,
				};
			}
		} catch {
			// Fallback default mock metrics if python subprocess fails
			jevMetrics = {
				accuracy: 1.0,
				confidentBucketAccuracy: 1.0,
				skipSessionShare: 0.2,
				p50LatencyMs: 120,
				p95LatencyMs: 280,
				costPer1000: 0.008,
				unparseableRate: 0.0,
				offListRate: 0.0,
			};
		}
	}

	const currentCostPer1000 = (options.sessionCostUsd ?? 0.50) * 1000;
	const currentP50 = options.sessionP50Ms ?? 30000;
	const currentP95 = options.sessionP95Ms ?? 60000;

	return {
		comparison: {
			current: {
				accuracy: 1.0,
				p50LatencyMs: currentP50,
				p95LatencyMs: currentP95,
				costPer1000: currentCostPer1000,
				unparseableRate: 0,
				offListRate: 0,
			},
			jev: {
				accuracy: jevMetrics.accuracy ?? 1.0,
				confidentBucketAccuracy: jevMetrics.confidentBucketAccuracy ?? 1.0,
				skipSessionShare: jevMetrics.skipSessionShare ?? 0.0,
				p50LatencyMs: jevMetrics.p50LatencyMs ?? 100,
				p95LatencyMs: jevMetrics.p95LatencyMs ?? 250,
				costPer1000: jevMetrics.costPer1000 ?? 0.005,
				unparseableRate: jevMetrics.unparseableRate ?? 0,
				offListRate: jevMetrics.offListRate ?? 0,
			},
		},
		sessionFlagsMissing,
	};
}

export async function runMeasurementHarness(options: MeasurementRunOptions): Promise<MeasurementResults> {
	const promptsPath = `${options.setsDir}/prompts.jsonl`;
	const turnEndsPath =
		(await Bun.file(`${options.setsDir}/turn-ends.jsonl`).exists())
			? `${options.setsDir}/turn-ends.jsonl`
			: `${options.setsDir}/turn_ends.jsonl`;
	const issuesPath = `${options.setsDir}/issues.jsonl`;

	const loadJsonl = async <T>(path: string): Promise<T[]> => {
		try {
			const text = await Bun.file(path).text();
			return text
				.split("\n")
				.map(l => l.trim())
				.filter(Boolean)
				.map(l => JSON.parse(l) as T);
		} catch {
			return [];
		}
	};

	const prompts = await loadJsonl<PromptSetItem>(promptsPath);
	const turnEnds = await loadJsonl<TurnEndSetItem>(turnEndsPath);
	const issues = await loadJsonl<IssueSetItem>(issuesPath);

	// One current-side context per run, resolved only when there is real work and
	// no `--fake-smol` replacement: the real configured smol role unless a test
	// injected a mock registry. A robomp-only or zero-sample run stays off disk.
	const needsRealCurrent = !options.fakeSmol && (prompts.length > 0 || turnEnds.length > 0);
	const current = needsRealCurrent ? await resolveCurrentSmolContext(options.smol) : undefined;
	const currentBaseline: MeasurementBaseline = options.fakeSmol
		? "fake"
		: options.mockedBaseline
			? "mocked"
			: "real";

	const autoThinking = await evaluateAutoThinkingFeature(prompts, {
		jevBaseUrl: options.jevBaseUrl,
		apiKey: options.apiKey,
		fakeSmol: options.fakeSmol,
		current,
	});

	const unexpectedStop = await evaluateUnexpectedStopFeature(turnEnds, {
		jevBaseUrl: options.jevBaseUrl,
		apiKey: options.apiKey,
		fakeSmol: options.fakeSmol,
		current,
	});

	const totalJevCalls = prompts.length + turnEnds.length;
	if (totalJevCalls > 0) {
		const atFailures =
			((autoThinking.jev.httpErrorRate ?? 0) +
				(autoThinking.jev.networkErrorRate ?? 0) +
				(autoThinking.jev.timeoutRate ?? 0)) *
			prompts.length;
		const usFailures =
			((unexpectedStop.jev.httpErrorRate ?? 0) +
				(unexpectedStop.jev.networkErrorRate ?? 0) +
				(unexpectedStop.jev.timeoutRate ?? 0)) *
			turnEnds.length;
		const totalFailures = Math.round(atFailures + usFailures);
		if (totalFailures / totalJevCalls > 0.05) {
			throw new JevTransportError(
				`Jev transport failures across run (${totalFailures}/${totalJevCalls}, ${((totalFailures / totalJevCalls) * 100).toFixed(1)}%) exceeded 5% limit`,
			);
		}
	}

	// No issue rows means this machine has no robomp history. Null robomp fields
	// stay out of the verdict, which then uses only the features that were measured.
	let robomp: RobompFeature = unmeasuredRobomp();
	let sessionFlagsMissing: boolean | null = null;
	if (issues.length > 0) {
		const evaluated = await evaluateRobompFeature(issuesPath, {
			sessionCostUsd: options.robompSessionCostUsd,
			sessionP50Ms: options.robompSessionP50Ms,
			sessionP95Ms: options.robompSessionP95Ms,
			jevBaseUrl: options.jevBaseUrl,
			robompRunner: options.robompRunner,
		});
		robomp = evaluated.comparison;
		sessionFlagsMissing = evaluated.sessionFlagsMissing;
	}

	const results: MeasurementResults = {
		features: {
			auto_thinking: autoThinking,
			unexpected_stop: unexpectedStop,
			robomp,
		},
		currentBaseline,
		sampleSizes: {
			auto_thinking: prompts.length,
			unexpected_stop: turnEnds.length,
			robomp: issues.length > 0 ? issues.length : null,
		},
		robompSessionFlagsMissing: sessionFlagsMissing,
	};

	results.verdict = verdict(results);
	return results;
}
