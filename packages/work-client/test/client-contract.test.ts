import { expect, test } from "bun:test";
import { type DraftMissionIntakePayload, type Fetch, WORK_CONTRACT_SHA256, WorkClient } from "../src/index";

const WORKSPACE = "00000000-0000-0000-0000-000000000001";
const REQUEST = "00000000-0000-0000-0000-0000000000b1";

const payload: DraftMissionIntakePayload = {
	mission_id: "00000000-0000-0000-0000-0000000000d1",
	intake: {
		archetype: "small_code_change",
		source: { text: "blueprint", sha256: "ab".repeat(32), spans: [] },
		goal: { id: "goal", statement: "title" },
		acceptance_criteria: [{ id: "ac-1", statement: "the gate passes", observable_outcome: "the gate passes" }],
	},
	scope: {
		project_id: "00000000-0000-0000-0000-0000000000f1",
		risk_policy: "default",
		approval_policy: "default",
		effort_policy: "default",
		kind: "engineering.execute",
	},
};

const responseBody = {
	outcome: "applied",
	state: null,
	evidence: [],
	blockers: [],
	decisions: [],
	artifacts: [],
	operation: "mission.intake",
	contract: "client.omp.dev/v1",
	result: { type: "draft_mission_intake", mission_id: payload.mission_id, outcome: "proceeded", questions: [] },
	detail: null,
};

function client(fetchImpl: Fetch): WorkClient {
	return new WorkClient("http://127.0.0.1:54322", WORKSPACE, () => "token", fetchImpl);
}

test("missionIntake posts the client contract path, header, and body", async () => {
	let request: Request | undefined;
	const result = await client(async (input, init) => {
		request = new Request(String(input), init);
		return Response.json(responseBody);
	}).missionIntake(REQUEST, payload);
	expect(request?.method).toBe("POST");
	expect(request?.url).toBe(`http://127.0.0.1:54322/v1/workspaces/${WORKSPACE}/client/mission-intake`);
	expect(request?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(request?.headers.get("x-omp-workspace-id")).toBe(WORKSPACE);
	expect(await request?.json()).toEqual({ request_id: REQUEST, payload });
	expect(result.operation).toBe("mission.intake");
	expect(result.contract).toBe("client.omp.dev/v1");
	expect(result.result).toMatchObject({ outcome: "proceeded" });
});

test("missionIntake sends detail only when asked", async () => {
	let url = "";
	const fetchImpl: Fetch = async input => {
		url = String(input);
		return Response.json(responseBody);
	};
	await client(fetchImpl).missionIntake(REQUEST, payload, { detail: true });
	expect(url).toBe(`http://127.0.0.1:54322/v1/workspaces/${WORKSPACE}/client/mission-intake?detail=true`);
});
