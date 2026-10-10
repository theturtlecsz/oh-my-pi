import type { ResearchObservation, ResearchProjection, ResearchView, UUID, WorkClient } from "./index";

export class ResearchViewError extends Error {
	constructor(readonly code: "unknown_campaign" | "version_stale") {
		super(code);
		this.name = "ResearchViewError";
	}
}

function record(value: unknown): Record<string, unknown> | null {
	return typeof value === "object" && value !== null && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: null;
}

function primaryMetric(payload: Record<string, unknown>): number | null {
	const primary = record(payload.metrics)?.primary;
	return typeof primary === "number" && Number.isFinite(primary) ? primary : null;
}

/** Service reads own campaign state; only current projection telemetry is used. */
export async function readResearchViews(client: WorkClient, workId: UUID, projection?: ResearchProjection) {
	const view = await client.research(workId);
	const nodes = view.trials.map(trial => {
		const payload = record("payload" in trial ? trial.payload : undefined);
		return {
			id: trial.trial_id,
			parentId: typeof payload?.parent_id === "string" ? payload.parent_id : null,
			metric: payload ? primaryMetric(payload) : null,
			cost: 0,
		};
	});
	const evidence: { claimId: string; status: string; sourceId: string; passage: string }[] = [];
	for (const observation of view.observations) {
		if (!Array.isArray(observation.payload.evidence)) continue;
		for (const entry of observation.payload.evidence) {
			const row = record(entry);
			if (
				row &&
				typeof row.claimId === "string" &&
				typeof row.status === "string" &&
				typeof row.sourceId === "string" &&
				typeof row.passage === "string"
			) {
				evidence.push({ claimId: row.claimId, status: row.status, sourceId: row.sourceId, passage: row.passage });
			}
		}
	}
	return {
		campaigns: view.campaigns.map(campaign => ({
			campaignId: campaign.campaign_id,
			status: campaign.state,
			objective: campaign.spec.objective,
			domain: campaign.domain,
			revisionId: campaign.revision_id,
			activeTrials: view.trials.filter(trial => trial.campaign_id === campaign.campaign_id && trial.state === "proposed")
				.length,
			completedTrials: view.observations.filter(
				observation => observation.campaign_id === campaign.campaign_id && observation.execution_status === "completed",
			).length,
		})),
		graph: {
			nodes,
			edges: nodes.flatMap(node => (node.parentId === null ? [] : [{ from: node.parentId, to: node.id }])),
		},
		evidence,
		usage:
			projection?.status === "current"
				? projection.usage
				: { computeSeconds: 0, modelTokens: 0, costUsd: "0", quotaExceeded: false },
		workerActivity: projection?.status === "current" ? projection.workerActivity : { workers: [], queueDepth: 0 },
		projectionStatus: projection?.status ?? null,
	};
}

export type StopResearchCampaignInput = {
	workId: UUID;
	campaignId: UUID;
	expectedRevision: string;
	reason: string;
	operationId: UUID;
};

export async function stopResearchCampaign(client: WorkClient, input: StopResearchCampaignInput) {
	const view = await client.research(input.workId);
	const campaign = view.campaigns.find(row => row.campaign_id === input.campaignId);
	if (!campaign) throw new ResearchViewError("unknown_campaign");
	if (campaign.revision_id !== input.expectedRevision) throw new ResearchViewError("version_stale");
	return client.researchCommand(input.operationId, {
		type: "cancel_research_campaign",
		payload: { campaign_id: input.campaignId, reason: input.reason, work_id: input.workId },
	});
}

export function openResearchEvidence(client: WorkClient, sourceId: UUID, projectId?: UUID) {
	return client.researchSource(sourceId, projectId);
}

export type VersionBoundAnnotation = {
	annotationId: string;
	targetId: string;
	expectedRevision: string;
	author: string;
	text: string;
	createdAt: string;
};

/** Local review notes never issue ledger commands. */
export class CandidateReviewSession {
	#annotations: VersionBoundAnnotation[] = [];

	addAnnotation(annotation: VersionBoundAnnotation, currentRevision: string): void {
		if (annotation.expectedRevision !== currentRevision) throw new ResearchViewError("version_stale");
		this.#annotations.push(structuredClone(annotation));
	}

	getAnnotations(targetId: string): VersionBoundAnnotation[] {
		return structuredClone(this.#annotations.filter(annotation => annotation.targetId === targetId));
	}
}

export function summarizeCandidates(view: ResearchView) {
	const latest = new Map<string, ResearchObservation>();
	for (const observation of view.observations) {
		if (typeof observation.trial_id === "string") latest.set(observation.trial_id, observation);
	}
	let passed = 0;
	let failed = 0;
	let bestMetric: number | null = null;
	for (const trial of view.trials) {
		const observation = latest.get(trial.trial_id);
		if (!observation) continue;
		const verdict = observation.payload.verdict;
		if (verdict === "PASS") {
			passed++;
			const metric = primaryMetric(observation.payload);
			if (metric !== null) bestMetric = bestMetric === null ? metric : Math.max(bestMetric, metric);
		} else if (
			verdict === "NEEDS_FIX" ||
			verdict === "BLOCKED" ||
			verdict === "candidate_failed" ||
			verdict === "evaluator_failed"
		) {
			failed++;
		}
	}
	return { passed, failed, bestMetric };
}

export const researchA11y = {
	keyboard: {
		tabStops: ["campaign", "graph", "evidence", "usage", "workers", "review"],
		arrows: "graph-nodes",
		shortcuts: { r: "summarizeCandidates", s: "stopResearchCampaign", e: "openResearchEvidence" },
	},
	touch: { minTargetPx: 44 },
} as const;
