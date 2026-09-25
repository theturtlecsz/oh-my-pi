import * as fs from "node:fs/promises";
import * as path from "node:path";
import { afterEach, describe, expect, test } from "bun:test";
import { type CommandEnvelope, payloadHash, type StoredOperation, WorkError } from "@oh-my-pi/pi-work-client";
import type { NowRef, PlanStamp } from "../extensions/workflow/backend";
import { claimPendingOp, intentFingerprint } from "../extensions/workflow/pending-ops";
import {
	createWorkBackend,
	matchStoredOperation,
	OPERATION_POLL_ATTEMPTS,
	OPERATION_POLL_INTERVAL_MS,
} from "../extensions/workflow/work";

const tempDirs: string[] = [];

async function makeTempDir(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(import.meta.dir, ".omp-262-rec-"));
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

describe("OMP-262 operation reconciliation", () => {
	test("exported poll budget constants exist", () => {
		expect(OPERATION_POLL_ATTEMPTS).toBe(10);
		expect(OPERATION_POLL_INTERVAL_MS).toBe(100);
	});

	test("POST 409 execution_grant_inactive: rejects with that code, no claim .json, zero GETs", async () => {
		const tempDir = await makeTempDir();
		let postCount = 0;
		let getCount = 0;

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const method = init?.method ?? (input instanceof Request ? input.method : "GET");
			if (method === "POST") {
				postCount++;
				return new Response(
					JSON.stringify({
						error: {
							code: "execution_grant_inactive",
							request_id: null,
							correlation_id: null,
							diagnostics: ["execution grant is inactive"],
						},
					}),
					{ status: 409, headers: { "Content-Type": "application/json" } },
				);
			}
			if (method === "GET") {
				getCount++;
				return new Response("not found", { status: 404 });
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{
				baseUrl: "http://127.0.0.1:9999",
				workspaceId: "00000000-0000-7000-8000-000000000000",
				ownerId: "00000000-0000-7000-8000-000000000002",
			},
			() => "mock-token",
			mockFetch as never,
			tempDir,
		);

		let thrownError: unknown;
		try {
			await backend.setExecutionState({
				grantId: "00000000-0000-7000-8000-000000000003",
				expectedGrantVersion: 1,
				targetState: "cancelled",
				reason: "cancelled",
			});
		} catch (err) {
			thrownError = err;
		}

		expect(thrownError).toBeInstanceOf(WorkError);
		expect((thrownError as WorkError).status).toBe(409);
		expect((thrownError as WorkError).code).toBe("execution_grant_inactive");
		expect(postCount).toBe(1);
		expect(getCount).toBe(0);

		const files = await fs.readdir(tempDir);
		expect(files.filter(f => f.endsWith(".json")).length).toBe(0);
	});

	test("recovery with stored top-level request_id mismatch: throws identity mismatch, claim bytes unchanged, zero POST", async () => {
		const tempDir = await makeTempDir();
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const ownerId = "00000000-0000-7000-8000-000000000002";
		const grantId = "00000000-0000-7000-8000-000000000003";
		const opId = "00000000-0000-7000-8000-000000000010";
		const reqId = "00000000-0000-7000-8000-000000000011";

		let postCount = 0;
		let getCount = 0;

		const command = {
			type: "seal_execution_criteria" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 2,
				work_id: "00000000-0000-7000-8000-000000000001",
				expected_revision_id: "00000000-0000-7000-8000-000000000002",
				criteria: ["criterion 1"],
				description_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
				judge_sha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
			},
		};
		const canonicalHash = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command,
		});

		const intent = intentFingerprint("intent", workspaceId, ownerId, command.type, command.payload);
		const claim = await claimPendingOp(tempDir, intent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: opId,
			request_id: reqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command,
		}));
		const bytesBefore = await Bun.file(claim.path).text();

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			const method = init?.method ?? (input instanceof Request ? input.method : "GET");
			if (method === "POST" || url.endsWith("/v1/commands")) {
				postCount++;
				return new Response(JSON.stringify({ error: "unexpected_post" }), { status: 500 });
			}
			if (url.includes(`/v1/operations/${opId}`)) {
				getCount++;
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: opId,
							request_id: reqId,
							state: "applied",
							request_sha256: canonicalHash,
							result_sha256: "4567",
							diagnostics: [],
						},
						command_type: "seal_execution_criteria",
						request_id: "00000000-0000-7000-8000-000000000999", // mismatch with env request_id
						correlation_id: "00000000-0000-7000-8000-000000000099",
						result: {
							type: "seal_execution_criteria",
							grant: { grant_id: grantId, grant_version: 3, state: "active" },
							revision: { revision_id: "00000000-0000-7000-8000-000000000021" },
						},
					}),
					{ status: 200, headers: { "Content-Type": "application/json" } },
				);
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{
				baseUrl: "http://127.0.0.1:9999",
				workspaceId,
				ownerId,
			},
			() => "mock-token",
			mockFetch as never,
			tempDir,
		);

		let thrown: Error | null = null;
		try {
			await backend.getPendingExecutionClaims(grantId);
		} catch (err) {
			thrown = err as Error;
		}

		expect(thrown).not.toBeNull();
		expect(thrown!.message).toContain("identity mismatch");
		expect(thrown!.message).toContain(`unresolved pending claim ${claim.path}`);
		expect(thrown!.message).toContain("automatic recovery refused; use stop/cancel or repair the claim");
		expect(postCount).toBe(0);
		expect(getCount).toBe(1);
		expect(await Bun.file(claim.path).text()).toBe(bytesBefore);
	});

	test("recovery with stored result.type mismatch: throws identity mismatch, claim bytes unchanged, zero POST", async () => {
		const tempDir = await makeTempDir();
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const ownerId = "00000000-0000-7000-8000-000000000002";
		const grantId = "00000000-0000-7000-8000-000000000003";
		const opId = "00000000-0000-7000-8000-000000000010";
		const reqId = "00000000-0000-7000-8000-000000000011";

		let postCount = 0;
		let getCount = 0;

		const command = {
			type: "seal_execution_criteria" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 2,
				work_id: "00000000-0000-7000-8000-000000000001",
				expected_revision_id: "00000000-0000-7000-8000-000000000002",
				criteria: ["criterion 1"],
				description_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
				judge_sha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
			},
		};
		const canonicalHash = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command,
		});

		const intent = intentFingerprint("intent", workspaceId, ownerId, command.type, command.payload);
		const claim = await claimPendingOp(tempDir, intent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: opId,
			request_id: reqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command,
		}));
		const bytesBefore = await Bun.file(claim.path).text();

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			const method = init?.method ?? (input instanceof Request ? input.method : "GET");
			if (method === "POST" || url.endsWith("/v1/commands")) {
				postCount++;
				return new Response(JSON.stringify({ error: "unexpected_post" }), { status: 500 });
			}
			if (url.includes(`/v1/operations/${opId}`)) {
				getCount++;
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: opId,
							request_id: reqId,
							state: "applied",
							request_sha256: canonicalHash,
							result_sha256: "4567",
							diagnostics: [],
						},
						command_type: "seal_execution_criteria",
						request_id: reqId,
						correlation_id: "00000000-0000-7000-8000-000000000099",
						result: {
							type: "begin_execution", // mismatch with command.type
							grant: { grant_id: grantId, grant_version: 3, state: "active" },
						},
					}),
					{ status: 200, headers: { "Content-Type": "application/json" } },
				);
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{
				baseUrl: "http://127.0.0.1:9999",
				workspaceId,
				ownerId,
			},
			() => "mock-token",
			mockFetch as never,
			tempDir,
		);

		let thrown: Error | null = null;
		try {
			await backend.getPendingExecutionClaims(grantId);
		} catch (err) {
			thrown = err as Error;
		}

		expect(thrown).not.toBeNull();
		expect(thrown!.message).toContain("identity mismatch");
		expect(thrown!.message).toContain(`unresolved pending claim ${claim.path}`);
		expect(thrown!.message).toContain("automatic recovery refused; use stop/cancel or repair the claim");
		expect(postCount).toBe(0);
		expect(getCount).toBe(1);
		expect(await Bun.file(claim.path).text()).toBe(bytesBefore);
	});

	describe("matchStoredOperation unit contract", () => {
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const opId = "00000000-0000-7000-8000-000000000010";
		const reqId = "00000000-0000-7000-8000-000000000011";

		const env: CommandEnvelope = {
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: opId,
			request_id: reqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: {
				type: "set_execution_state",
				payload: {
					grant_id: "00000000-0000-7000-8000-000000000003",
					expected_grant_version: 1,
					target_state: "cancelled",
					reason: "cancelled",
				},
			},
		};

		const expectedHash = payloadHash({
			api_version: env.api_version,
			workspace_id: env.workspace_id,
			command: env.command,
		});

		const validStored: StoredOperation = {
			receipt: {
				operation_id: opId,
				request_id: reqId,
				state: "applied",
				request_sha256: expectedHash,
				result_sha256: "hash-res",
				diagnostics: [],
			},
			command_type: "set_execution_state",
			request_id: reqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			result: {
				type: "set_execution_state",
				execution: {
					grant: {
						grant_id: "00000000-0000-7000-8000-000000000003",
						workspace_id: workspaceId,
						owner_id: "00000000-0000-7000-8000-000000000002",
						repository: "repo",
						remote_ref: "ref",
						state: "cancelled",
						mode: "single",
						grant_version: 2,
						max_continuations: 5,
						max_close_attempts: 3,
						max_no_progress: 2,
						continuations_scheduled: 0,
						authorization_hash: "auth",
						judge_sha256: "judge",
						created_at: new Date().toISOString(),
						expires_at: new Date().toISOString(),
					},
					items: [],
				},
			},
		};

		test("accepts applied operation with matching identity and result", () => {
			const match = matchStoredOperation(env, validStored);
			expect("result" in match).toBe(true);
			if ("result" in match) {
				expect(match.result).toEqual(validStored.result!);
			}
		});

		test("accepts replayed state", () => {
			const replayedStored: StoredOperation = {
				...validStored,
				receipt: { ...validStored.receipt, state: "replayed" },
			};
			const match = matchStoredOperation(env, replayedStored);
			expect("result" in match).toBe(true);
		});

		test("rejects when top-level request_id mismatches", () => {
			const stored: StoredOperation = {
				...validStored,
				request_id: "00000000-0000-7000-8000-000000000999",
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "identity mismatch" });
		});

		test("rejects when receipt request_id mismatches", () => {
			const stored: StoredOperation = {
				...validStored,
				receipt: { ...validStored.receipt, request_id: "00000000-0000-7000-8000-000000000999" },
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "identity mismatch" });
		});

		test("rejects when receipt operation_id mismatches", () => {
			const stored: StoredOperation = {
				...validStored,
				receipt: { ...validStored.receipt, operation_id: "00000000-0000-7000-8000-000000000999" },
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "identity mismatch" });
		});

		test("rejects when stored command_type mismatches", () => {
			const stored: StoredOperation = {
				...validStored,
				command_type: "seal_execution_criteria",
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "identity mismatch" });
		});

		test("rejects when receipt request_sha256 mismatches", () => {
			const stored: StoredOperation = {
				...validStored,
				receipt: { ...validStored.receipt, request_sha256: "0".repeat(64) },
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "identity mismatch" });
		});

		test("rejects when result.type mismatches", () => {
			const stored: StoredOperation = {
				...validStored,
				result: {
					type: "seal_execution_criteria",
					grant: {
						grant_id: "00000000-0000-7000-8000-000000000003",
						grant_version: 2,
						state: "active",
					},
					revision: {
						revision_id: "00000000-0000-7000-8000-000000000021",
					},
				},
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "identity mismatch" });
		});

		test("returns state reason when state is rejected", () => {
			const stored: StoredOperation = {
				...validStored,
				receipt: { ...validStored.receipt, state: "rejected" },
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "state is rejected" });
		});

		test("returns state reason when state is pending_approval", () => {
			const stored: StoredOperation = {
				...validStored,
				receipt: { ...validStored.receipt, state: "pending_approval" },
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "state is pending_approval" });
		});

		test("returns result is null when result is null", () => {
			const stored: StoredOperation = {
				...validStored,
				result: null,
			};
			expect(matchStoredOperation(env, stored)).toEqual({ reason: "result is null" });
		});
	});

	describe("run() lost-response reconciliation", () => {
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

		/** POST always fails 503; the operation read 404s `notFoundCount` times
		 *  before returning the row the POST would have committed. */
		function makeMock(options: { notFoundCount: number; mutateStored?: (stored: StoredOperation) => StoredOperation }) {
			let postCount = 0;
			let operationGetCount = 0;
			let stored: StoredOperation | undefined;
			const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
				const url = String(input);
				const method = init?.method ?? (input instanceof Request ? input.method : "GET");
				if (method === "POST" || url.endsWith("/v1/commands")) {
					postCount++;
					const env = JSON.parse(String(init?.body)) as CommandEnvelope;
					stored = {
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
					return new Response(
						JSON.stringify({ error: { code: "unavailable", request_id: null, correlation_id: null, diagnostics: ["boom"] } }),
						{ status: 503, headers: { "Content-Type": "application/json" } },
					);
				}
				if (url.includes("/v1/operations/")) {
					operationGetCount++;
					if (operationGetCount <= options.notFoundCount) return new Response("not found", { status: 404 });
					const body = options.mutateStored && stored ? options.mutateStored(stored) : stored;
					return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
				}
				if (url.includes(`/v1/work-items/${KEY}`)) {
					return new Response(JSON.stringify(workItemView()), { status: 200, headers: { "Content-Type": "application/json" } });
				}
				return new Response("not found", { status: 404 });
			};
			return { mockFetch, counts: () => ({ postCount, operationGetCount }) };
		}

		async function readOnlyClaim(dir: string) {
			const files = (await fs.readdir(dir)).filter(f => f.endsWith(".json"));
			expect(files.length).toBe(1);
			const path = `${dir}/${files[0]}`;
			return { path, record: JSON.parse(await Bun.file(path).text()) as { result?: unknown; envelope: CommandEnvelope } };
		}

		test("delayed visibility: 503 then 404 x3 then applied resolves with the stored result and never re-POSTs", async () => {
			const tempDir = await makeTempDir();
			const { mockFetch, counts } = makeMock({ notFoundCount: 3 });
			const backend = createWorkBackend(
				{ baseUrl: "http://127.0.0.1:9999", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			const first = await backend.stampPlan(nowRef, stamp);
			expect(first.plannedCandidateId).toBeDefined();
			expect(counts().postCount).toBe(1);
			expect(counts().operationGetCount).toBe(4); // 3 x 404 + 1 applied

			const claim = await readOnlyClaim(tempDir);
			expect(claim.record.result).toBeDefined();

			const second = await backend.stampPlan(nowRef, stamp);
			expect(second.plannedCandidateId).toBe(first.plannedCandidateId);
			expect(counts().postCount).toBe(1);
			expect(counts().operationGetCount).toBe(4);
		});

		test("mismatched stored identity after 503 rejects outcome unknown with one GET and keeps the claim unresolved", async () => {
			const tempDir = await makeTempDir();
			const { mockFetch, counts } = makeMock({
				notFoundCount: 0,
				mutateStored: stored => ({ ...stored, receipt: { ...stored.receipt, request_sha256: "f".repeat(64) } }),
			});
			const backend = createWorkBackend(
				{ baseUrl: "http://127.0.0.1:9999", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			let thrown: Error | null = null;
			try {
				await backend.stampPlan(nowRef, stamp);
			} catch (err) {
				thrown = err as Error;
			}
			expect(thrown).not.toBeNull();
			expect(thrown!.message).toContain("outcome unknown");
			expect(counts().postCount).toBe(1);
			expect(counts().operationGetCount).toBe(1);

			const claim = await readOnlyClaim(tempDir);
			expect(claim.record.result).toBeUndefined();
		});

		test("404 forever rejects outcome unknown after exactly OPERATION_POLL_ATTEMPTS GETs and the next call adds no POST", async () => {
			const tempDir = await makeTempDir();
			const { mockFetch, counts } = makeMock({ notFoundCount: Number.POSITIVE_INFINITY });
			const backend = createWorkBackend(
				{ baseUrl: "http://127.0.0.1:9999", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			let thrown: Error | null = null;
			try {
				await backend.stampPlan(nowRef, stamp);
			} catch (err) {
				thrown = err as Error;
			}
			expect(thrown).not.toBeNull();
			expect(thrown!.message).toContain("outcome unknown");
			expect(counts().postCount).toBe(1);
			expect(counts().operationGetCount).toBe(OPERATION_POLL_ATTEMPTS);

			const claim = await readOnlyClaim(tempDir);
			expect(claim.record.result).toBeUndefined();

			let secondThrown: Error | null = null;
			try {
				await backend.stampPlan(nowRef, stamp);
			} catch (err) {
				secondThrown = err as Error;
			}
			expect(secondThrown).not.toBeNull();
			expect(secondThrown!.message).toContain("outcome unknown");
			expect(counts().postCount).toBe(1); // no new POST
			expect(claim.record.result).toBeUndefined();
		});
	});
});
