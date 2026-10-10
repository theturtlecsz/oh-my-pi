import { canonicalJson } from "./index";

export type ResearchCampaign = {
	campaignId: string;
	status: string;
	objective: string;
	domain: string;
	revisionId: string;
	activeTrials: number;
	completedTrials: number;
};
export type ResearchGraph = {
	nodes: { id: string; parentId: string | null; metric: number | null; cost: number }[];
	edges: { from: string; to: string }[];
};
export type ResearchEvidence = {
	claimId: string;
	status: "supported" | "contested" | "unresolved";
	sourceId: string;
	passage: string;
};
export type ResearchUsage = {
	computeSeconds: number;
	modelTokens: number;
	costUsd: string;
	quotaExceeded: boolean;
};
export type ResearchWorker = {
	workerId: string;
	capabilities: string[];
	activeLeases: number;
	status: string;
	queueDepth: number;
};
export type ResearchOperation = {
	operationId: string;
	name: "create_research_campaign" | "cancel_research_campaign" | "conclude_research_campaign";
	campaignId: string;
};
type Payloads = {
	"research.campaign": ResearchCampaign;
	"research.graph": ResearchGraph;
	"research.evidence": ResearchEvidence;
	"research.usage": ResearchUsage;
	"research.worker": ResearchWorker;
	"research.operation": ResearchOperation;
};
export type ResearchFact = {
	[K in keyof Payloads]: {
		schema: "cpk5/v1";
		sourceIdentity: "research-program";
		sourceVersion: "research-program.v1";
		sequence: number;
		factType: K;
		timestamp: string;
		payload: Payloads[K];
	};
}[keyof Payloads];
export type ResearchRow<T> = {
	id: string;
	sourceIdentity: "research-program";
	sourceVersion: "research-program.v1";
	sourceSequence: number;
	projectionVersion: "research-views.v1";
	data: T;
};
export type ResearchProjectionStatus = "current" | "stale" | "offline";

export class ResearchProjectionError extends Error {
	constructor(readonly code: string) {
		super(code);
		this.name = "ResearchProjectionError";
	}
}

type Check = (value: unknown) => boolean;
const string: Check = value => typeof value === "string";
const number: Check = value => typeof value === "number" && Number.isFinite(value);
const count: Check = value => typeof value === "number" && Number.isInteger(value) && value >= 0;
const record = (value: unknown): value is Record<string, unknown> =>
	typeof value === "object" && value !== null && !Array.isArray(value);
const fields = (value: unknown, shape: Record<string, Check>): boolean =>
	record(value) && Object.entries(shape).every(([key, check]) => key in value && check(value[key]));
const array =
	(check: Check): Check =>
	value =>
		Array.isArray(value) && value.every(check);
const nullable =
	(check: Check): Check =>
	value =>
		value === null || check(value);
const timestamp: Check = value =>
	typeof value === "string" &&
	/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value) &&
	Number(value.slice(0, 4)) > 0 &&
	Number(value.slice(11, 13)) < 24 &&
	Number.isFinite(Date.parse(value)) &&
	new Date(`${value.slice(0, 10)}T00:00:00Z`).toISOString().startsWith(value.slice(0, 10));
const payloads: Record<keyof Payloads, Record<string, Check>> = {
	"research.campaign": {
		campaignId: string,
		status: string,
		objective: string,
		domain: string,
		revisionId: string,
		activeTrials: count,
		completedTrials: count,
	},
	"research.graph": {
		nodes: array(value =>
			fields(value, { id: string, parentId: nullable(string), metric: nullable(number), cost: number }),
		),
		edges: array(value => fields(value, { from: string, to: string })),
	},
	"research.evidence": {
		claimId: string,
		status: value => ["supported", "contested", "unresolved"].includes(value as string),
		sourceId: string,
		passage: string,
	},
	"research.usage": {
		computeSeconds: number,
		modelTokens: count,
		costUsd: string,
		quotaExceeded: value => typeof value === "boolean",
	},
	"research.worker": {
		workerId: string,
		capabilities: array(string),
		activeLeases: count,
		status: string,
		queueDepth: count,
	},
	"research.operation": {
		operationId: string,
		campaignId: string,
		name: value =>
			["create_research_campaign", "cancel_research_campaign", "conclude_research_campaign"].includes(
				value as string,
			),
	},
};

function validateFact(value: unknown): asserts value is ResearchFact {
	if (
		!record(value) ||
		!count(value.sequence) ||
		value.sequence === 0 ||
		!timestamp(value.timestamp) ||
		!record(value.payload)
	) {
		throw new ResearchProjectionError("invalid_schema");
	}
	if (typeof value.factType !== "string" || !Object.hasOwn(payloads, value.factType)) {
		throw new ResearchProjectionError("unknown_fact");
	}
	if (!fields(value.payload, payloads[value.factType as keyof Payloads])) {
		throw new ResearchProjectionError("invalid_schema");
	}
}

