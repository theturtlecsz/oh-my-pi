/**
 * OMP-395: a 5xx from the service must never release the pending claim as
 * "did not apply". Every POST failure at or above 500 reconciles against the
 * stored operation row instead, exactly as the s02 503 body demands.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { afterEach, describe, expect, test } from "bun:test";
import { type CommandEnvelope, payloadHash, type StoredOperation } from "@oh-my-pi/pi-work-client";
import type { NowRef, PlanStamp } from "../extensions/workflow/backend";
import { createWorkBackend } from "../extensions/workflow/work";

const tempDirs: string[] = [];

async function makeTempDir(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(import.meta.dir, ".omp-395-reconcile-"));
	tempDirs.push(dir);
	return dir;
}

afterEach(async () => {
	while (tempDirs.length > 0) {
		const dir = tempDirs.pop();
		if (dir) {
			try {
				await fs.rm(dir, { recursive: true, force: true });
			} catch {
				// tolerate failed setup or cleanup
			}
		}
	}
});

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000000";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";
const WORK_ID = "00000000-0000-7000-8000-000000000010";
const REVISION_ID = "00000000-0000-7000-8000-000000000020";
const KEY = "OMP-1";
const PLAN_BYTES = "## Approach\n- Step 1\n\n## Verification\n- Check 1\n";

const workItemView = () => ({
	work_id: WORK_ID,
	workspace_id: WORKSPACE_ID,
	alias: { work_id: WORK_ID, key: KEY, primary: true, origin: "local" },
	state: "IN_PROGRESS",
	revision: {
		revision_id: REVISION_ID,
		work_id: WORK_ID,
		revision_number: 1,
		title: "Work title",
		description: "Work description",
		scope: "scope",
		acceptance_criteria: ["Criteria 1"],
		content_sha256: "0".repeat(64),
		created_by: "test",
		created_at: new Date().toISOString(),
	},
	candidate: null,
	project_id: null,
	archived: false,
});

const stamp: PlanStamp = {
	hash: Bun.SHA256.hash(PLAN_BYTES, "hex"),
	body: PLAN_BYTES,
	title: "Plan title",
	planFilePath: "/fake/plan.md",
	approach: ["Step 1"],
	verification: ["Check 1"],
	paths: ["main.ts"],
};
const nowRef: NowRef = { id: WORK_ID, key: KEY, title: "Work title" };

/** The exact s02 post-commit fault body: 503, `unavailable`, outcome unknown. */
function s02Body(env: CommandEnvelope): Response {
	return Response.json(
		{
			error: {
				code: "unavailable",
				request_id: env.request_id,
				correlation_id: env.correlation_id,
				diagnostics: ["ValueError: simulated post-commit error", "outcome unknown, reconcile by operation_id"],
			},
		},
		{ status: 503 },
	);
}

/** The command the POST committed, echoed by the operation read. */
function storedFor(env: CommandEnvelope): StoredOperation {
	return {
		receipt: {
			operation_id: env.operation_id,
			request_id: env.request_id,
			state: "applied",
			request_sha256: payloadHash({
				api_version: env.api_version,
				workspace_id: env.workspace_id,
				command: env.command,
			}),
			result_sha256: "0".repeat(64),
			diagnostics: [],
		},
		command_type: env.command.type,
		request_id: env.request_id,
		correlation_id: env.correlation_id,
		result: {
			type: "append_evidence",
			receipt: {
				receipt_id: "00000000-0000-7000-8000-000000000099",
				work_id: WORK_ID,
				revision_id: REVISION_ID,
				candidate_id: "00000000-0000-7000-8000-000000000030",
				kind: "plan",
				payload: {},
				payload_sha256: "0".repeat(64),
				issuer: "test",
				issued_at: new Date().toISOString(),
			},
		},
	};
}

function makeMock(postResponse: (env: CommandEnvelope) => Response) {
	let postCount = 0;
	let operationGetCount = 0;
	let stored: StoredOperation | undefined;
	const postedOperationIds: string[] = [];
	const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
		const url = String(input);
		const method = init?.method ?? (input instanceof Request ? input.method : "GET");
		if (method === "POST" || url.endsWith("/v1/commands")) {
			postCount++;
			const env = JSON.parse(String(init?.body)) as CommandEnvelope;
			postedOperationIds.push(env.operation_id);
			stored = storedFor(env);
			return postResponse(env);
		}
		if (url.includes("/v1/operations/")) {
			operationGetCount++;
			return Response.json(stored ?? {});
		}
		if (url.includes(`/v1/work-items/${KEY}`)) {
			return Response.json(workItemView());
		}
		return new Response("not found", { status: 404 });
	};
	return { mockFetch, counts: () => ({ postCount, operationGetCount, postedOperationIds }) };
}

async function readOnlyClaim(dir: string) {
	const files = (await fs.readdir(dir)).filter(f => f.endsWith(".json"));
	expect(files.length).toBe(1);
	const claimPath = `${dir}/${files[0]}`;
	return {
		path: claimPath,
		record: JSON.parse(await Bun.file(claimPath).text()) as { result?: unknown; envelope: CommandEnvelope },
	};
}

describe("OMP-395 post-commit reconcile", () => {
	const cases: Array<{ name: string; respond: (env: CommandEnvelope) => Response }> = [
		{ name: "503 with the s02 unavailable body", respond: s02Body },
		{
			name: "500 with a non-JSON text body",
			respond: () => new Response("internal server error", { status: 500, headers: { "Content-Type": "text/plain" } }),
		},
		{
			name: "500 with a JSON body whose code is invalid_request",
			respond: () =>
				Response.json(
					{ error: { code: "invalid_request", request_id: null, correlation_id: null, diagnostics: ["boom"] } },
					{ status: 500 },
				),
		},
	];

	for (const { name, respond } of cases) {
		test(`${name}: reconciles the stored result, holds the claim, and reuses it with no second POST`, async () => {
			const tempDir = await makeTempDir();
			const { mockFetch, counts } = makeMock(respond);
			const backend = createWorkBackend(
				{ baseUrl: "http://127.0.0.1:9999", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			const first = await backend.stampPlan(nowRef, stamp);
			expect(first.plannedCandidateId).toBeDefined();
			expect(counts().postCount).toBe(1);
			// One POST that failed, one operation read that found it applied.
			expect(counts().operationGetCount).toBe(1);

			const claim = await readOnlyClaim(tempDir);
			expect(claim.record.result).toBeDefined();
			expect((claim.record.result as { type: string }).type).toBe("append_evidence");
			const operationId = claim.record.envelope.operation_id;

			// The identical intent is served from the held claim — no new POST,
			// same operation id, same result.
			const second = await backend.stampPlan(nowRef, stamp);
			expect(second.plannedCandidateId).toBe(first.plannedCandidateId);
			expect(counts().postCount).toBe(1);
			expect(counts().operationGetCount).toBe(1);
			expect(counts().postedOperationIds).toEqual([operationId]);
			expect((claim.record.result as { type: string }).type).toBe("append_evidence");
		});
	}
});
