import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import {
	type CommandEnvelope,
	type ExecutionGrantItemView,
	type ExecutionGrantView,
	payloadHash,
} from "@oh-my-pi/pi-work-client";
import { claimPendingOp, intentFingerprint, resolvePendingOp } from "../extensions/workflow/pending-ops";
import { createWorkBackend } from "../extensions/workflow/work";

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";
const OTHER_WORKSPACE_ID = "00000000-0000-7000-8000-000000000099";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";
const BASE_URL = "http://127.0.0.1:9999";
const GRANT_ID = "00000000-0000-7000-8000-000000000010";
const OTHER_GRANT_ID = "00000000-0000-7000-8000-000000000011";

const mockGrant: ExecutionGrantView = {
	grant_id: GRANT_ID,
	workspace_id: WORKSPACE_ID,
	owner_id: OWNER_ID,
	repository: "theturtlecsz/oh-my-pi",
	remote_ref: "refs/heads/main",
	state: "active",
	mode: "queue",
	grant_version: 3,
	max_continuations: 3,
	max_close_attempts: 3,
	max_no_progress: 3,
	continuations_scheduled: 0,
	authorization_hash: "b".repeat(64),
	judge_sha256: "a".repeat(64),
	created_at: "2026-09-25T10:00:00Z",
	expires_at: "2026-09-25T11:00:00Z",
};

const mockItem: ExecutionGrantItemView = {
	item_id: "00000000-0000-7000-8000-000000000020",
	workspace_id: WORKSPACE_ID,
	grant_id: GRANT_ID,
	work_id: "00000000-0000-7000-8000-000000000030",
	position: 0,
	phase: "skipped",
	claimed_revision_id: "00000000-0000-7000-8000-000000000040",
	initial_git_baseline: "c".repeat(40),
	original_request: "do work",
	original_request_sha256: "d".repeat(64),
	close_attempts_started: 0,
	consecutive_no_progress: 0,
	skipped_at: "2026-09-25T10:05:00Z",
	terminal_reason: "owner_skip",
};

