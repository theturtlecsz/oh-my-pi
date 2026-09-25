import { expect, test } from "bun:test";
import {
	type ExecutionGrantItemView,
	type ExecutionGrantView,
	type SkipActiveItemPayload,
	WorkClient,
	WorkError,
} from "../src/index";

const ENV = {
	api_version: "work.omp.dev/v1" as const,
	workspace_id: "00000000-0000-0000-0000-000000000001",
	operation_id: "00000000-0000-0000-0000-0000000000a1",
	request_id: "00000000-0000-0000-0000-0000000000b1",
	correlation_id: "00000000-0000-0000-0000-0000000000c1",
};

const RECEIPT = {
	operation_id: "00000000-0000-0000-0000-0000000000a1",
	request_id: "00000000-0000-0000-0000-0000000000b1",
	state: "applied" as const,
	request_sha256: "0".repeat(64),
	result_sha256: "1".repeat(64),
	diagnostics: [],
};

test("request body for a skip_active_item command carries all seven payload fields unchanged", async () => {
	let requestBody: Record<string, unknown> | undefined;
	const payload: SkipActiveItemPayload = {
		grant_id: "00000000-0000-0000-0000-000000000001",
		expected_grant_version: 3,
		position: 2,
		work_id: "00000000-0000-0000-0000-000000000002",
		expected_focus_version: 5,
		judge_sha256: "a".repeat(64),
		reason: "deferring active item for triage",
	};

	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (_input, init) => {
			requestBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
			return Response.json({
				receipt: RECEIPT,
				result: {
					type: "skip_active_item",
					grant: {
						grant_id: payload.grant_id,
						workspace_id: ENV.workspace_id,
						owner_id: "00000000-0000-0000-0000-000000000009",
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
						judge_sha256: payload.judge_sha256,
						created_at: "2026-09-25T10:00:00Z",
						expires_at: "2026-09-25T11:00:00Z",
					},
					item: {
						item_id: "00000000-0000-0000-0000-000000000003",
						workspace_id: ENV.workspace_id,
						grant_id: payload.grant_id,
						work_id: payload.work_id,
						position: payload.position,
						phase: "skipped",
						claimed_revision_id: "00000000-0000-0000-0000-000000000004",
						initial_git_baseline: "c".repeat(40),
						original_request: "do work",
						original_request_sha256: "d".repeat(64),
						close_attempts_started: 0,
						consecutive_no_progress: 0,
						skipped_at: "2026-09-25T10:05:00Z",
						terminal_reason: payload.reason,
					},
					reason: payload.reason,
				},
			});
		},
	);

	await client.execute({
		...ENV,
		command: {
			type: "skip_active_item",
			payload,
		},
	});

	expect(requestBody).toBeDefined();
	const command = requestBody?.command as { type: string; payload: Record<string, unknown> };
	expect(command.type).toBe("skip_active_item");
	expect(command.payload).toEqual({
		grant_id: "00000000-0000-0000-0000-000000000001",
		expected_grant_version: 3,
		position: 2,
		work_id: "00000000-0000-0000-0000-000000000002",
		expected_focus_version: 5,
		judge_sha256: "a".repeat(64),
		reason: "deferring active item for triage",
	});
});

test("200 response narrows on result.type === 'skip_active_item' to grant, item, and reason", async () => {
	const grant: ExecutionGrantView = {
		grant_id: "00000000-0000-0000-0000-000000000001",
		workspace_id: ENV.workspace_id,
		owner_id: "00000000-0000-0000-0000-000000000009",
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

	const item: ExecutionGrantItemView = {
		item_id: "00000000-0000-0000-0000-000000000003",
		workspace_id: ENV.workspace_id,
		grant_id: grant.grant_id,
		work_id: "00000000-0000-0000-0000-000000000002",
		position: 0,
		phase: "skipped",
		claimed_revision_id: "00000000-0000-0000-0000-000000000004",
		initial_git_baseline: "c".repeat(40),
		original_request: "do work",
		original_request_sha256: "d".repeat(64),
		close_attempts_started: 0,
		consecutive_no_progress: 0,
		skipped_at: "2026-09-25T10:05:00Z",
		terminal_reason: "skip this item",
	};

	const reason = "skip this item";

	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async () =>
			Response.json({
				receipt: RECEIPT,
				result: {
					type: "skip_active_item",
					grant,
					item,
					reason,
				},
			}),
	);

	const res = await client.execute({
		...ENV,
		command: {
			type: "skip_active_item",
			payload: {
				grant_id: grant.grant_id,
				expected_grant_version: 3,
				position: 0,
				work_id: item.work_id,
				expected_focus_version: 1,
				judge_sha256: "a".repeat(64),
				reason,
			},
		},
	});

	if (res.result.type !== "skip_active_item") {
		throw new Error(`expected skip_active_item result, got ${res.result.type}`);
	}

	expect(res.result.type).toBe("skip_active_item");
	expect(res.result.grant).toEqual(grant);
	expect(res.result.item).toEqual(item);
	expect(res.result.reason).toBe(reason);
});

test("409 service rejection surfaces as the client's typed error", async () => {
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async () =>
			Response.json(
				{
					error: {
						code: "grant_version_mismatch",
						request_id: ENV.request_id,
						correlation_id: ENV.correlation_id,
						diagnostics: ["expected grant version 3, but grant is at version 4"],
					},
				},
				{ status: 409 },
			),
	);

	let rejection: unknown;
	try {
		await client.execute({
			...ENV,
			command: {
				type: "skip_active_item",
				payload: {
					grant_id: "00000000-0000-0000-0000-000000000001",
					expected_grant_version: 3,
					position: 0,
					work_id: "00000000-0000-0000-0000-000000000002",
					expected_focus_version: 1,
					judge_sha256: "a".repeat(64),
					reason: "defer item",
				},
			},
		});
	} catch (error) {
		rejection = error;
	}

	expect(rejection).toBeInstanceOf(WorkError);
	const workError = rejection as WorkError;
	expect(workError.code).toBe("grant_version_mismatch");
	expect(workError.status).toBe(409);
	expect(workError.requestId).toBe(ENV.request_id);
	expect(workError.diagnostics).toEqual(["expected grant version 3, but grant is at version 4"]);
	expect(workError.message).toContain(
		"grant_version_mismatch (HTTP 409): expected grant version 3, but grant is at version 4",
	);
});
