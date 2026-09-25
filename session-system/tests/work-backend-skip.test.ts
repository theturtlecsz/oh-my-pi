import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import type {
	ExecutionGrantItemView,
	ExecutionGrantView,
	ExecutionView,
} from "@oh-my-pi/pi-work-client";
import { createWorkBackend } from "../extensions/workflow/work";

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";
const BASE_URL = "http://127.0.0.1:9999";

const mockGrant: ExecutionGrantView = {
	grant_id: "00000000-0000-7000-8000-000000000010",
	workspace_id: WORKSPACE_ID,
	owner_id: OWNER_ID,
	repository: "theturtlecsz/oh-my-pi",
	remote_ref: "refs/heads/main",
	state: "active",
	mode: "queue",
	grant_version: 4,
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
	grant_id: mockGrant.grant_id,
	work_id: "00000000-0000-7000-8000-000000000030",
	position: 1,
	phase: "skipped",
	claimed_revision_id: "00000000-0000-7000-8000-000000000040",
	initial_git_baseline: "c".repeat(40),
	original_request: "do work",
	original_request_sha256: "d".repeat(64),
	close_attempts_started: 0,
	consecutive_no_progress: 0,
	skipped_at: "2026-09-25T10:05:00Z",
	terminal_reason: "defer",
};

