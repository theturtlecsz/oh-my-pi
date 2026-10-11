import { expect, test } from "bun:test";
import {
	CandidateReviewSession,
	type CommandEnvelope,
	openResearchEvidence,
	readResearchViews,
	researchA11y,
	type ResearchObservation,
	ResearchProjection,
	type ResearchTrial,
	type ResearchView,
	ResearchViewError,
	stopResearchCampaign,
	summarizeCandidates,
	WorkClient,
} from "../src/index";

const workspaceId = "00000000-0000-0000-0000-000000000001";
const workId = "00000000-0000-0000-0000-000000000002";
const campaignId = "00000000-0000-0000-0000-000000000003";
const operationId = "00000000-0000-0000-0000-000000000004";
const sourceId = "00000000-0000-0000-0000-000000000005";

function trial(
	id: string,
	payload: Record<string, unknown> = {},
): ResearchTrial & { payload: Record<string, unknown> } {
	return {
		trial_id: id,
		workspace_id: workspaceId,
		campaign_id: campaignId,
		work_id: workId,
		decision_id: "decision-1",
		candidate_digest: "a".repeat(64),
		experiment_spec_sha256: "b".repeat(64),
		evaluator_sha256: "c".repeat(64),
		environment_sha256: "d".repeat(64),
		input_manifest_sha256: "e".repeat(64),
		policy_sha256: "f".repeat(64),
		state: "proposed",
		proposed_at: "2026-10-02T00:00:00Z",
		payload,
	};
}

function observation(trialId: string, payload: Record<string, unknown>): ResearchObservation {
	return {
		observation_id: `obs-${trialId}`,
		workspace_id: workspaceId,
		campaign_id: campaignId,
		trial_id: trialId,
		issuer_kind: "candidate_authored",
		source_ref: "fixture",
		execution_status: "completed",
		payload,
		payload_sha256: "0".repeat(64),
		observed_at: "2026-10-02T00:00:00Z",
		recorded_at: "2026-10-02T00:00:01Z",
	};
}

function researchView(): ResearchView {
	return {
		work_id: workId,
		campaigns: [
			{
				campaign_id: campaignId,
				workspace_id: workspaceId,
				work_id: workId,
				revision_id: "rev-1",
				domain: "machine_learning",
				spec: {
					objective: "compare candidates",
					evaluation_protocol_id: "protocol-1",
					evaluation_protocol_sha256: "0".repeat(64),
					resource_policy_ref: "policy-1",
					candidate_mapping_policy: "mapping-1",
				},
				spec_sha256: "1".repeat(64),
				state: "running",
				created_at: "2026-10-02T00:00:00Z",
			},
		],
		trials: [
			trial("pass", { metrics: { primary: 0.8 } }),
			trial("fix", { parent_id: "pass", metrics: { primary: "bad" } }),
		],
		observations: [
			observation("pass", {
				verdict: "PASS",
				metrics: { primary: 0.8 },
				evidence: [
					{ claimId: "claim-1", status: "supported", sourceId, passage: "measured result" },
					{ claimId: "invalid", status: "supported", sourceId, passage: 3 },
					null,
				],
			}),
			observation("fix", { verdict: "NEEDS_FIX", metrics: { primary: 0.99 } }),
		],
		deliverable_bindings: [],
		artifacts: [],
	};
}

function fakeService(view = researchView()) {
	const posts: { envelope: CommandEnvelope; headers: Headers }[] = [];
	const reads: string[] = [];
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		workspaceId,
		() => "client-token",
		async (url, init) => {
			if (init?.method === "POST") {
				expect(String(url)).toBe("http://127.0.0.1:54322/v1/commands");
				const envelope = JSON.parse(String(init.body)) as CommandEnvelope;
				posts.push({ envelope, headers: new Headers(init.headers) });
				view.campaigns[0].state = "cancelled";
				return Response.json({
					receipt: { operation_id: envelope.operation_id, request_id: envelope.request_id, state: "applied" },
					result: { type: "cancel_research_campaign", status: "applied", campaign: view.campaigns[0] },
				});
			}
			reads.push(String(url));
			if (String(url).includes("/research-sources/")) {
				return Response.json({ source: { source_id: sourceId }, content_base64: "bWVhc3VyZWQgcmVzdWx0" });
			}
			expect(String(url)).toBe(`http://127.0.0.1:54322/v1/work-items/${workId}/research`);
			return Response.json(view);
		},
	);
	return { client, posts, reads, view };
}

