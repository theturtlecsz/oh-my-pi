import type { ExperimentStatus, NumericMetricMap } from "@oh-my-pi/pi-tui/tools/autoresearch";
import { AutoresearchStorage, type SessionRow } from "./storage";

const MANAGED_FORMAT = "omp-managed-result.v1";

const MANAGED_KEYS = [
	"format",
	"trial_id",
	"campaign_id",
	"receipt_sha256",
	"candidate_digest",
	"verdict",
	"primary_metric",
	"direction",
	"metrics",
	"description",
] as const;

const VERDICT_STATUS = {
	qualified: "keep",
	candidate_failed: "crash",
	evaluator_failed: "checks_failed",
} as const;

const RECEIPT_SHA256 = /^[0-9a-fA-F]{64}$/;

export interface ParsedManagedResult {
	trialId: string;
	campaignId: string;
	receiptSha256: string;
	candidateDigest: string;
	status: ExperimentStatus;
	description: string;
	metric: number;
	metrics: NumericMetricMap;
}

export interface ParsedManagedResults {
	accepted: ParsedManagedResult[];
	rejected: number;
	incompatible: number;
}

export interface ImportManagedResultsCounts {
	imported: number;
	rejected: number;
	incompatible: number;
	skipped: number;
}

/** Read omp-managed-result.v1 JSONL. Blank lines are ignored. */
export function parseManagedResults(text: string, session: SessionRow): ParsedManagedResults {
	const accepted: ParsedManagedResult[] = [];
	let rejected = 0;
	let incompatible = 0;
	for (const rawLine of text.split(/\r?\n/)) {
		const line = rawLine.trim();
		if (line.length === 0) continue;
		const outcome = classifyLine(line, session);
		if (outcome === "rejected") rejected += 1;
		else if (outcome === "incompatible") incompatible += 1;
		else accepted.push(outcome);
	}
	return { accepted, rejected, incompatible };
}

/**
 * Log each accepted line as a flagged current-segment run.
 * A trial id already stored for the session, including one imported earlier in this call, is skipped.
 */
export function importManagedResults(
	storage: AutoresearchStorage,
	sessionId: number,
	text: string,
): ImportManagedResultsCounts {
	const session = storage.getSessionById(sessionId);
	if (!session) throw new Error(`Session ${sessionId} not found`);
	const parsed = parseManagedResults(text, session);
	const known = new Set(storage.listManagedTrialIds(sessionId));
	let imported = 0;
	let skipped = 0;
	const loggedAt = Date.now();
	for (const line of parsed.accepted) {
		if (known.has(line.trialId)) {
			skipped += 1;
			continue;
		}
		storage.insertImportedRun({
			sessionId,
			segment: session.currentSegment,
			status: line.status,
			description: line.description,
			metric: line.metric,
			metrics: line.metrics,
			trialId: line.trialId,
			receiptSha256: line.receiptSha256,
			loggedAt,
		});
		known.add(line.trialId);
		imported += 1;
	}
	return {
		imported,
		rejected: parsed.rejected,
		incompatible: parsed.incompatible,
		skipped,
	};
}

function classifyLine(line: string, session: SessionRow): ParsedManagedResult | "rejected" | "incompatible" {
	let parsed: unknown;
	try {
		parsed = JSON.parse(line) as unknown;
	} catch {
		return "rejected";
	}
	if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return "rejected";
	const record = parsed as Record<string, unknown>;
	if (!hasExactKeys(record)) return "rejected";
	if (record.format !== MANAGED_FORMAT) return "rejected";
	if (typeof record.trial_id !== "string" || record.trial_id.length === 0) return "rejected";
	if (typeof record.campaign_id !== "string") return "rejected";
	if (typeof record.candidate_digest !== "string") return "rejected";
	if (typeof record.description !== "string") return "rejected";
	if (typeof record.receipt_sha256 !== "string" || !RECEIPT_SHA256.test(record.receipt_sha256)) return "rejected";
	if (typeof record.verdict !== "string" || !Object.hasOwn(VERDICT_STATUS, record.verdict)) return "rejected";
	if (typeof record.primary_metric !== "string") return "rejected";
	if (typeof record.direction !== "string") return "rejected";
	const metrics = parseMetrics(record.metrics, record.primary_metric);
	if (!metrics) return "rejected";
	const metric = metrics[record.primary_metric];
	if (metric === undefined) return "rejected";
	if (record.primary_metric !== session.primaryMetric || record.direction !== session.direction) {
		return "incompatible";
	}
	const verdict = record.verdict as keyof typeof VERDICT_STATUS;
	return {
		trialId: record.trial_id,
		campaignId: record.campaign_id,
		receiptSha256: record.receipt_sha256,
		candidateDigest: record.candidate_digest,
		status: VERDICT_STATUS[verdict],
		description: record.description,
		metric,
		metrics,
	};
}

function hasExactKeys(record: Record<string, unknown>): boolean {
	const keys = Object.keys(record);
	if (keys.length !== MANAGED_KEYS.length) return false;
	return MANAGED_KEYS.every(key => Object.hasOwn(record, key));
}

function parseMetrics(value: unknown, primaryMetric: string): NumericMetricMap | null {
	if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
	const metrics: NumericMetricMap = {};
	for (const [key, metric] of Object.entries(value)) {
		if (key === "__proto__" || key === "constructor" || key === "prototype") return null;
		if (typeof metric !== "number" || !Number.isFinite(metric)) return null;
		metrics[key] = metric;
	}
	if (!Object.hasOwn(metrics, primaryMetric)) return null;
	return metrics;
}