describe("WorkflowBackend.skipActiveItem", () => {
	let tempDir: string;

	beforeEach(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "work-backend-skip-"));
	});

	afterEach(async () => {
		await fs.rm(tempDir, { recursive: true, force: true });
	});

	test("sends exact snake_case payload fields and trims surrounding whitespace from reason", async () => {
		let capturedMethod: string | undefined;
		let capturedUrl: string | undefined;
		let capturedBody: Record<string, unknown> | undefined;

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			const method = init?.method ?? "GET";
			if (method === "POST" && url.endsWith("/v1/commands")) {
				capturedMethod = method;
				capturedUrl = url;
				capturedBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
				const envelope = capturedBody;
				return Response.json({
					receipt: {
						operation_id: envelope.operation_id,
						request_id: envelope.request_id,
						state: "applied",
						request_sha256: "0".repeat(64),
						result_sha256: "1".repeat(64),
						diagnostics: [],
					},
					result: {
						type: "skip_active_item",
						grant: mockGrant,
						item: mockItem,
						reason: "defer",
					},
				});
			}
			if (method === "GET" && url.includes(`/v1/workspaces/${WORKSPACE_ID}/execution/`)) {
				const view: ExecutionView = {
					grant: mockGrant,
					items: [mockItem],
					active_item: null,
				};
				return Response.json(view);
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
			() => "test-token",
			mockFetch as never,
			tempDir,
		);

		await backend.skipActiveItem({
			grantId: mockGrant.grant_id,
			expectedGrantVersion: 3,
			position: 1,
			workId: mockItem.work_id,
			expectedFocusVersion: 2,
			judgeSha256: "a".repeat(64),
			reason: "  defer  ",
		});

		expect(capturedMethod).toBe("POST");
		expect(capturedUrl).toBe(`${BASE_URL}/v1/commands`);
		const command = capturedBody?.command as { type: string; payload: Record<string, unknown> };
		expect(command.type).toBe("skip_active_item");
		expect(command.payload).toEqual({
			grant_id: mockGrant.grant_id,
			expected_grant_version: 3,
			position: 1,
			work_id: mockItem.work_id,
			expected_focus_version: 2,
			judge_sha256: "a".repeat(64),
			reason: "defer",
		});
	});

	test("issues follow-up GET on the grant's execution URL and uses GET items and active_item in snapshot", async () => {
		const requests: Array<{ method: string; url: string }> = [];

		const getActiveItem: ExecutionGrantItemView = {
			...mockItem,
			item_id: "00000000-0000-7000-8000-000000000099",
			position: 2,
			phase: "active",
		};
		const getItems: ExecutionGrantItemView[] = [mockItem, getActiveItem];

		const postResultGrant: ExecutionGrantView = {
			...mockGrant,
			grant_version: 5,
		};

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			const method = init?.method ?? "GET";
			requests.push({ method, url });

			if (method === "POST" && url.endsWith("/v1/commands")) {
				const envelope = JSON.parse(String(init?.body)) as { operation_id: string; request_id: string };
				return Response.json({
					receipt: {
						operation_id: envelope.operation_id,
						request_id: envelope.request_id,
						state: "applied",
						request_sha256: "0".repeat(64),
						result_sha256: "1".repeat(64),
						diagnostics: [],
					},
					result: {
						type: "skip_active_item",
						grant: postResultGrant,
						item: mockItem,
						reason: "defer",
					},
				});
			}
			if (method === "GET" && url === `${BASE_URL}/v1/workspaces/${WORKSPACE_ID}/execution/${encodeURIComponent(mockGrant.grant_id)}`) {
				const view: ExecutionView = {
					grant: mockGrant,
					items: getItems,
					active_item: getActiveItem,
				};
				return Response.json(view);
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
			() => "test-token",
			mockFetch as never,
			tempDir,
		);

		const snapshot = await backend.skipActiveItem({
			grantId: mockGrant.grant_id,
			expectedGrantVersion: 4,
			position: 1,
			workId: mockItem.work_id,
			expectedFocusVersion: 2,
			judgeSha256: "a".repeat(64),
			reason: "defer",
		});

		expect(requests).toHaveLength(2);
		expect(requests[0]).toEqual({ method: "POST", url: `${BASE_URL}/v1/commands` });
		expect(requests[1]).toEqual({
			method: "GET",
			url: `${BASE_URL}/v1/workspaces/${WORKSPACE_ID}/execution/${mockGrant.grant_id}`,
		});

		expect(snapshot.grant).toEqual(postResultGrant);
		expect(snapshot.items).toEqual(getItems);
		expect(snapshot.activeItem).toEqual(getActiveItem);
	});

	test("409 rejection throws and no GET is issued", async () => {
		const requests: Array<{ method: string; url: string }> = [];

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			const method = init?.method ?? "GET";
			requests.push({ method, url });

			if (method === "POST" && url.endsWith("/v1/commands")) {
				return Response.json(
					{
						error: {
							code: "grant_version_mismatch",
							request_id: "00000000-0000-7000-8000-0000000000b1",
							correlation_id: "00000000-0000-7000-8000-0000000000c1",
							diagnostics: ["expected grant version 3, but grant is at version 4"],
						},
					},
					{ status: 409 },
				);
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
			() => "test-token",
			mockFetch as never,
			tempDir,
		);

		await expect(
			backend.skipActiveItem({
				grantId: mockGrant.grant_id,
				expectedGrantVersion: 3,
				position: 1,
				workId: mockItem.work_id,
				expectedFocusVersion: 2,
				judgeSha256: "a".repeat(64),
				reason: "defer",
			}),
		).rejects.toThrow();

		expect(requests).toHaveLength(1);
		expect(requests[0]?.method).toBe("POST");
		const getRequests = requests.filter(r => r.method === "GET");
		expect(getRequests).toHaveLength(0);
	});

	test("mismatched result type throws unexpected result and issues no GET", async () => {
		const requests: Array<{ method: string; url: string }> = [];

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			const method = init?.method ?? "GET";
			requests.push({ method, url });

			if (method === "POST" && url.endsWith("/v1/commands")) {
				const envelope = JSON.parse(String(init?.body)) as { operation_id: string; request_id: string };
				return Response.json({
					receipt: {
						operation_id: envelope.operation_id,
						request_id: envelope.request_id,
						state: "applied",
						request_sha256: "0".repeat(64),
						result_sha256: "1".repeat(64),
						diagnostics: [],
					},
					result: {
						type: "complete_execution_item",
						grant: mockGrant,
						items: [mockItem],
						activeItem: mockItem,
					},
				});
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{ baseUrl: BASE_URL, workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
			() => "test-token",
			mockFetch as never,
			tempDir,
		);

		await expect(
			backend.skipActiveItem({
				grantId: mockGrant.grant_id,
				expectedGrantVersion: 4,
				position: 1,
				workId: mockItem.work_id,
				expectedFocusVersion: 2,
				judgeSha256: "a".repeat(64),
				reason: "defer",
			}),
		).rejects.toThrow("unexpected result complete_execution_item");

		expect(requests).toHaveLength(1);
		expect(requests[0]?.method).toBe("POST");
		const getRequests = requests.filter(r => r.method === "GET");
		expect(getRequests).toHaveLength(0);
	});
});