function projectionWithDraft(): ResearchProjection {
	const projection = new ResearchProjection();
	projection.consume({
		schema: "cpk5/v1",
		sourceIdentity: "research-program",
		sourceVersion: "research-program.v1",
		sequence: 1,
		timestamp: "2026-10-02T00:00:00Z",
		factType: "research.campaign",
		payload: {
			campaignId,
			status: "draft",
			objective: "old",
			domain: "machine_learning",
			revisionId: "rev-0",
			activeTrials: 0,
			completedTrials: 0,
		},
	});
	return projection;
}

test("stop uses fetched revision and authenticated command; subsequent reads show cancelled state", async () => {
	const { client, posts } = fakeService();
	const projection = projectionWithDraft();
	expect((await readResearchViews(client, workId, projection)).campaigns[0].status).toBe("running");
	const input = { workId, campaignId, expectedRevision: "rev-0", reason: "review stop", operationId };
	await expect(stopResearchCampaign(client, input)).rejects.toMatchObject({ code: "version_stale" });
	await expect(stopResearchCampaign(client, { ...input, campaignId: "missing" })).rejects.toMatchObject({
		code: "unknown_campaign",
	});
	expect(posts).toEqual([]);
	await stopResearchCampaign(client, { ...input, expectedRevision: "rev-1" });
	expect(posts).toHaveLength(1);
	const { envelope, headers } = posts[0];
	expect(envelope).toEqual({
		api_version: "work.omp.dev/v1",
		workspace_id: workspaceId,
		operation_id: operationId,
		request_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
		correlation_id: expect.stringMatching(/^[0-9a-f-]{36}$/),
		command: {
			type: "cancel_research_campaign",
			payload: { campaign_id: campaignId, reason: input.reason, work_id: workId },
		},
	});
	expect(envelope.request_id).not.toBe(envelope.correlation_id);
	expect(headers.get("Authorization")).toBe("Bearer client-token");
	expect((await client.research(workId)).campaigns[0].state).toBe("cancelled");
	expect((await readResearchViews(client, workId, projection)).campaigns[0].status).toBe("cancelled");
	await stopResearchCampaign(client, { ...input, expectedRevision: "rev-1", operationId: "next-operation" });
	expect(posts[1].envelope.request_id).not.toBe(envelope.request_id);
	expect(posts[1].envelope.correlation_id).not.toBe(envelope.correlation_id);
});

test("service rows determine counts, graph payloads, and evidence; malformed evidence is skipped", async () => {
	const { client, view } = fakeService();
	view.trials.push({ ...trial("other"), campaign_id: "other-campaign" }, { ...trial("archived"), state: "archived" });
	view.observations.push(
		{ ...observation("other", {}), campaign_id: "other-campaign" },
		{ ...observation("crashed", {}), execution_status: "crashed" },
	);
	const snapshot = await readResearchViews(client, workId);
	expect(snapshot.campaigns).toEqual([
		{
			campaignId,
			status: "running",
			objective: "compare candidates",
			domain: "machine_learning",
			revisionId: "rev-1",
			activeTrials: 2,
			completedTrials: 2,
		},
	]);
	expect(snapshot.graph.nodes).toEqual([
		{ id: "pass", parentId: null, metric: 0.8, cost: 0 },
		{ id: "fix", parentId: "pass", metric: null, cost: 0 },
		{ id: "other", parentId: null, metric: null, cost: 0 },
		{ id: "archived", parentId: null, metric: null, cost: 0 },
	]);
	expect(snapshot.graph.edges).toEqual([{ from: "pass", to: "fix" }]);
	expect(snapshot.evidence).toEqual([
		{ claimId: "claim-1", status: "supported", sourceId, passage: "measured result" },
	]);
	expect(snapshot.projectionStatus).toBeNull();
	expect(snapshot.usage).toEqual({ computeSeconds: 0, modelTokens: 0, costUsd: "0", quotaExceeded: false });
	expect(snapshot.workerActivity).toEqual({ workers: [], queueDepth: 0 });
});

