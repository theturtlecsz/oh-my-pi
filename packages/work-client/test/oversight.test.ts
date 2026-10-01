import { expect, test } from "bun:test";
import type { ClientResponse } from "../src/index";
import { WorkClient, engageOversightStop, readOversight } from "../src/index";
import type { Fetch } from "../src/index";

const BASE = "http://127.0.0.1:54322";
const WORKSPACE = "00000000-0000-0000-0000-000000000001";
const TOKEN = "client-token";
const P1 = "00000000-0000-0000-0000-0000000000f1";
const P2 = "00000000-0000-0000-0000-0000000000f2";
const M1 = "00000000-0000-0000-0000-0000000000d1";
const M2 = "00000000-0000-0000-0000-0000000000d2";
const M3 = "00000000-0000-0000-0000-0000000000d3";
const D1 = "00000000-0000-0000-0000-0000000000c1";
const D2 = "00000000-0000-0000-0000-0000000000c2";
const D3 = "00000000-0000-0000-0000-0000000000c3";

const CLIENT = "client.omp.dev/v1" as const;

function read(result: Record<string, unknown>, operation: string): ClientResponse {
	return {
		outcome: "read",
		state: null,
		evidence: [],
		blockers: [],
		decisions: [],
		artifacts: [],
		operation,
		contract: CLIENT,
		result,
		detail: null,
	};
}

function applied(result: Record<string, unknown>, operation: string): ClientResponse {
	return { ...read(result, operation), outcome: "applied" };
}

function mission(
	status: string,
	transitionAt: string,
	linkedWork: number,
	usd: string,
	wallClock: number,
): Record<string, unknown> {
	return {
		transitions: [{ at: transitionAt }],
		links: Array.from({ length: linkedWork }, (_, index) => ({ work_id: `w${index}` })),
		drawn: { usd, wall_clock_seconds: wallClock },
		status,
	};
}

/** Stateful contract fixture: two projects, three missions, three decisions. */
function fixture() {
	const calls: { method: string; path: string }[] = [];
	const state = {
		answeredDecision: null as string | null,
		stopped: false,
		reason: null as string | null,
		changedAt: null as string | null,
	};
	const fetchImpl: Fetch = async (input, init) => {
		const url = new URL(String(input));
		const method = init?.method ?? "GET";
		calls.push({ method, path: url.pathname });

		if (url.pathname.endsWith("/client/stop")) {
			if (method === "POST") {
				const body = JSON.parse(String(init?.body)) as { payload: { reason: string } };
				state.stopped = true;
				state.reason = body.payload.reason;
				state.changedAt = "2026-10-01T00:00:00Z";
				return Response.json(applied({ type: "engage_stop", stopped: true, reason: body.payload.reason }, "stop.engage"));
			}
			return Response.json(
				read(
					{
						workspace_id: WORKSPACE,
						stopped: state.stopped,
						reason: state.reason,
						changed_at: state.changedAt,
						changed_by_actor_kind: null,
					},
					"stop.status",
				),
			);
		}
		if (url.pathname.endsWith("/client/projects")) {
			return Response.json(
				read(
					{
						workspace_id: WORKSPACE,
						projects: [
							{ project_id: P1, key: "alpha", name: "Alpha", kind: "engineering" },
							{ project_id: P2, key: "beta", name: "Beta", kind: "engineering" },
						],
					},
					"project.list",
				),
			);
		}
		if (url.pathname.endsWith(`/${P1}/status`)) {
			return Response.json(
				read(
					{
						project_id: P1,
						mission_progress: [
							{ mission_id: M1, objective: "ship oversight", status: "running", revision: 4, updated_at: "2026-09-30T10:00:00Z" },
							{ mission_id: M2, objective: "write docs", status: "completed", revision: 2, updated_at: "2026-09-29T10:00:00Z" },
						],
					},
					"project.status",
				),
			);
		}
		if (url.pathname.endsWith(`/${P2}/status`)) {
			return Response.json(
				read(
					{
						project_id: P2,
						mission_progress: [
							{ mission_id: M3, objective: "audit ledger", status: "blocked", revision: 1, updated_at: "2026-09-28T10:00:00Z" },
						],
					},
					"project.status",
				),
			);
		}
		if (url.pathname.endsWith(`/${P1}/decisions`)) {
			return Response.json(
				read(
					{
						decisions: [
							{
								decision_id: D1,
								project_id: P1,
								mission_id: M1,
								status: state.answeredDecision === D1 ? "answered" : "pending",
								question: "Adopt the snapshot?",
								why_it_matters: "The panel gates on it.",
								options: ["yes", "no"],
								default_if_any: "yes",
								evidence_refs: ["receipt:1"],
							},
							{
								decision_id: D3,
								project_id: P1,
								mission_id: M1,
								status: "answered",
								question: "Already answered",
								why_it_matters: "Must not surface.",
								options: ["yes"],
								default_if_any: null,
								evidence_refs: [],
							},
						],
					},
					"project.decisions",
				),
			);
		}
		if (url.pathname.endsWith(`/${P2}/decisions`)) {
			return Response.json(
				read(
					{
						decisions: [
							{
								decision_id: D2,
								project_id: P2,
								mission_id: M3,
								status: "pending",
								question: "Release the stop?",
								why_it_matters: "Beta is blocked.",
								options: ["hold", "release"],
								default_if_any: "hold",
								evidence_refs: ["receipt:2"],
							},
						],
					},
					"project.decisions",
				),
			);
		}
		if (url.pathname.endsWith(`/${M1}`)) {
			return Response.json(read(mission("running", "2026-09-30T09:00:00Z", 2, "12.50", 300), "mission.status"));
		}
		if (url.pathname.endsWith(`/${M2}`)) {
			return Response.json(read(mission("completed", "2026-09-29T09:00:00Z", 0, "0", 0), "mission.status"));
		}
		if (url.pathname.endsWith(`/${M3}`)) {
			return Response.json(read(mission("blocked", "2026-09-28T09:00:00Z", 1, "3.00", 60), "mission.status"));
		}
		return new Response("not found", { status: 404 });
	};
	return { calls, state, fetchImpl, client: new WorkClient(BASE, WORKSPACE, () => TOKEN, fetchImpl) };
}

