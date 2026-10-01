/**
 * OMP-425-s02: WebUI oversight snapshot over the OMP-425-s01 client reads.
 *
 * One `readOversight` reads the workspace stop state, every project, each
 * project's contract mission progress, and its pending decisions. There is no
 * cache: a fresh snapshot is a fresh set of client reads, so a restarted
 * daemon and a newly answered decision are reflected at once.
 */
import type { ClientResponse, UUID, WorkClient } from "./index";

/** `stop.status` stop state — `changed_at` renamed to `changedAt`. */
export type OversightStop = {
	stopped: boolean;
	reason: string | null;
	changedAt: string | null;
};

/** One pending decision, in the client contract's own field names. */
export type OversightDecision = {
	decision_id: UUID;
	project_id: UUID;
	mission_id: string | null;
	question: string;
	why_it_matters: string;
	options: string[];
	default_if_any: string | null;
	evidence_refs: string[];
};

/** One mission: a `mission_progress` row (s03) plus the derived standing. */
export type OversightMission = {
	mission_id: UUID;
	objective: string;
	status: string;
	revision: number;
	updated_at: string;
	lastTransitionAt: string | null;
	linkedWork: number;
	drawn: { usd: string; wall_clock_seconds: number };
};

export type OversightProject = {
	projectId: UUID;
	key: string | null;
	name: string;
	missions: OversightMission[];
	pendingDecisions: OversightDecision[];
};

export type OversightSnapshot = { stop: OversightStop; projects: OversightProject[] };

/** `engageOversightStop` result — the engage_stop receipt's stopped flag. */
export type OversightStopResult = { stopped: boolean };

function resultOf(response: ClientResponse): Record<string, unknown> {
	return response.result ?? {};
}

function asRecord(value: unknown): Record<string, unknown> | null {
	return typeof value === "object" && value !== null && !Array.isArray(value)
		? (value as Record<string, unknown>)
		: null;
}

function asArray(value: unknown): unknown[] {
	return Array.isArray(value) ? value : [];
}

function asString(value: unknown): string | null {
	return typeof value === "string" ? value : null;
}

function asNumber(value: unknown): number | null {
	return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function asStrings(value: unknown): string[] {
	return asArray(value).filter((item): item is string => typeof item === "string");
}

async function readStop(client: WorkClient): Promise<OversightStop> {
	const result = resultOf(await client.clientStopStatus());
	return {
		stopped: result.stopped === true,
		reason: asString(result.reason),
		changedAt: asString(result.changed_at),
	};
}

async function readMission(
	client: WorkClient,
	progress: Record<string, unknown>,
	missionId: string,
): Promise<OversightMission> {
	const mission = asRecord(resultOf(await client.clientMission(missionId)));
	const transitions = asArray(mission?.transitions);
	const links = asArray(mission?.links);
	const drawn = asRecord(mission?.drawn);
	const last = asRecord(transitions.at(-1));
	return {
		mission_id: missionId,
		objective: asString(progress.objective) ?? "",
		status: asString(progress.status) ?? "",
		revision: asNumber(progress.revision) ?? 0,
		updated_at: asString(progress.updated_at) ?? "",
		lastTransitionAt: last ? asString(last.at) : null,
		linkedWork: links.length,
		drawn: {
			usd: asString(drawn?.usd) ?? "0",
			wall_clock_seconds: asNumber(drawn?.wall_clock_seconds) ?? 0,
		},
	};
}

async function readProjectMissions(client: WorkClient, projectId: string): Promise<OversightMission[]> {
	const result = resultOf(await client.clientProjectStatus(projectId));
	const missions: OversightMission[] = [];
	for (const value of asArray(result.mission_progress)) {
		const progress = asRecord(value);
		const missionId = progress ? asString(progress.mission_id) : null;
		if (!progress || !missionId) continue;
		missions.push(await readMission(client, progress, missionId));
	}
	return missions;
}

/** A decision row plus its pending flag for the pending-only filter. */
function readDecision(value: unknown): { pending: boolean; decision: OversightDecision } | null {
	const row = asRecord(value);
	const decisionId = row ? asString(row.decision_id) : null;
	const projectId = row ? asString(row.project_id) : null;
	if (!row || !decisionId || !projectId) return null;
	return {
		pending: row.status === "pending",
		decision: {
			decision_id: decisionId,
			project_id: projectId,
			mission_id: asString(row.mission_id),
			question: asString(row.question) ?? "",
			why_it_matters: asString(row.why_it_matters) ?? "",
			options: asStrings(row.options),
			default_if_any: asString(row.default_if_any),
			evidence_refs: asStrings(row.evidence_refs),
		},
	};
}

/**
 * One oversight snapshot. Pending decisions are fetched through every
 * project's `project.decisions` read, deduplicated by `decision_id`, filtered
 * to `status === "pending"`, and grouped by each decision's own `project_id`.
 * A decision whose project is not in the project list is dropped.
 */
export async function readOversight(client: WorkClient): Promise<OversightSnapshot> {
	const [stop, projectResponse] = await Promise.all([readStop(client), client.clientProjects()]);

	const projects: OversightProject[] = [];
	const byId = new Map<string, OversightProject>();
	for (const value of asArray(resultOf(projectResponse).projects)) {
		const row = asRecord(value);
		const projectId = row ? asString(row.project_id) : null;
		if (!row || !projectId) continue;
		const project: OversightProject = {
			projectId,
			key: asString(row.key),
			name: asString(row.name) ?? projectId,
			missions: await readProjectMissions(client, projectId),
			pendingDecisions: [],
		};
		projects.push(project);
		byId.set(projectId, project);
	}

	const decisions = new Map<string, OversightDecision>();
	for (const project of projects) {
		const response = await client.clientProjectDecisions(project.projectId);
		for (const value of asArray(resultOf(response).decisions)) {
			const read = readDecision(value);
			if (!read || !read.pending || decisions.has(read.decision.decision_id)) continue;
			decisions.set(read.decision.decision_id, read.decision);
		}
	}
	for (const decision of decisions.values()) {
		byId.get(decision.project_id)?.pendingDecisions.push(decision);
	}

	return { stop, projects };
}

/** Engage the workspace agent stop through `stop.engage` alone. */
export async function engageOversightStop(client: WorkClient, reason: string): Promise<OversightStopResult> {
	const result = resultOf(await client.clientEngageStop(reason));
	return { stopped: result.stopped === true };
}