test("only current projection exposes telemetry; offline and gap states clear it", async () => {
	const { client } = fakeService();
	const projection = projectionWithDraft();
	const base = {
		schema: "cpk5/v1",
		sourceIdentity: "research-program",
		sourceVersion: "research-program.v1",
		timestamp: "2026-10-02T00:00:00Z",
	};
	const usage = { computeSeconds: 12, modelTokens: 30, costUsd: "0.25", quotaExceeded: true };
	const worker = { workerId: "worker-1", capabilities: ["evaluate"], activeLeases: 1, status: "busy", queueDepth: 2 };
	projection.consume({ ...base, sequence: 2, factType: "research.usage", payload: usage });
	projection.consume({ ...base, sequence: 3, factType: "research.worker", payload: worker });
	const current = await readResearchViews(client, workId, projection);
	expect(current.usage).toEqual(usage);
	expect(current.workerActivity).toEqual({ workers: [worker], queueDepth: 2 });
	projection.setOffline(true);
	const offline = await readResearchViews(client, workId, projection);
	expect(offline.projectionStatus).toBe("offline");
	expect(offline.usage).toEqual({ computeSeconds: 0, modelTokens: 0, costUsd: "0", quotaExceeded: false });
	expect(offline.workerActivity).toEqual({ workers: [], queueDepth: 0 });
	projection.setOffline(false);
	expect(() => projection.consume({ ...base, sequence: 5, factType: "research.usage", payload: usage })).toThrow();
	const stale = await readResearchViews(client, workId, projection);
	expect(stale.projectionStatus).toBe("stale");
	expect(stale.usage).toEqual(offline.usage);
	expect(stale.workerActivity).toEqual(offline.workerActivity);
});

test("evidence opens source bytes with optional project scope", async () => {
	const { client, reads, posts } = fakeService();
	expect((await openResearchEvidence(client, sourceId)).content_base64).toBe("bWVhc3VyZWQgcmVzdWx0");
	expect(reads.at(-1)).toBe(`http://127.0.0.1:54322/v1/workspaces/${workspaceId}/research-sources/${sourceId}`);
	await openResearchEvidence(client, sourceId, "project-1");
	expect(reads.at(-1)).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${workspaceId}/research-sources/${sourceId}/projects/project-1`,
	);
	expect(posts).toEqual([]);
});

test("summaries use last trial observation and finite PASS metrics only", () => {
	const view = researchView();
	expect(summarizeCandidates(view)).toEqual({ passed: 1, failed: 1, bestMetric: 0.8 });
	for (const verdict of ["BLOCKED", "candidate_failed", "evaluator_failed", "PASS", "pending"]) {
		view.trials.push(trial(verdict));
		view.observations.push(observation(verdict, { verdict, metrics: { primary: Number.POSITIVE_INFINITY } }));
	}
	view.trials.push(trial("better"), trial("unobserved"));
	view.observations.push(observation("better", { verdict: "PASS", metrics: { primary: 0.9 } }));
	view.observations.push(observation("fix", { verdict: "PASS", metrics: { primary: 0.7 } }));
	view.observations.push(observation("absent-trial", { verdict: "PASS", metrics: { primary: 10 } }));
	expect(summarizeCandidates(view)).toEqual({ passed: 4, failed: 3, bestMetric: 0.9 });
	view.observations = [observation("fix", { verdict: "NEEDS_FIX" })];
	expect(summarizeCandidates(view)).toEqual({ passed: 0, failed: 1, bestMetric: null });
});

test("annotations reject stale revisions and remain local, isolated by target", () => {
	const { posts } = fakeService();
	const session = new CandidateReviewSession();
	const annotation = {
		annotationId: "note-1",
		targetId: "pass",
		expectedRevision: "rev-1",
		author: "reviewer",
		text: "verify holdout",
		createdAt: "2026-10-02T00:00:00Z",
	};
	session.addAnnotation(annotation, "rev-1");
	expect(session.getAnnotations("pass")).toEqual([annotation]);
	expect(session.getAnnotations("other")).toEqual([]);
	annotation.text = "changed";
	const rows = session.getAnnotations("pass");
	rows[0].text = "changed again";
	expect(session.getAnnotations("pass")[0].text).toBe("verify holdout");
	expect(() => session.addAnnotation(annotation, "rev-2")).toThrow(ResearchViewError);
	expect(() => session.addAnnotation(annotation, "rev-2")).toThrow("version_stale");
	expect(session.getAnnotations("pass")).toHaveLength(1);
	expect(posts).toEqual([]);
	expect(researchA11y.keyboard.shortcuts.s).toBe("stopResearchCampaign");
	expect(researchA11y.touch.minTargetPx).toBe(44);
});
