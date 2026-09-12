import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { createWorkflowHost } from "../extensions/workflow/host";
import { ackOps, claimPendingOp, intentFingerprint, resolvePendingOp } from "../extensions/workflow/pending-ops";
import { createWorkBackend } from "../extensions/workflow/work";
import { payloadHash } from "@oh-my-pi/pi-work-client";
let tempDir: string;

beforeEach(() => {
	tempDir = mkdtempSync(join(tmpdir(), "pending-ops-test-"));
});

afterEach(() => {
	rmSync(tempDir, { recursive: true, force: true });
});

describe("pending-ops claim lifecycle and housekeeping", () => {
	test("resolved create claim survives delivery ack and identical create reuses stored result with one POST", async () => {
		let postCount = 0;
		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (url.includes("/tree")) {
				return new Response(JSON.stringify({ projects: [{ project_id: "00000000-0000-7000-8000-000000000010", name: "Alpha" }] }), {
					status: 200,
					headers: { "Content-Type": "application/json" },
				});
			}
			if (url.endsWith("/v1/commands")) {
				postCount++;
				const body = JSON.parse(String(init?.body)) as { operation_id: string; command: { type: string; payload: unknown } };
				return new Response(
					JSON.stringify({
						applied: true,
						result: {
							type: "create_work_batch",
							items: [
								{
									client_ref: "p",
									work_id: "00000000-0000-7000-8000-000000000001",
									key: "OMP-1",
								},
							],
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
				workspaceId: "00000000-0000-7000-8000-000000000000",
				ownerId: "00000000-0000-7000-8000-000000000002",
			},
			() => "mock-token",
			mockFetch as never,
			tempDir,
		);

		// First create: sends POST
		const res1 = await backend.createIssue({ title: "Task 1", project: "Alpha" });
		expect(res1.key).toBe("OMP-1");
		expect(postCount).toBe(1);

		const delivered = backend.deliveredOps?.() ?? [];
		expect(delivered.length).toBe(1);

		// Acknowledge delivery (as done at turn start / session start)
		await backend.ackOps?.(delivered);

		// Claim file must still exist on disk (delivered create claim survives delivery ack)
		const filesAfterAck = await readdir(tempDir);
		expect(filesAfterAck.filter(f => f.endsWith(".json")).length).toBe(1);

		// Second identical create in same or restarted session: must return stored result without second POST
		const res2 = await backend.createIssue({ title: "Task 1", project: "Alpha" });
		expect(res2.key).toBe("OMP-1");
		expect(postCount).toBe(1); // STILL 1: reused stored result
	});

	test("resolved create claim expires after the 24-hour ceiling", async () => {
		const claim = await claimPendingOp(tempDir, "test-intent-ttl", () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: "00000000-0000-7000-8000-000000000000",
			operation_id: "00000000-0000-7000-8000-000000000001",
			command: { type: "create_work", payload: {} },
		}));
		await resolvePendingOp(claim.path, claim.record!, { work_id: "00000000-0000-7000-8000-000000000001" });

		const now = Date.now();
		// Sweep at +1 hour: claim survives
		await ackOps(tempDir, new Set(), now + 3600_000);
		expect(existsSync(claim.path)).toBe(true);

		// Sweep at +25 hours (> 24 hour ceiling): claim is removed
		await ackOps(tempDir, new Set(), now + 25 * 3600_000);
		expect(existsSync(claim.path)).toBe(false);
	});

	test("unresolved pending claim is never swept", async () => {
		const claim = await claimPendingOp(tempDir, "test-intent-unresolved", () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: "00000000-0000-7000-8000-000000000000",
			operation_id: "00000000-0000-7000-8000-000000000001",
			command: { type: "create_work", payload: {} },
		}));
		// Record has no result (unresolved)
		expect(claim.record?.result).toBeUndefined();

		const now = Date.now();
		// Sweep even after 48 hours with empty or populated delivered set
		await ackOps(tempDir, new Set(["00000000-0000-7000-8000-000000000001"]), now + 48 * 3600_000);
		expect(existsSync(claim.path)).toBe(true);
	});

	test("resolved health claim sweeps at next session start so later same-status update performs a new POST", async () => {
		let healthPosts = 0;
		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (url.includes("/tree")) {
				return new Response(JSON.stringify({ projects: [{ project_id: "00000000-0000-7000-8000-000000000010", name: "Beta" }] }), {
					status: 200,
					headers: { "Content-Type": "application/json" },
				});
			}
			if (url.endsWith("/v1/commands")) {
				healthPosts++;
				return new Response(
					JSON.stringify({
						applied: true,
						result: {
							type: "record_project_health",
							project_id: "00000000-0000-7000-8000-000000000010",
							health: "onTrack",
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
				workspaceId: "00000000-0000-7000-8000-000000000000",
				ownerId: "00000000-0000-7000-8000-000000000002",
			},
			() => "mock-token",
			mockFetch as never,
			tempDir,
		);

		// First health update: performs POST
		await backend.recordHealth("Beta", "onTrack");
		expect(healthPosts).toBe(1);

		// Before sweep, claim exists
		const filesBeforeSweep = await readdir(tempDir);
		expect(filesBeforeSweep.filter(f => f.endsWith(".json")).length).toBe(1);

		// Session sweep (ackOps called on session start with or without delivered ids)
		await backend.ackOps?.([]);

		// Claim must be deleted on sweep
		const filesAfterSweep = await readdir(tempDir);
		expect(filesAfterSweep.filter(f => f.endsWith(".json")).length).toBe(0);

		// Later same-status recording: performs a fresh POST
		await backend.recordHealth("Beta", "onTrack");
		expect(healthPosts).toBe(2);
	});

	test("getPendingExecutionClaims reconciles applied execution claim, refuses invalid/missing rows, ignores other principal/grant, and supports concurrent resolution", async () => {
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const ownerId = "00000000-0000-7000-8000-000000000002";
		const grantId = "00000000-0000-7000-8000-000000000003";
		const otherGrantId = "00000000-0000-7000-8000-000000000004";
		const otherOwnerId = "00000000-0000-7000-8000-000000000005";

		const appliedOpId = "00000000-0000-7000-8000-000000000010";
		const appliedReqId = "00000000-0000-7000-8000-000000000011";

		const missingOpId = "00000000-0000-7000-8000-000000000020";
		const missingReqId = "00000000-0000-7000-8000-000000000021";

		const otherGrantOpId = "00000000-0000-7000-8000-000000000030";
		const otherOwnerOpId = "00000000-0000-7000-8000-000000000040";

		const operationsRequested: string[] = [];

		// 1. Claim for applied operation (eligible: matching workspace, owner, grant, grant-bearing command)
		const appliedCommand = {
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
		const appliedCanonicalHash = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command: appliedCommand,
		});

		const mockFetch = async (input: RequestInfo | URL): Promise<Response> => {
			const url = String(input);
			if (url.includes(`/v1/operations/${appliedOpId}`)) {
				operationsRequested.push(appliedOpId);
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: appliedOpId,
							request_id: appliedReqId,
							state: "applied",
							request_sha256: appliedCanonicalHash,
							result_sha256: "4567",
							diagnostics: [],
						},
						command_type: "seal_execution_criteria",
						request_id: appliedReqId,
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
			if (url.includes(`/v1/operations/${missingOpId}`)) {
				operationsRequested.push(missingOpId);
				return new Response(
					JSON.stringify({
						error: "invalid_request",
						message: "operation not found",
					}),
					{ status: 400, headers: { "Content-Type": "application/json" } },
				);
			}
			if (url.includes(`/v1/operations/${otherGrantOpId}`) || url.includes(`/v1/operations/${otherOwnerOpId}`)) {
				operationsRequested.push(url);
				return new Response("not found", { status: 404 });
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
		const appliedIntent = intentFingerprint("intent", workspaceId, ownerId, appliedCommand.type, appliedCommand.payload);
		const appliedClaim = await claimPendingOp(tempDir, appliedIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: appliedOpId,
			request_id: appliedReqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: appliedCommand,
		}));

		// 2. Claim for other-grant operation (eligible command type, matching owner, but different grantId)
		const otherGrantCommand = {
			type: "seal_execution_criteria" as const,
			payload: {
				grant_id: otherGrantId,
				expected_grant_version: 1,
				work_id: "00000000-0000-7000-8000-000000000001",
				expected_revision_id: "00000000-0000-7000-8000-000000000002",
				criteria: ["other criteria"],
				description_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
				judge_sha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
			},
		};
		const otherGrantIntent = intentFingerprint("intent", workspaceId, ownerId, otherGrantCommand.type, otherGrantCommand.payload);
		const otherGrantClaim = await claimPendingOp(tempDir, otherGrantIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: otherGrantOpId,
			request_id: "00000000-0000-7000-8000-000000000031",
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: otherGrantCommand,
		}));
		const otherGrantBytesBefore = await Bun.file(otherGrantClaim.path).text();

		// 3. Claim for other-principal operation (matching grantId, but fingerprinted with otherOwnerId)
		const otherOwnerIntent = intentFingerprint("intent", workspaceId, otherOwnerId, appliedCommand.type, appliedCommand.payload);
		const otherOwnerClaim = await claimPendingOp(tempDir, otherOwnerIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: otherOwnerOpId,
			request_id: "00000000-0000-7000-8000-000000000041",
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: appliedCommand,
		}));
		const otherOwnerBytesBefore = await Bun.file(otherOwnerClaim.path).text();

		// 4. Claim for non-execution operation (matching workspace, owner, but non-execution command without grant_id)
		const nonExecutionCommand = {
			type: "revise_work" as const,
			payload: {
				work_id: "00000000-0000-7000-8000-000000000001",
				revision: {
					title: "revised title",
					description: "revised desc",
				},
			},
		};
		const nonExecutionIntent = intentFingerprint("intent", workspaceId, ownerId, nonExecutionCommand.type, nonExecutionCommand.payload);
		const nonExecutionClaim = await claimPendingOp(tempDir, nonExecutionIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: "00000000-0000-7000-8000-000000000050",
			request_id: "00000000-0000-7000-8000-000000000051",
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: nonExecutionCommand,
		}));
		const nonExecutionBytesBefore = await Bun.file(nonExecutionClaim.path).text();

		// Reconcile for grantId:
		// Should resolve appliedClaim; otherGrantClaim, otherOwnerClaim, and nonExecutionClaim must NOT be touched or fetched.
		await backend.getPendingExecutionClaims!(grantId);

		expect(operationsRequested).toEqual([appliedOpId]);

		// Applied claim must now have result written to disk
		const appliedClaimAfter = JSON.parse(await Bun.file(appliedClaim.path).text()) as { result?: { type: string } };
		expect(appliedClaimAfter.result?.type).toBe("seal_execution_criteria");

		// Other-grant, other-principal, and non-execution claims must remain untouched byte-identical
		expect(await Bun.file(otherGrantClaim.path).text()).toBe(otherGrantBytesBefore);
		expect(await Bun.file(otherOwnerClaim.path).text()).toBe(otherOwnerBytesBefore);
		expect(await Bun.file(nonExecutionClaim.path).text()).toBe(nonExecutionBytesBefore);

		// 5. Test 400 / missing row refusal:
		const missingCommand = {
			type: "seal_execution_criteria" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 3,
				work_id: "00000000-0000-7000-8000-000000000001",
				expected_revision_id: "00000000-0000-7000-8000-000000000002",
				criteria: ["criterion missing"],
				description_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
				judge_sha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
			},
		};
		const missingIntent = intentFingerprint("intent", workspaceId, ownerId, missingCommand.type, missingCommand.payload);
		const missingClaim = await claimPendingOp(tempDir, missingIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: missingOpId,
			request_id: missingReqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: missingCommand,
		}));
		const missingBytesBefore = await Bun.file(missingClaim.path).text();

		let thrownError: Error | null = null;
		try {
			await backend.getPendingExecutionClaims!(grantId);
		} catch (err) {
			thrownError = err as Error;
		}
		expect(thrownError).not.toBeNull();
		expect(thrownError!.message).toContain(`unresolved pending claim ${missingClaim.path}`);
		expect(thrownError!.message).toContain("automatic recovery refused; use stop/cancel or repair the claim");

		// Claim file must remain byte-identical
		expect(await Bun.file(missingClaim.path).text()).toBe(missingBytesBefore);

		// 5. Test concurrent resolvePendingOp:
		const concurrentRecord = { envelope: appliedClaim.record!.envelope };
		const res1 = { type: "seal_execution_criteria", run: 1 };
		const res2 = { type: "seal_execution_criteria", run: 2 };
		await Promise.all([
			resolvePendingOp(appliedClaim.path, concurrentRecord, res1),
			resolvePendingOp(appliedClaim.path, concurrentRecord, res2),
		]);
		const finalRecord = JSON.parse(await Bun.file(appliedClaim.path).text()) as { result?: { type: string; run: number } };
		expect(finalRecord.result?.type).toBe("seal_execution_criteria");
		expect([1, 2]).toContain(finalRecord.result!.run);
	});

	test("getPendingExecutionClaims refuses claim with identity mismatch when receipt request_sha256 differs", async () => {
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const ownerId = "00000000-0000-7000-8000-000000000002";
		const grantId = "00000000-0000-7000-8000-000000000003";
		const opId = "00000000-0000-7000-8000-000000000010";
		const reqId = "00000000-0000-7000-8000-000000000011";

		let postCount = 0;
		let operationGetCount = 0;

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

		// Receipt has different canonical request hash (matching IDs/type, but differing request payload hash)
		const differentCanonicalHash = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command: {
				...command,
				payload: { ...command.payload, criteria: ["differing criterion"] },
			},
		});

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (init?.method === "POST" || url.endsWith("/v1/commands")) {
				postCount++;
				return new Response(JSON.stringify({ applied: false }), { status: 500 });
			}
			if (url.includes(`/v1/operations/${opId}`)) {
				operationGetCount++;
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: opId,
							request_id: reqId,
							state: "applied",
							request_sha256: differentCanonicalHash,
							result_sha256: "4567",
							diagnostics: [],
						},
						command_type: "seal_execution_criteria",
						request_id: reqId,
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

		let thrown: Error | null = null;
		try {
			await backend.getPendingExecutionClaims!(grantId);
		} catch (err) {
			thrown = err as Error;
		}

		expect(thrown).not.toBeNull();
		expect(thrown!.message).toContain(`unresolved pending claim ${claim.path}`);
		expect(thrown!.message).toContain("identity mismatch");
		expect(thrown!.message).toContain("automatic recovery refused; use stop/cancel or repair the claim");

		expect(operationGetCount).toBe(1);
		expect(postCount).toBe(0);
		expect(await Bun.file(claim.path).text()).toBe(bytesBefore);
	});

	test("getPendingExecutionClaims reconciles applied non-seal execution claim without POST", async () => {
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const ownerId = "00000000-0000-7000-8000-000000000002";
		const grantId = "00000000-0000-7000-8000-000000000003";

		const stampOpId = "00000000-0000-7000-8000-000000000060";
		const stampReqId = "00000000-0000-7000-8000-000000000061";
		const stateOpId = "00000000-0000-7000-8000-000000000070";
		const stateReqId = "00000000-0000-7000-8000-000000000071";

		let postCount = 0;
		const operationsRequested: string[] = [];

		const stampCommand = {
			type: "stamp_execution_plan" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 2,
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_id: "00000000-0000-7000-8000-000000000002",
				candidate_id: "00000000-0000-7000-8000-000000000004",
				plan_file: "plan.md",
				plan_body: "plan body",
				plan_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
				approach: ["step 1"],
				verification: ["verify 1"],
				paths: ["file.ts"],
				candidate_sha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
				judge_sha256: "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
			},
		};
		const stampHash = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command: stampCommand,
		});

		// set_execution_state with reason: null (schema default alignment)
		const stateCommand = {
			type: "set_execution_state" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 3,
				target_state: "active" as const,
				reason: null,
				judge_sha256: "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
			},
		};
		const stateHash = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command: stateCommand,
		});

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (init?.method === "POST" || url.endsWith("/v1/commands")) {
				postCount++;
				return new Response(JSON.stringify({ error: "unexpected_post" }), { status: 500 });
			}
			if (url.includes(`/v1/operations/${stampOpId}`)) {
				operationsRequested.push(stampOpId);
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: stampOpId,
							request_id: stampReqId,
							state: "applied",
							request_sha256: stampHash,
							result_sha256: "1111",
							diagnostics: [],
						},
						command_type: "stamp_execution_plan",
						request_id: stampReqId,
						correlation_id: "00000000-0000-7000-8000-000000000099",
						result: {
							type: "stamp_execution_plan",
							item: { plan_stamp: "stamp-123" },
							receipt: { kind: "plan", receipt_id: "00000000-0000-7000-8000-000000000088", payload: {} },
							candidate: { candidate_id: "00000000-0000-7000-8000-000000000004" },
						},
					}),
					{ status: 200, headers: { "Content-Type": "application/json" } },
				);
			}
			if (url.includes(`/v1/operations/${stateOpId}`)) {
				operationsRequested.push(stateOpId);
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: stateOpId,
							request_id: stateReqId,
							state: "applied",
							request_sha256: stateHash,
							result_sha256: "2222",
							diagnostics: [],
						},
						command_type: "set_execution_state",
						request_id: stateReqId,
						correlation_id: "00000000-0000-7000-8000-000000000099",
						result: {
							type: "set_execution_state",
							grant: { grant_id: grantId, grant_version: 4, state: "active" },
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

		const stampIntent = intentFingerprint("intent", workspaceId, ownerId, stampCommand.type, stampCommand.payload);
		const stampClaim = await claimPendingOp(tempDir, stampIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: stampOpId,
			request_id: stampReqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: stampCommand,
		}));

		const stateIntent = intentFingerprint("intent", workspaceId, ownerId, stateCommand.type, stateCommand.payload);
		const stateClaim = await claimPendingOp(tempDir, stateIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: stateOpId,
			request_id: stateReqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: stateCommand,
		}));

		const recovered = await backend.getPendingExecutionClaims!(grantId);

		expect(postCount).toBe(0);
		expect(operationsRequested).toContain(stampOpId);
		expect(operationsRequested).toContain(stateOpId);

		const stampAfter = JSON.parse(await Bun.file(stampClaim.path).text()) as { result?: { type: string } };
		expect(stampAfter.result?.type).toBe("stamp_execution_plan");

		const stateAfter = JSON.parse(await Bun.file(stateClaim.path).text()) as { result?: { type: string } };
		expect(stateAfter.result?.type).toBe("set_execution_state");

		// Preserves result projection: set_execution_state is returned; stamp_execution_plan is resolved on disk but not in host projection
		const returnedSetState = recovered.find(r => r.command.type === "set_execution_state");
		expect(returnedSetState).toBeDefined();
		expect(returnedSetState!.result?.type).toBe("set_execution_state");
		const returnedStamp = recovered.find(r => r.command.type === "stamp_execution_plan");
		expect(returnedStamp).toBeUndefined();
	});

	test("getPendingExecutionClaims refuses claim when stored operation command_type mismatches actual claim type", async () => {
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const ownerId = "00000000-0000-7000-8000-000000000002";
		const grantId = "00000000-0000-7000-8000-000000000003";
		const opId = "00000000-0000-7000-8000-000000000080";
		const reqId = "00000000-0000-7000-8000-000000000081";

		let postCount = 0;
		let operationGetCount = 0;

		const command = {
			type: "stamp_execution_plan" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 2,
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_id: "00000000-0000-7000-8000-000000000002",
				candidate_id: "00000000-0000-7000-8000-000000000004",
				plan_file: "plan.md",
				plan_body: "plan body",
				plan_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
				approach: ["step 1"],
				verification: ["verify 1"],
				paths: ["file.ts"],
				candidate_sha256: "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
				judge_sha256: "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
			},
		};
		const canonicalHash = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command,
		});

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (init?.method === "POST" || url.endsWith("/v1/commands")) {
				postCount++;
				return new Response(JSON.stringify({ error: "unexpected_post" }), { status: 500 });
			}
			if (url.includes(`/v1/operations/${opId}`)) {
				operationGetCount++;
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: opId,
							request_id: reqId,
							state: "applied",
							request_sha256: canonicalHash,
							result_sha256: "9999",
							diagnostics: [],
						},
						// Mismatched stored command_type: seal_execution_criteria instead of stamp_execution_plan
						command_type: "seal_execution_criteria",
						request_id: reqId,
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

		let thrown: Error | null = null;
		try {
			await backend.getPendingExecutionClaims!(grantId);
		} catch (err) {
			thrown = err as Error;
		}

		expect(thrown).not.toBeNull();
		expect(thrown!.message).toContain(`unresolved pending claim ${claim.path}`);
		expect(thrown!.message).toContain("identity mismatch");
		expect(thrown!.message).toContain("automatic recovery refused; use stop/cancel or repair the claim");

		expect(operationGetCount).toBe(1);
		expect(postCount).toBe(0);
		expect(await Bun.file(claim.path).text()).toBe(bytesBefore);
	});

	test("getPendingExecutionClaims leaves unadmitted grant-bound claims untouched and unfetched", async () => {
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const ownerId = "00000000-0000-7000-8000-000000000002";
		const grantId = "00000000-0000-7000-8000-000000000003";

		const activateOpId = "00000000-0000-7000-8000-000000000010";
		const activateReqId = "00000000-0000-7000-8000-000000000011";
		const beginOpId = "00000000-0000-7000-8000-000000000020";
		const beginReqId = "00000000-0000-7000-8000-000000000021";
		const completeOpId = "00000000-0000-7000-8000-000000000030";
		const completeReqId = "00000000-0000-7000-8000-000000000031";

		let postCount = 0;
		const operationsRequested: string[] = [];

		// activate_execution_item omitting expected_project_id / expected_blocker_ids (project-less client form)
		const activateCommand = {
			type: "activate_execution_item" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 1,
				position: 0,
				work_id: "00000000-0000-7000-8000-000000000001",
				expected_revision_id: "00000000-0000-7000-8000-000000000002",
				git_baseline: "abcdef0123456789abcdef0123456789abcdef01",
				judge_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
				expected_focus_version: 1,
			},
		};
		// Service computes hash over model_dump which includes defaulted fields
		const activateServiceSha256 = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command: {
				type: "activate_execution_item" as const,
				payload: {
					...activateCommand.payload,
					expected_project_id: null,
					expected_blocker_ids: [],
				},
			},
		});

		// begin_execution omitting items[0].project_id and active_blocker_ids
		const beginCommand = {
			type: "begin_execution" as const,
			payload: {
				grant_id: grantId,
				provenance: {
					owner_input_id: "00000000-0000-7000-8000-000000000040",
					owner_session_id: "00000000-0000-7000-8000-000000000041",
					normalized_command: "test",
					workspace_id: workspaceId,
					repository: "/repo",
					nonce: "nonce",
					issued_at: "2026-09-12T10:00:00.000Z",
				},
				remote_ref: "refs/heads/main",
				mode: "single" as const,
				items: [
					{
						work_id: "00000000-0000-7000-8000-000000000001",
						revision_id: "00000000-0000-7000-8000-000000000002",
						position: 0,
						original_request: "req",
						original_request_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
						initial_git_baseline: "abcdef0123456789abcdef0123456789abcdef01",
					},
				],
				expected_focus_version: 1,
				judge_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
				judge_manifest: {
					auditor_agent_sha256: "1111111111111111111111111111111111111111111111111111111111111111",
					host_sha256: "2222222222222222222222222222222222222222222222222222222222222222",
					adapter_sha256: "3333333333333333333333333333333333333333333333333333333333333333",
					freeze_sha256: "4444444444444444444444444444444444444444444444444444444444444444",
					runner_sha256: "5555555555555555555555555555555555555555555555555555555555555555",
					executor_sha256: "6666666666666666666666666666666666666666666666666666666666666666",
					contract_sha256: "7777777777777777777777777777777777777777777777777777777777777777",
					service_fingerprint: "8888888888888888888888888888888888888888888888888888888888888888",
					service_code_fingerprint: "9999999999999999999999999999999999999999999999999999999999999999",
					service_migration_sha256: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
				},
			},
		};
		const beginServiceSha256 = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command: {
				type: "begin_execution" as const,
				payload: {
					...beginCommand.payload,
					items: [
						{
							...beginCommand.payload.items[0],
							project_id: null,
							active_blocker_ids: [],
						},
					],
				},
			},
		});

		// complete_execution_item
		const completeCommand = {
			type: "complete_execution_item" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 1,
				work_id: "00000000-0000-7000-8000-000000000001",
				attempt_id: "00000000-0000-7000-8000-000000000050",
				evidence: {
					runner: {
						issuer: "work-service/auditor-settle",
						launch_id: "00000000-0000-7000-8000-000000000051",
						tool_call_id: "call_1",
						task_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
						judge_sha256: null,
					},
					subject: {
						work_id: "00000000-0000-7000-8000-000000000001",
						revision_id: "00000000-0000-7000-8000-000000000002",
						candidate_id: "00000000-0000-7000-8000-000000000052",
						candidate_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
						candidate_commit: "0123456789abcdef0123456789abcdef01234567",
					},
					check: {
						definition: "sealed_audit_manifest",
						version: 1,
						manifest_id: "00000000-0000-7000-8000-000000000053",
						task_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
					},
					result: "PASS" as const,
					artifacts: [],
					delivery: {
						repository: "/repo",
						remote_url: "https://example.invalid/repo.git",
						remote_ref: "refs/heads/main",
						candidate_commit: "0123456789abcdef0123456789abcdef01234567",
						remote_commit: "0123456789abcdef0123456789abcdef01234567",
					},
				},
				judge_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
			},
		};
		const completeSha256 = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command: completeCommand,
		});

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (init?.method === "POST" || url.endsWith("/v1/commands")) {
				postCount++;
				return new Response(JSON.stringify({ error: "unexpected_post" }), { status: 500 });
			}
			if (url.includes(`/v1/operations/${activateOpId}`)) {
				operationsRequested.push(activateOpId);
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: activateOpId,
							request_id: activateReqId,
							state: "applied",
							request_sha256: activateServiceSha256,
							result_sha256: "1111",
							diagnostics: [],
						},
						command_type: "activate_execution_item",
						request_id: activateReqId,
						correlation_id: "00000000-0000-7000-8000-000000000099",
						result: {
							type: "activate_execution_item",
							grant: { grant_id: grantId, grant_version: 2, state: "active" },
							item: { work_id: "00000000-0000-7000-8000-000000000001", phase: "planning" },
						},
					}),
					{ status: 200, headers: { "Content-Type": "application/json" } },
				);
			}
			if (url.includes(`/v1/operations/${beginOpId}`)) {
				operationsRequested.push(beginOpId);
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: beginOpId,
							request_id: beginReqId,
							state: "applied",
							request_sha256: beginServiceSha256,
							result_sha256: "2222",
							diagnostics: [],
						},
						command_type: "begin_execution",
						request_id: beginReqId,
						correlation_id: "00000000-0000-7000-8000-000000000099",
						result: {
							type: "begin_execution",
							grant: { grant_id: grantId, grant_version: 1, state: "active" },
						},
					}),
					{ status: 200, headers: { "Content-Type": "application/json" } },
				);
			}
			if (url.includes(`/v1/operations/${completeOpId}`)) {
				operationsRequested.push(completeOpId);
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: completeOpId,
							request_id: completeReqId,
							state: "applied",
							request_sha256: completeSha256,
							result_sha256: "3333",
							diagnostics: [],
						},
						command_type: "complete_execution_item",
						request_id: completeReqId,
						correlation_id: "00000000-0000-7000-8000-000000000099",
						result: {
							type: "complete_execution_item",
							grant: { grant_id: grantId, grant_version: 2, state: "completed" },
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

		const activateIntent = intentFingerprint("intent", workspaceId, ownerId, activateCommand.type, activateCommand.payload);
		const activateClaim = await claimPendingOp(tempDir, activateIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: activateOpId,
			request_id: activateReqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: activateCommand,
		}));
		const activateBytesBefore = await Bun.file(activateClaim.path).text();

		const beginIntent = intentFingerprint("intent", workspaceId, ownerId, beginCommand.type, beginCommand.payload);
		const beginClaim = await claimPendingOp(tempDir, beginIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: beginOpId,
			request_id: beginReqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: beginCommand,
		}));
		const beginBytesBefore = await Bun.file(beginClaim.path).text();

		const completeIntent = intentFingerprint("intent", workspaceId, ownerId, completeCommand.type, completeCommand.payload);
		const completeClaim = await claimPendingOp(tempDir, completeIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: completeOpId,
			request_id: completeReqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: completeCommand,
		}));
		const completeBytesBefore = await Bun.file(completeClaim.path).text();

		const recovered = await backend.getPendingExecutionClaims!(grantId);

		expect(postCount).toBe(0);
		expect(operationsRequested).toEqual([]);
		expect(await Bun.file(activateClaim.path).text()).toBe(activateBytesBefore);
		expect(await Bun.file(beginClaim.path).text()).toBe(beginBytesBefore);
		expect(await Bun.file(completeClaim.path).text()).toBe(completeBytesBefore);

		// Returned projection preserves lines 2110–2112: begin_execution included with undefined result
		const returnedBegin = recovered.find(r => r.command.type === "begin_execution");
		expect(returnedBegin).toBeDefined();
		expect(returnedBegin!.result).toBeUndefined();
		expect(recovered.find(r => r.command.type === "activate_execution_item")).toBeUndefined();
		expect(recovered.find(r => r.command.type === "complete_execution_item")).toBeUndefined();
	});

	test("pre-OMP-277 set_execution_state claim without reason key is refused as identity mismatch", async () => {
		const workspaceId = "00000000-0000-7000-8000-000000000000";
		const ownerId = "00000000-0000-7000-8000-000000000002";
		const grantId = "00000000-0000-7000-8000-000000000003";
		const opId = "00000000-0000-7000-8000-000000000090";
		const reqId = "00000000-0000-7000-8000-000000000091";

		let postCount = 0;
		let operationGetCount = 0;

		// Pre-upgrade claim payload has no 'reason' property
		const legacyCommand = {
			type: "set_execution_state" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 2,
				target_state: "active" as const,
				judge_sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
			},
		};

		// Service computes receipt request_sha256 including reason: null from Pydantic model_dump
		const serviceCommand = {
			type: "set_execution_state" as const,
			payload: {
				...legacyCommand.payload,
				reason: null,
			},
		};
		const serviceHash = payloadHash({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			command: serviceCommand,
		});

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (init?.method === "POST" || url.endsWith("/v1/commands")) {
				postCount++;
				return new Response(JSON.stringify({ error: "unexpected_post" }), { status: 500 });
			}
			if (url.includes(`/v1/operations/${opId}`)) {
				operationGetCount++;
				return new Response(
					JSON.stringify({
						receipt: {
							operation_id: opId,
							request_id: reqId,
							state: "applied",
							request_sha256: serviceHash,
							result_sha256: "4444",
							diagnostics: [],
						},
						command_type: "set_execution_state",
						request_id: reqId,
						correlation_id: "00000000-0000-7000-8000-000000000099",
						result: {
							type: "set_execution_state",
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

		const intent = intentFingerprint("intent", workspaceId, ownerId, legacyCommand.type, legacyCommand.payload);
		const claim = await claimPendingOp(tempDir, intent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: opId,
			request_id: reqId,
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: legacyCommand,
		}));
		const bytesBefore = await Bun.file(claim.path).text();

		let thrown: Error | null = null;
		try {
			await backend.getPendingExecutionClaims!(grantId);
		} catch (err) {
			thrown = err as Error;
		}

		expect(thrown).not.toBeNull();
		expect(thrown!.message).toContain(`unresolved pending claim ${claim.path}`);
		expect(thrown!.message).toContain("identity mismatch");
		expect(thrown!.message).toContain("automatic recovery refused; use stop/cancel or repair the claim");

		expect(operationGetCount).toBe(1);
		expect(postCount).toBe(0);
		expect(await Bun.file(claim.path).text()).toBe(bytesBefore);
	});
});

