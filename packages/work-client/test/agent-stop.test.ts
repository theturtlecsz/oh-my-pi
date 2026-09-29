import { expect, test } from "bun:test";
import { type StopStatusView, WorkClient, WorkError } from "../src/index";

const BASE = "http://127.0.0.1:54322";
const WORKSPACE = "00000000-0000-0000-0000-000000000001";
const TOKEN = "stop-token";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

type Call = { url: string; init?: RequestInit };

function capture(body: unknown, status = 200) {
	const calls: Call[] = [];
	const client = new WorkClient(BASE, WORKSPACE, () => TOKEN, async (input, init) => {
		calls.push({ url: String(input), init });
		return Response.json(body, { status });
	});
	return { client, calls };
}

const APPLIED = {
	receipt: {
		operation_id: "00000000-0000-0000-0000-0000000000a1",
		request_id: "00000000-0000-0000-0000-0000000000b1",
		state: "applied",
		request_sha256: "0".repeat(64),
		result_sha256: "1".repeat(64),
		diagnostics: [],
	},
	result: { type: "engage_stop", stopped: true, reason: "operator hold" },
};

test("engageStop posts engage_stop with the reason and the bearer header", async () => {
	const { client, calls } = capture(APPLIED);
	const response = await client.engageStop("operator hold");

	expect(calls).toHaveLength(1);
	expect(calls[0]?.url).toBe(`${BASE}/v1/commands`);
	expect(calls[0]?.init?.method).toBe("POST");
	expect(new Headers(calls[0]?.init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);

	const body = JSON.parse(String(calls[0]?.init?.body)) as {
		api_version: string;
		workspace_id: string;
		operation_id: string;
		request_id: string;
		correlation_id: string;
		command: { type: string; payload: { reason: string } };
	};
	expect(body.api_version).toBe("work.omp.dev/v1");
	expect(body.workspace_id).toBe(WORKSPACE);
	expect(body.command).toEqual({ type: "engage_stop", payload: { reason: "operator hold" } });
	expect(body.operation_id).toMatch(UUID_RE);
	expect(body.request_id).toMatch(UUID_RE);
	expect(body.correlation_id).toMatch(UUID_RE);
	expect(new Set([body.operation_id, body.request_id, body.correlation_id]).size).toBe(3);

	expect(response.result.type).toBe("engage_stop");
	if (response.result.type !== "engage_stop") throw new Error("expected engage_stop");
	expect(response.result.stopped).toBe(true);
	expect(response.result.reason).toBe("operator hold");
});

test("two engageStop calls use distinct operation ids", async () => {
	const { client, calls } = capture(APPLIED);
	await client.engageStop("first hold");
	await client.engageStop("second hold");

	const ids = calls.map(call => (JSON.parse(String(call.init?.body)) as { operation_id: string }).operation_id);
	expect(ids).toHaveLength(2);
	expect(ids[0]).toMatch(UUID_RE);
	expect(ids[1]).toMatch(UUID_RE);
	expect(ids[0]).not.toBe(ids[1]);
});

test("stopStatus GETs this workspace stop route and returns the view", async () => {
	const view: StopStatusView = {
		workspace_id: WORKSPACE,
		stopped: true,
		reason: "operator hold",
		changed_at: "2026-09-29T12:00:00+00:00",
		changed_by_actor_kind: "owner",
	};
	const { client, calls } = capture(view);

	expect(await client.stopStatus()).toEqual(view);
	expect(calls).toHaveLength(1);
	expect(calls[0]?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/stop`);
	expect(calls[0]?.init?.method).toBe("GET");
	expect(calls[0]?.init?.body).toBeUndefined();
	expect(new Headers(calls[0]?.init?.headers).get("authorization")).toBe(`Bearer ${TOKEN}`);
});

test("a 403 from releaseStop rejects with WorkError code forbidden", async () => {
	const { client, calls } = capture(
		{
			error: {
				code: "forbidden",
				request_id: null,
				correlation_id: null,
				diagnostics: ["release_stop is owner-only"],
			},
		},
		403,
	);

	const error = await client.releaseStop("resume work").catch((caught: unknown) => caught);
	expect(error).toBeInstanceOf(WorkError);
	const workError = error as WorkError;
	expect(workError.code).toBe("forbidden");
	expect(workError.status).toBe(403);
	expect(workError.diagnostics).toEqual(["release_stop is owner-only"]);

	const body = JSON.parse(String(calls[0]?.init?.body)) as { command: { type: string; payload: { reason: string } } };
	expect(calls[0]?.url).toBe(`${BASE}/v1/commands`);
	expect(body.command).toEqual({ type: "release_stop", payload: { reason: "resume work" } });
});

test("a 409 agent_stop_engaged body surfaces its code and diagnostics", async () => {
	const requestId = "00000000-0000-0000-0000-0000000000b1";
	const { client } = capture(
		{
			error: {
				code: "agent_stop_engaged",
				request_id: requestId,
				correlation_id: "00000000-0000-0000-0000-0000000000c1",
				diagnostics: ["workspace stop is engaged"],
			},
		},
		409,
	);

	const error = await client.engageStop("another hold").catch((caught: unknown) => caught);
	expect(error).toBeInstanceOf(WorkError);
	const workError = error as WorkError;
	expect(workError.code).toBe("agent_stop_engaged");
	expect(workError.status).toBe(409);
	expect(workError.requestId).toBe(requestId);
	expect(workError.diagnostics).toEqual(["workspace stop is engaged"]);
});
