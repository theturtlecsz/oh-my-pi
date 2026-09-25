import { expect, test } from "bun:test";
import { WORK_CONTRACT_SHA256, WorkClient, WorkError } from "../src/index";

const BASE = "http://127.0.0.1:54322";
const WORKSPACE = "00000000-0000-0000-0000-000000000001";
const TOKEN = "fk-read-token";

/** Captures the outgoing Request and answers with a fixed JSON body. */
function capture(body: unknown, status = 200) {
	let request: Request | undefined;
	const client = new WorkClient(
		BASE,
		WORKSPACE,
		() => TOKEN,
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json(body, { status });
		},
	);
	return { client, request: () => request };
}

test("revision encodes the key and accepts numeric and UUID selectors", async () => {
	const { client, request } = capture({
		revision_id: "00000000-0000-0000-0000-0000000000a1",
		work_id: "00000000-0000-0000-0000-0000000000b1",
		revision_number: 2,
		title: "t",
		description: "d",
		scope: "s",
		acceptance_criteria: [],
		content_sha256: "0".repeat(64),
		created_by: "owner",
		created_at: "2026-08-19T12:00:00+00:00",
	});

	await client.revision("OMP 1/a#b", 2);
	expect(request()?.url).toBe(`${BASE}/v1/work-items/OMP%201%2Fa%23b/revisions/2`);

	const revisionId = "00000000-0000-0000-0000-0000000000c1";
	await client.revision("OMP-1", revisionId);
	expect(request()?.url).toBe(`${BASE}/v1/work-items/OMP-1/revisions/${revisionId}`);
});

test("receipt reads by id and sends the authenticated headers", async () => {
	const { client, request } = capture({
		receipt_id: "00000000-0000-0000-0000-0000000000d1",
		work_id: "00000000-0000-0000-0000-0000000000b1",
		revision_id: "00000000-0000-0000-0000-0000000000a1",
		candidate_id: "00000000-0000-0000-0000-0000000000e1",
		kind: "verification",
		payload: {},
		payload_sha256: "1".repeat(64),
		issuer: "owner",
		issued_at: "2026-08-19T12:00:00+00:00",
	});

	const receiptId = "00000000-0000-0000-0000-0000000000d1";
	await client.receipt(receiptId);
	expect(request()?.url).toBe(`${BASE}/v1/receipts/${receiptId}`);
	expect(request()?.headers.get("authorization")).toBe(`Bearer ${TOKEN}`);
	expect(request()?.headers.get("x-omp-workspace-id")).toBe(WORKSPACE);
	expect(request()?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
});

test("workItems omits the cursor when absent and sets both after_* params when given", async () => {
	const { client, request } = capture({
		items: [],
		next_created_at: null,
		next_work_id: null,
	});

	await client.workItems();
	expect(request()?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/work-items`);

	await client.workItems({ limit: 25 });
	expect(request()?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/work-items?limit=25`);

	const createdAt = "2026-08-19T12:00:00+00:00";
	const workId = "00000000-0000-0000-0000-0000000000f1";
	await client.workItems({ after: { created_at: createdAt, work_id: workId } });
	expect(request()?.url).toBe(
		`${BASE}/v1/workspaces/${WORKSPACE}/work-items?after_created_at=${encodeURIComponent(createdAt)}&after_work_id=${workId}`,
	);

	await client.workItems({ after: { created_at: createdAt, work_id: workId }, limit: 5 });
	expect(request()?.url).toBe(
		`${BASE}/v1/workspaces/${WORKSPACE}/work-items?after_created_at=${encodeURIComponent(createdAt)}&after_work_id=${workId}&limit=5`,
	);
});

test("events omits absent params and encodes afterSequence/limit when given", async () => {
	const { client, request } = capture({
		events: [],
		watermark_sequence: 0,
		next_after_sequence: 0,
		has_more: false,
	});

	await client.events();
	expect(request()?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/events`);

	await client.events({ afterSequence: 7 });
	expect(request()?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/events?after_sequence=7`);

	await client.events({ limit: 50 });
	expect(request()?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/events?limit=50`);

	await client.events({ afterSequence: 7, limit: 50 });
	expect(request()?.url).toBe(`${BASE}/v1/workspaces/${WORKSPACE}/events?after_sequence=7&limit=50`);
});

test("a 403 forbidden body surfaces as a WorkError with code and status", async () => {
	const { client } = capture(
		{ error: { code: "forbidden", request_id: null, correlation_id: null, diagnostics: ["missing work.read"] } },
		403,
	);

	const err = await client.events().catch(e => e);
	expect(err).toBeInstanceOf(WorkError);
	expect((err as WorkError).code).toBe("forbidden");
	expect((err as WorkError).status).toBe(403);
});
