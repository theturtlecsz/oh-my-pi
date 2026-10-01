import { expect, test } from "bun:test";
import { type ClientResponse, type Fetch, WORK_CONTRACT_SHA256, WorkClient, WorkError } from "../src/index";

const BASE = "http://127.0.0.1:54322";
const WORKSPACE = "00000000-0000-0000-0000-000000000001";
const TOKEN = "client-token";
const PROJECT = "00000000-0000-0000-0000-0000000000f1";
const MISSION = "00000000-0000-0000-0000-0000000000d1";
const REQUEST = "00000000-0000-0000-0000-0000000000b1";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

type Call = { url: string; init?: RequestInit };

const READ: ClientResponse = {
	outcome: "read",
	state: null,
	evidence: [],
	blockers: [],
	decisions: [],
	artifacts: [],
	operation: "project.list",
	contract: "client.omp.dev/v1",
	result: null,
	detail: null,
};

function capture(body: unknown, status = 200) {
	const calls: Call[] = [];
	const fetchImpl: Fetch = async (input, init) => {
		calls.push({ url: String(input), init });
		return Response.json(body, { status });
	};
	return { client: new WorkClient(BASE, WORKSPACE, () => TOKEN, fetchImpl), calls };
}

const GET_ROUTES: [string, (client: WorkClient) => Promise<ClientResponse>, string][] = [
	["clientProjects", c => c.clientProjects(), `${BASE}/v1/workspaces/${WORKSPACE}/client/projects`],
	[
		"clientProjectStatus",
		c => c.clientProjectStatus(PROJECT),
		`${BASE}/v1/workspaces/${WORKSPACE}/client/projects/${PROJECT}/status`,
	],
	[
		"clientProjectDecisions",
		c => c.clientProjectDecisions(PROJECT),
		`${BASE}/v1/workspaces/${WORKSPACE}/client/projects/${PROJECT}/decisions`,
	],
	["clientMission", c => c.clientMission(MISSION), `${BASE}/v1/workspaces/${WORKSPACE}/client/missions/${MISSION}`],
	["clientStopStatus", c => c.clientStopStatus(), `${BASE}/v1/workspaces/${WORKSPACE}/client/stop`],
];

for (const [name, call, url] of GET_ROUTES) {
	test(`${name} GETs the client contract route with bearer and contract headers`, async () => {
		const { client, calls } = capture(READ);
		expect(await call(client)).toEqual(READ);

		expect(calls).toHaveLength(1);
		expect(calls[0]?.url).toBe(url);
		expect(calls[0]?.init?.method).toBe("GET");
		expect(calls[0]?.init?.body).toBeUndefined();
		const headers = new Headers(calls[0]?.init?.headers);
		expect(headers.get("authorization")).toBe(`Bearer ${TOKEN}`);
		expect(headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
		expect(headers.get("x-omp-workspace-id")).toBe(WORKSPACE);
	});
}

test("clientProjectStatus and clientMission URL-encode the path id", async () => {
	const { client, calls } = capture(READ);
	await client.clientProjectStatus("a/b c");
	await client.clientMission("m/n?x");

	expect(calls[0]?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/client/projects/a%2Fb%20c/status`);
	expect(calls[1]?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/client/missions/m%2Fn%3Fx`);
});

test("clientEngageStop POSTs the client stop route with the exact request envelope", async () => {
	const { client, calls } = capture({ ...READ, outcome: "applied", operation: "stop.engage" });
	await client.clientEngageStop("operator hold", REQUEST);

	expect(calls).toHaveLength(1);
	expect(calls[0]?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/client/stop`);
	expect(calls[0]?.init?.method).toBe("POST");
	const headers = new Headers(calls[0]?.init?.headers);
	expect(headers.get("authorization")).toBe(`Bearer ${TOKEN}`);
	expect(headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
		request_id: REQUEST,
		payload: { reason: "operator hold" },
	});
});

test("clientEngageStop defaults to a fresh request id", async () => {
	const { client, calls } = capture({ ...READ, outcome: "applied", operation: "stop.engage" });
	await client.clientEngageStop("first hold");
	await client.clientEngageStop("second hold");

	const ids = calls.map(call => (JSON.parse(String(call.init?.body)) as { request_id: string }).request_id);
	expect(ids[0]).toMatch(UUID_RE);
	expect(ids[1]).toMatch(UUID_RE);
	expect(ids[0]).not.toBe(ids[1]);
});

test("a 409 agent_stop_engaged body surfaces its code and diagnostics", async () => {
	const { client } = capture(
		{
			error: {
				code: "agent_stop_engaged",
				request_id: REQUEST,
				correlation_id: "00000000-0000-0000-0000-0000000000c1",
				diagnostics: ["workspace stop is engaged"],
			},
		},
		409,
	);

	const error = await client.clientEngageStop("another hold").catch((caught: unknown) => caught);
	expect(error).toBeInstanceOf(WorkError);
	const workError = error as WorkError;
	expect(workError.code).toBe("agent_stop_engaged");
	expect(workError.status).toBe(409);
	expect(workError.requestId).toBe(REQUEST);
	expect(workError.diagnostics).toEqual(["workspace stop is engaged"]);
});
