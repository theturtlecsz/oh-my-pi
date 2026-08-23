import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { readdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { createWorkflowHost } from "../extensions/workflow/host";
import { ackOps, claimPendingOp, resolvePendingOp } from "../extensions/workflow/pending-ops";
import { createWorkBackend } from "../extensions/workflow/work";
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
});

