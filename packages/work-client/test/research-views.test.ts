import { describe, expect, test } from "bun:test";
import { ResearchProjection, ResearchProjectionError, type ResearchFact } from "../src/research-views";

const campaign: ResearchFact = {
	schema: "cpk5/v1",
	sourceIdentity: "research-program",
	sourceVersion: "research-program.v1",
	sequence: 1,
	factType: "research.campaign",
	timestamp: "2026-10-02T00:00:00Z",
	payload: {
		campaignId: "camp-1",
		status: "running",
		objective: "holdout",
		domain: "machine_learning",
		revisionId: "rev-1",
		activeTrials: 1,
		completedTrials: 0,
	},
};
function fact(sequence: number, body: Pick<ResearchFact, "factType" | "payload">): ResearchFact {
	return { ...campaign, ...body, sequence } as ResearchFact;
}
function error(code: string, action: () => void): void {
	try {
		action();
	} catch (caught) {
		expect(caught).toBeInstanceOf(ResearchProjectionError);
		expect((caught as ResearchProjectionError).code).toBe(code);
		return;
	}
	throw new Error(`Expected ${code}`);
}

describe("research projection reconnect", () => {
	test("campaign rows retain provenance; gaps and replay conflicts cannot overwrite them", () => {
		const projection = new ResearchProjection();
		projection.consume(campaign);
		expect(projection.campaignRows).toEqual([
			{
				id: "camp-1",
				sourceIdentity: "research-program",
				sourceVersion: "research-program.v1",
				sourceSequence: 1,
				projectionVersion: "research-views.v1",
				data: campaign.payload,
			},
		]);
		projection.consume({ ...campaign, payload: Object.fromEntries(Object.entries(campaign.payload).reverse()) });
		error("stale_cursor", () =>
			projection.consume({ ...campaign, payload: { ...campaign.payload, status: "done" } }),
		);
		error("gap_detected", () => projection.consume({ ...campaign, sequence: 3 }));
		expect(projection.status).toBe("stale");
		expect(projection.campaigns).toEqual([campaign.payload]);
		projection.rebuild([{ ...campaign, sequence: 2, payload: { ...campaign.payload, status: "done" } }, campaign]);
		expect(projection.status).toBe("current");
		expect(projection.campaignRows[0].sourceSequence).toBe(2);
		expect(projection.campaigns[0].status).toBe("done");
		expect(projection.banner).toBeNull();
	});

	test("reconnect deduplicates operation ids while advancing the cursor", () => {
		const projection = new ResearchProjection();
		const operation = {
			factType: "research.operation" as const,
			payload: { operationId: "op-1", name: "create_research_campaign" as const, campaignId: "camp-1" },
		};
		projection.consume(fact(1, operation));
		projection.consume(fact(2, operation));
		projection.consume(
			fact(3, { ...operation, payload: { ...operation.payload, name: "cancel_research_campaign" } }),
		);
		projection.consume({ ...campaign, sequence: 4 });
		expect(projection.operations).toEqual([operation.payload]);
		expect(projection.status).toBe("current");
		expect(projection.campaignRows[0].sourceSequence).toBe(4);
		projection.rebuild([campaign]);
		expect(projection.operations).toEqual([]);
	});

	test("offline consume preserves data and resume restores prior stale status", () => {
		const projection = new ResearchProjection();
		projection.consume(campaign);
		projection.setOffline(true);
		projection.setOffline(true);
		expect(projection.banner).toBe("offline");
		error("projection_offline", () => projection.consume({ ...campaign, sequence: 2 }));
		expect(projection.campaignRows[0].sourceSequence).toBe(1);
		projection.setOffline(false);
		expect(projection.status).toBe("current");
		error("gap_detected", () => projection.consume({ ...campaign, sequence: 3 }));
		projection.setOffline(true);
		projection.setOffline(false);
		expect(projection.status).toBe("stale");
		projection.rebuild([campaign]);
		expect(projection.status).toBe("current");
	});

	for (const field of ["schema", "sourceIdentity", "sourceVersion"]) {
		test(`incompatible ${field} marks stale without losing rows`, () => {
			const projection = new ResearchProjection();
			projection.consume(campaign);
			error("schema_incompatible", () => projection.consume({ ...campaign, [field]: "incompatible", sequence: 2 }));
			expect(projection.status).toBe("stale");
			expect(projection.campaigns).toEqual([campaign.payload]);
		});
	}

	test("malformed facts fail before advancing the cursor", () => {
		const projection = new ResearchProjection();
		error("unknown_fact", () => projection.consume({ ...campaign, factType: "research.unknown" }));
		for (const invalid of [
			{ ...campaign, sequence: 0 },
			{ ...campaign, timestamp: "yesterday" },
			{ ...campaign, timestamp: "2026-02-30T00:00:00Z" },
			{ ...campaign, payload: { ...campaign.payload, activeTrials: true } },
		])
			error("invalid_schema", () => projection.consume(invalid));
		projection.consume(campaign);
		expect(projection.campaigns).toEqual([campaign.payload]);
	});

	test("all projection mutation methods refuse writes", () => {
		const projection = new ResearchProjection();
		projection.consume(campaign);
		for (const mutate of [
			() => projection.createCampaign(),
			() => projection.updateState(),
			() => projection.stopCampaign(),
		]) {
			error("mutation_forbidden", mutate);
		}
		expect(projection.campaigns).toEqual([campaign.payload]);
	});

	test("rebuild with a missing sequence remains stale and preserves its contiguous prefix", () => {
		const projection = new ResearchProjection();
		error("gap_detected", () => projection.rebuild([{ ...campaign, sequence: 3 }, campaign]));
		expect(projection.status).toBe("stale");
		expect(projection.campaignRows[0].sourceSequence).toBe(1);
	});

	test("payload validation rejects malformed graph, evidence, usage, worker, and operation facts", () => {
		const projection = new ResearchProjection();
		for (const body of [
			{
				factType: "research.graph",
				payload: { nodes: [{ id: "n", parentId: null, metric: "bad", cost: 1 }], edges: [] },
			},
			{ factType: "research.evidence", payload: { claimId: "c", sourceId: "s", status: "invalid", passage: "p" } },
			{
				factType: "research.usage",
				payload: { computeSeconds: 1, modelTokens: 1, costUsd: 1, quotaExceeded: false },
			},
			{
				factType: "research.worker",
				payload: { workerId: "w", capabilities: [1], activeLeases: 1, status: "busy", queueDepth: 0 },
			},
			{ factType: "research.operation", payload: { operationId: "o", campaignId: "c", name: "delete_campaign" } },
		])
			error("invalid_schema", () => projection.consume({ ...campaign, ...body }));
		projection.consume(campaign);
		expect(projection.campaigns).toEqual([campaign.payload]);
	});

	test("views replace snapshots, key evidence pairs, and accumulate workers", () => {
		const projection = new ResearchProjection();
		let sequence = 0;
		const consume = (body: Pick<ResearchFact, "factType" | "payload">) => projection.consume(fact(++sequence, body));
		consume({
			factType: "research.graph",
			payload: { nodes: [{ id: "root", parentId: null, metric: null, cost: 1 }], edges: [] },
		});
		const graph = {
			nodes: [{ id: "child", parentId: "root", metric: 0.9, cost: 2 }],
			edges: [{ from: "root", to: "child" }],
		};
		consume({ factType: "research.graph", payload: graph });
		const evidence = { claimId: "claim-1", status: "supported" as const, sourceId: "source-1", passage: "finding" };
		consume({ factType: "research.evidence", payload: evidence });
		consume({ factType: "research.evidence", payload: { ...evidence, sourceId: "source-2" } });
		consume({ factType: "research.evidence", payload: { ...evidence, status: "contested" } });
		const usage = { computeSeconds: 2.5, modelTokens: 10, costUsd: "0.03", quotaExceeded: false };
		consume({ factType: "research.usage", payload: usage });
		consume({ factType: "research.usage", payload: { ...usage, quotaExceeded: true } });
		const worker = { workerId: "worker-1", capabilities: ["search"], activeLeases: 1, status: "busy", queueDepth: 3 };
		consume({ factType: "research.worker", payload: worker });
		consume({ factType: "research.worker", payload: { ...worker, workerId: "worker-2", queueDepth: 2 } });
		consume({ factType: "research.worker", payload: { ...worker, activeLeases: 0, queueDepth: 0 } });
		expect(projection.graph).toEqual(graph);
		expect(projection.evidence).toEqual([
			{ ...evidence, status: "contested" },
			{ ...evidence, sourceId: "source-2" },
		]);
		expect(projection.usage).toEqual({ ...usage, quotaExceeded: true });
		expect(projection.workerActivity).toEqual({
			workers: [
				{ ...worker, activeLeases: 0, queueDepth: 0 },
				{ ...worker, workerId: "worker-2", queueDepth: 2 },
			],
			queueDepth: 0,
		});
		projection.rebuild([campaign]);
		expect(projection.graph).toBeNull();
		expect(projection.evidence).toEqual([]);
		expect(projection.usage).toBeNull();
		expect(projection.workerActivity).toEqual({ workers: [], queueDepth: 0 });
	});
});