describe("work-backend-skip-recovery", () => {
	let tempDir: string;

	beforeEach(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "work-backend-skip-recovery-"));
	});

	afterEach(async () => {
		await fs.rm(tempDir, { recursive: true, force: true });
	});

	describe("getCommittedSkipClaims", () => {
		test("unresolved claim + applied stored op: returned, file resolved, zero POST", async () => {
			const opId = "00000000-0000-7000-8000-000000000100";
			const reqId = "00000000-0000-7000-8000-000000000101";
			let postCount = 0;
			let getCount = 0;

			const skipCommand = {
				type: "skip_active_item" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					position: 0,
					work_id: mockItem.work_id,
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "owner_skip",
				},
			};

			const canonicalHash = payloadHash({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				command: skipCommand,
			});

			const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
				const url = String(input);
				const method = init?.method ?? "GET";
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
								result_sha256: "r".repeat(64),
								diagnostics: [],
							},
							command_type: "skip_active_item",
							request_id: reqId,
							correlation_id: "00000000-0000-7000-8000-000000000099",
							result: {
								type: "skip_active_item",
								grant: mockGrant,
								item: mockItem,
								reason: "owner_skip",
							},
						}),
						{ status: 200, headers: { "Content-Type": "application/json" } },
					);
				}
				return new Response("not found", { status: 404 });
			};

			const backend = createWorkBackend(
				{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			const intent = intentFingerprint("intent", WORKSPACE_ID, OWNER_ID, skipCommand.type, skipCommand.payload);
			const claim = await claimPendingOp(tempDir, intent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				operation_id: opId,
				request_id: reqId,
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: skipCommand,
			}));

			const claims = await backend.getCommittedSkipClaims!(GRANT_ID);

			expect(postCount).toBe(0);
			expect(getCount).toBe(1);
			expect(claims).toHaveLength(1);
			expect(claims[0]?.claimId).toBe(intent);
			expect(claims[0]?.command).toEqual(skipCommand);
			expect(claims[0]?.result).toEqual({
				type: "skip_active_item",
				grant: mockGrant,
				item: mockItem,
				reason: "owner_skip",
			});

			const updatedRecord = JSON.parse(await Bun.file(claim.path).text()) as {
				result?: { type: string };
				resolved_at?: string;
			};
			expect(updatedRecord.result).toBeDefined();
			expect(updatedRecord.result?.type).toBe("skip_active_item");
			expect(updatedRecord.resolved_at).toBeDefined();
		});

		test("already resolved claim: returned directly without GET or POST", async () => {
			let postCount = 0;
			let getCount = 0;

			const skipCommand = {
				type: "skip_active_item" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					position: 0,
					work_id: mockItem.work_id,
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "owner_skip",
				},
			};

			const mockFetch = async (): Promise<Response> => {
				throw new Error("fetch should not be called for resolved claim");
			};

			const backend = createWorkBackend(
				{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			const intent = intentFingerprint("intent", WORKSPACE_ID, OWNER_ID, skipCommand.type, skipCommand.payload);
			const claim = await claimPendingOp(tempDir, intent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				operation_id: "00000000-0000-7000-8000-000000000100",
				request_id: "00000000-0000-7000-8000-000000000101",
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: skipCommand,
			}));

			await resolvePendingOp(claim.path, claim.record!, {
				type: "skip_active_item",
				grant: mockGrant,
				item: mockItem,
				reason: "owner_skip",
			});

			const claims = await backend.getCommittedSkipClaims!(GRANT_ID);

			expect(postCount).toBe(0);
			expect(getCount).toBe(0);
			expect(claims).toHaveLength(1);
			expect(claims[0]?.claimId).toBe(intent);
			expect(claims[0]?.result.type).toBe("skip_active_item");
		});

		test("mismatched stored op: throws identity mismatch, bytes unchanged, zero POST", async () => {
			const opId = "00000000-0000-7000-8000-000000000110";
			const reqId = "00000000-0000-7000-8000-000000000111";
			let postCount = 0;
			let getCount = 0;

			const skipCommand = {
				type: "skip_active_item" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					position: 0,
					work_id: mockItem.work_id,
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "owner_skip",
				},
			};

			const differentHash = payloadHash({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				command: {
					...skipCommand,
					payload: { ...skipCommand.payload, reason: "different_reason" },
				},
			});

			const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
				const url = String(input);
				const method = init?.method ?? "GET";
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
								request_sha256: differentHash,
								result_sha256: "r".repeat(64),
								diagnostics: [],
							},
							command_type: "skip_active_item",
							request_id: reqId,
							correlation_id: "00000000-0000-7000-8000-000000000099",
							result: {
								type: "skip_active_item",
								grant: mockGrant,
								item: mockItem,
								reason: "different_reason",
							},
						}),
						{ status: 200, headers: { "Content-Type": "application/json" } },
					);
				}
				return new Response("not found", { status: 404 });
			};

			const backend = createWorkBackend(
				{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			const intent = intentFingerprint("intent", WORKSPACE_ID, OWNER_ID, skipCommand.type, skipCommand.payload);
			const claim = await claimPendingOp(tempDir, intent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				operation_id: opId,
				request_id: reqId,
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: skipCommand,
			}));
			const bytesBefore = await Bun.file(claim.path).text();

			let thrown: Error | null = null;
			try {
				await backend.getCommittedSkipClaims!(GRANT_ID);
			} catch (err) {
				thrown = err as Error;
			}

			expect(thrown).not.toBeNull();
			expect(thrown!.message).toContain(`unresolved pending claim ${claim.path}`);
			expect(thrown!.message).toContain("identity mismatch");
			expect(thrown!.message).toContain("automatic recovery refused; use stop/cancel or repair the claim");

			expect(postCount).toBe(0);
			expect(getCount).toBe(1);
			expect(await Bun.file(claim.path).text()).toBe(bytesBefore);
		});

		test("other grant/workspace/type claims ignored", async () => {
			const mockFetch = async (): Promise<Response> => {
				throw new Error("fetch should not be called for non-matching claims");
			};

			const backend = createWorkBackend(
				{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			// 1. Other grant
			const otherGrantCmd = {
				type: "skip_active_item" as const,
				payload: {
					grant_id: OTHER_GRANT_ID,
					expected_grant_version: 2,
					position: 0,
					work_id: mockItem.work_id,
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "owner_skip",
				},
			};
			const otherGrantIntent = intentFingerprint("intent", WORKSPACE_ID, OWNER_ID, otherGrantCmd.type, otherGrantCmd.payload);
			await claimPendingOp(tempDir, otherGrantIntent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				operation_id: "00000000-0000-7000-8000-000000000201",
				request_id: "00000000-0000-7000-8000-000000000202",
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: otherGrantCmd,
			}));

			// 2. Other workspace
			const otherWsCmd = {
				type: "skip_active_item" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					position: 0,
					work_id: mockItem.work_id,
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "owner_skip",
				},
			};
			const otherWsIntent = intentFingerprint("intent", OTHER_WORKSPACE_ID, OWNER_ID, otherWsCmd.type, otherWsCmd.payload);
			await claimPendingOp(tempDir, otherWsIntent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: OTHER_WORKSPACE_ID,
				operation_id: "00000000-0000-7000-8000-000000000203",
				request_id: "00000000-0000-7000-8000-000000000204",
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: otherWsCmd,
			}));

			// 3. Other type (e.g. set_execution_state)
			const otherTypeCmd = {
				type: "set_execution_state" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					target_state: "paused" as const,
					judge_sha256: "a".repeat(64),
				},
			};
			const otherTypeIntent = intentFingerprint("intent", WORKSPACE_ID, OWNER_ID, otherTypeCmd.type, otherTypeCmd.payload);
			await claimPendingOp(tempDir, otherTypeIntent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				operation_id: "00000000-0000-7000-8000-000000000205",
				request_id: "00000000-0000-7000-8000-000000000206",
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: otherTypeCmd,
			}));

			// 4. Filename mismatch
			const fileMismatchCmd = {
				type: "skip_active_item" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					position: 0,
					work_id: mockItem.work_id,
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "owner_skip",
				},
			};
			await claimPendingOp(tempDir, "wrong-intent-filename", () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				operation_id: "00000000-0000-7000-8000-000000000207",
				request_id: "00000000-0000-7000-8000-000000000208",
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: fileMismatchCmd,
			}));

			const claims = await backend.getCommittedSkipClaims!(GRANT_ID);
			expect(claims).toHaveLength(0);
		});

		test("unreadable claim in pendingDir: throws", async () => {
			const mockFetch = async (): Promise<Response> => {
				throw new Error("unexpected fetch");
			};

			const backend = createWorkBackend(
				{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				mockFetch as never,
				tempDir,
			);

			// Write unparseable JSON file
			await fs.writeFile(path.join(tempDir, "corrupted.json"), "{ invalid json");

			await expect(backend.getCommittedSkipClaims!(GRANT_ID)).rejects.toThrow("unreadable pending claim(s)");
		});
	});

	describe("acknowledgeSkipClaim", () => {
		test("removes file and repeat is no-op", async () => {
			const backend = createWorkBackend(
				{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				fetch,
				tempDir,
			);

			const skipCommand = {
				type: "skip_active_item" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					position: 0,
					work_id: mockItem.work_id,
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "owner_skip",
				},
			};

			const intent = intentFingerprint("intent", WORKSPACE_ID, OWNER_ID, skipCommand.type, skipCommand.payload);
			const claim = await claimPendingOp(tempDir, intent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				operation_id: "00000000-0000-7000-8000-000000000301",
				request_id: "00000000-0000-7000-8000-000000000302",
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: skipCommand,
			}));

			const claimId = path.basename(claim.path, ".json");
			expect(await Bun.file(claim.path).exists()).toBe(true);

			// First acknowledge removes the file
			await backend.acknowledgeSkipClaim!(claimId);
			expect(await Bun.file(claim.path).exists()).toBe(false);

			// Repeat acknowledge is no-op
			await expect(backend.acknowledgeSkipClaim!(claimId)).resolves.toBeUndefined();
		});

		test("non-skip claim is refused and left untouched", async () => {
			const backend = createWorkBackend(
				{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				fetch,
				tempDir,
			);

			const setStateCommand = {
				type: "set_execution_state" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					target_state: "paused" as const,
					judge_sha256: "a".repeat(64),
				},
			};

			const intent = intentFingerprint("intent", WORKSPACE_ID, OWNER_ID, setStateCommand.type, setStateCommand.payload);
			const claim = await claimPendingOp(tempDir, intent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE_ID,
				operation_id: "00000000-0000-7000-8000-000000000303",
				request_id: "00000000-0000-7000-8000-000000000304",
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: setStateCommand,
			}));

			const claimId = path.basename(claim.path, ".json");
			const bytesBefore = await Bun.file(claim.path).text();

			await expect(backend.acknowledgeSkipClaim!(claimId)).rejects.toThrow("not a skip_active_item claim");
			expect(await Bun.file(claim.path).text()).toBe(bytesBefore);
		});

		test("claim for different workspace is refused and left untouched", async () => {
			const backend = createWorkBackend(
				{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
				() => "mock-token",
				fetch,
				tempDir,
			);

			const skipCommand = {
				type: "skip_active_item" as const,
				payload: {
					grant_id: GRANT_ID,
					expected_grant_version: 2,
					position: 0,
					work_id: mockItem.work_id,
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "owner_skip",
				},
			};

			const intent = intentFingerprint("intent", OTHER_WORKSPACE_ID, OWNER_ID, skipCommand.type, skipCommand.payload);
			const claim = await claimPendingOp(tempDir, intent, () => ({
				api_version: "work.omp.dev/v1",
				workspace_id: OTHER_WORKSPACE_ID,
				operation_id: "00000000-0000-7000-8000-000000000305",
				request_id: "00000000-0000-7000-8000-000000000306",
				correlation_id: "00000000-0000-7000-8000-000000000099",
				command: skipCommand,
			}));

			const claimId = path.basename(claim.path, ".json");
			const bytesBefore = await Bun.file(claim.path).text();

			await expect(backend.acknowledgeSkipClaim!(claimId)).rejects.toThrow("not a skip_active_item claim for workspace");
			expect(await Bun.file(claim.path).text()).toBe(bytesBefore);
		});
	});
});