/** Read-only projection of the contiguous research-program fact stream. */
export class ResearchProjection {
	readonly projectionVersion = "research-views.v1";
	#status: ResearchProjectionStatus = "current";
	#beforeOffline: Exclude<ResearchProjectionStatus, "offline"> = "current";
	#lastSequence = 0;
	#sequences = new Map<number, string>();
	#campaigns = new Map<string, ResearchRow<ResearchCampaign>>();
	#graph: ResearchRow<ResearchGraph> | null = null;
	#evidence = new Map<string, ResearchRow<ResearchEvidence>>();
	#usage: ResearchRow<ResearchUsage> | null = null;
	#workers = new Map<string, ResearchRow<ResearchWorker>>();
	#queueDepth = 0;
	#operations = new Map<string, ResearchRow<ResearchOperation>>();

	get status() {
		return this.#status;
	}
	get banner() {
		return this.#status === "current" ? null : this.#status;
	}
	get campaignRows() {
		return structuredClone([...this.#campaigns.values()]);
	}
	get campaigns() {
		return this.campaignRows.map(row => row.data);
	}
	get graph() {
		return structuredClone(this.#graph?.data ?? null);
	}
	get evidence() {
		return structuredClone([...this.#evidence.values()].map(row => row.data));
	}
	get usage() {
		return structuredClone(this.#usage?.data ?? null);
	}
	get workerActivity() {
		return {
			workers: structuredClone([...this.#workers.values()].map(row => row.data)),
			queueDepth: this.#queueDepth,
		};
	}
	get operations() {
		return structuredClone([...this.#operations.values()].map(row => row.data));
	}

	setOffline(offline: boolean): void {
		if (offline && this.#status !== "offline") {
			this.#beforeOffline = this.#status;
			this.#status = "offline";
		} else if (!offline && this.#status === "offline") {
			this.#status = this.#beforeOffline;
		}
	}

	consume(value: unknown): void {
		if (this.#status === "offline") throw new ResearchProjectionError("projection_offline");
		if (
			!record(value) ||
			value.schema !== "cpk5/v1" ||
			value.sourceIdentity !== "research-program" ||
			value.sourceVersion !== "research-program.v1"
		) {
			this.#status = "stale";
			throw new ResearchProjectionError("schema_incompatible");
		}
		validateFact(value);
		const fingerprint = canonicalJson(value.payload);
		const previous = this.#sequences.get(value.sequence);
		if (previous !== undefined) {
			if (previous === fingerprint) return;
			throw new ResearchProjectionError("stale_cursor");
		}
		if (value.sequence > this.#lastSequence + 1) {
			this.#status = "stale";
			throw new ResearchProjectionError("gap_detected");
		}
		const fact = structuredClone(value);
		switch (fact.factType) {
			case "research.campaign":
				this.#campaigns.set(fact.payload.campaignId, this.#row(fact.payload.campaignId, fact, fact.payload));
				break;
			case "research.graph":
				this.#graph = this.#row("graph", fact, fact.payload);
				break;
			case "research.evidence": {
				const id = canonicalJson([fact.payload.claimId, fact.payload.sourceId]);
				this.#evidence.set(id, this.#row(id, fact, fact.payload));
				break;
			}
			case "research.usage":
				this.#usage = this.#row("usage", fact, fact.payload);
				break;
			case "research.worker":
				this.#workers.set(fact.payload.workerId, this.#row(fact.payload.workerId, fact, fact.payload));
				this.#queueDepth = fact.payload.queueDepth;
				break;
			case "research.operation":
				if (!this.#operations.has(fact.payload.operationId)) {
					this.#operations.set(fact.payload.operationId, this.#row(fact.payload.operationId, fact, fact.payload));
				}
				break;
		}
		this.#sequences.set(fact.sequence, fingerprint);
		this.#lastSequence = fact.sequence;
	}

	#row<T>(id: string, fact: ResearchFact, data: T): ResearchRow<T> {
		return {
			id,
			sourceIdentity: fact.sourceIdentity,
			sourceVersion: fact.sourceVersion,
			sourceSequence: fact.sequence,
			projectionVersion: this.projectionVersion,
			data,
		};
	}

	rebuild(facts: readonly ResearchFact[]): void {
		this.#campaigns.clear();
		this.#evidence.clear();
		this.#workers.clear();
		this.#operations.clear();
		this.#graph = null;
		this.#usage = null;
		this.#queueDepth = 0;
		this.#sequences.clear();
		this.#lastSequence = 0;
		this.#status = "current";
		this.#beforeOffline = "current";
		for (const fact of [...facts].sort((a, b) => a.sequence - b.sequence)) this.consume(fact);
	}

	createCampaign(..._args: unknown[]): never {
		throw new ResearchProjectionError("mutation_forbidden");
	}
	updateState(..._args: unknown[]): never {
		throw new ResearchProjectionError("mutation_forbidden");
	}
	stopCampaign(..._args: unknown[]): never {
		throw new ResearchProjectionError("mutation_forbidden");
	}
}
