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

		// 4. Claim for non-seal operation (matching workspace, owner, grant, but non-seal command with omitted default/optional field)
		const nonSealCommand = {
			type: "set_execution_state" as const,
			payload: {
				grant_id: grantId,
				expected_grant_version: 3,
				phase: "planning" as const,
			},
		};
		const nonSealIntent = intentFingerprint("intent", workspaceId, ownerId, nonSealCommand.type, nonSealCommand.payload);
		const nonSealClaim = await claimPendingOp(tempDir, nonSealIntent, () => ({
			api_version: "work.omp.dev/v1",
			workspace_id: workspaceId,
			operation_id: "00000000-0000-7000-8000-000000000050",
			request_id: "00000000-0000-7000-8000-000000000051",
			correlation_id: "00000000-0000-7000-8000-000000000099",
			command: nonSealCommand,
		}));
		const nonSealBytesBefore = await Bun.file(nonSealClaim.path).text();

		// Reconcile for grantId:
		// Should resolve appliedClaim; otherGrantClaim, otherOwnerClaim, and nonSealClaim must NOT be touched or fetched.
		await backend.getPendingExecutionClaims!(grantId);

		expect(operationsRequested).toEqual([appliedOpId]);

		// Applied claim must now have result written to disk
		const appliedClaimAfter = JSON.parse(await Bun.file(appliedClaim.path).text()) as { result?: { type: string } };
		expect(appliedClaimAfter.result?.type).toBe("seal_execution_criteria");

		// Other-grant, other-principal, and non-seal claims must remain untouched byte-identical
		expect(await Bun.file(otherGrantClaim.path).text()).toBe(otherGrantBytesBefore);
		expect(await Bun.file(otherOwnerClaim.path).text()).toBe(otherOwnerBytesBefore);
		expect(await Bun.file(nonSealClaim.path).text()).toBe(nonSealBytesBefore);

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
});