test("readOversight groups missions and pending decisions per project", async () => {
	const { client } = fixture();
	const snapshot = await readOversight(client);

	expect(snapshot.stop).toEqual({ stopped: false, reason: null, changedAt: null });
	expect(snapshot.projects.map(project => [project.projectId, project.name])).toEqual([
		[P1, "Alpha"],
		[P2, "Beta"],
	]);

	const [alpha, beta] = snapshot.projects;
	expect(alpha?.missions.map(entry => entry.mission_id)).toEqual([M1, M2]);
	expect(beta?.missions.map(entry => entry.mission_id)).toEqual([M3]);

	expect(alpha?.missions[0]).toEqual({
		mission_id: M1,
		objective: "ship oversight",
		status: "running",
		revision: 4,
		updated_at: "2026-09-30T10:00:00Z",
		lastTransitionAt: "2026-09-30T09:00:00Z",
		linkedWork: 2,
		drawn: { usd: "12.50", wall_clock_seconds: 300 },
	});

	expect(alpha?.pendingDecisions).toEqual([
		{
			decision_id: D1,
			project_id: P1,
			mission_id: M1,
			question: "Adopt the snapshot?",
			why_it_matters: "The panel gates on it.",
			options: ["yes", "no"],
			default_if_any: "yes",
			evidence_refs: ["receipt:1"],
		},
	]);
	expect(beta?.pendingDecisions.map(decision => decision.decision_id)).toEqual([D2]);
});

test("an answered decision drops out of a fresh snapshot", async () => {
	const { fetchImpl, state } = fixture();
	const restarted = new WorkClient(BASE, WORKSPACE, () => TOKEN, fetchImpl);

	expect((await readOversight(restarted)).projects[0]?.pendingDecisions.map(d => d.decision_id)).toEqual([D1]);

	state.answeredDecision = D1;
	const snapshot = await readOversight(restarted);

	// A new client (daemon restart) sees the same projects and missions, but the
	// now-answered D1 is filtered to pending-only and disappears.
	expect(snapshot.projects.map(project => project.projectId)).toEqual([P1, P2]);
	expect(snapshot.projects[0]?.pendingDecisions).toEqual([]);
	expect(snapshot.projects[0]?.missions.map(entry => entry.mission_id)).toEqual([M1, M2]);
});

test("engageOversightStop posts one client/stop and leaves missions unchanged", async () => {
	const { client, calls } = fixture();
	const before = await readOversight(client);

	calls.length = 0;
	const stopped = await engageOversightStop(client, "operator hold");
	expect(stopped).toEqual({ stopped: true });

	const stopCalls = calls.filter(call => call.path.endsWith("/client/stop"));
	expect(stopCalls).toEqual([{ method: "POST", path: `/v1/workspaces/${WORKSPACE}/client/stop` }]);
	expect(calls.some(call => call.path.includes("/missions/"))).toBe(false);

	const after = await readOversight(client);
	expect(after.stop).toEqual({ stopped: true, reason: "operator hold", changedAt: "2026-10-01T00:00:00Z" });
	expect(after.projects).toEqual(before.projects);
});
