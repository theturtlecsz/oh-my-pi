import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test, vi } from "bun:test";
import type { ExecutionGrantItemClaim, ExecutionJudgeManifest, ExecutionProvenanceEnvelope } from "@oh-my-pi/pi-work-client";
import { createWorkBackend } from "../extensions/workflow/work";

describe("OMP-233 beginExecution grant id", () => {
	const tempDirs: string[] = [];

	afterEach(async () => {
		vi.restoreAllMocks();
		await Promise.all(tempDirs.splice(0).map(d => fs.rm(d, { recursive: true, force: true })));
	});

	const workspaceId = "00000000-0000-7000-8000-000000000001";
	const ownerId = "00000000-0000-7000-8000-000000000002";
	const workId = "00000000-0000-7000-8000-000000000010";
	const revisionId = "00000000-0000-7000-8000-000000000020";

	const provenance: ExecutionProvenanceEnvelope = {
		owner_input_id: "00000000-0000-7000-8000-000000000030",
		owner_session_id: "00000000-0000-7000-8000-000000000031",
		normalized_command: "/execute OMP-1",
		workspace_id: workspaceId,
		repository: "/tmp/repo",
		nonce: "00000000-0000-7000-8000-000000000032",
		issued_at: new Date().toISOString(),
	};
	const items: ExecutionGrantItemClaim[] = [
		{
			work_id: workId,
			revision_id: revisionId,
			position: 0,
			original_request: "Do the thing",
			original_request_sha256: "2".repeat(64),
			initial_git_baseline: "1".repeat(40),
		},
	];
	const judgeManifest: ExecutionJudgeManifest = {
		auditor_agent_sha256: "3".repeat(64),
		host_sha256: "4".repeat(64),
		adapter_sha256: "5".repeat(64),
		freeze_sha256: "6".repeat(64),
		runner_sha256: "7".repeat(64),
		executor_sha256: "8".repeat(64),
		contract_sha256: "9".repeat(64),
		service_fingerprint: "a".repeat(64),
		service_code_fingerprint: "b".repeat(64),
		service_migration_sha256: "c".repeat(64),
	};

	async function makeBackend() {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-233-grant-"));
		tempDirs.push(tempDir);
		const beginExecutionCommands: Array<{ grant_id: string }> = [];
		const mockFetch = async (url: RequestInfo | URL, init?: RequestInit) => {
			const u = String(url);
			if (u.includes("/v1/commands")) {
				const body = JSON.parse(String(init?.body)) as {
					command: { type: string; payload: { grant_id: string } };
				};
				if (body.command.type === "begin_execution") {
					beginExecutionCommands.push(body.command.payload);
					return new Response(
						JSON.stringify({
							receipt: { state: "applied", operation_id: crypto.randomUUID() },
							result: {
								type: "begin_execution",
								grant: {
									grant_id: body.command.payload.grant_id,
									workspace_id: workspaceId,
									owner_id: ownerId,
									repository: "/tmp/repo",
									remote_ref: "refs/heads/execution/omp-1",
									state: "active",
									mode: "single",
									grant_version: 1,
									max_continuations: 8,
									max_close_attempts: 5,
									max_no_progress: 3,
									continuations_scheduled: 0,
									authorization_hash: "d".repeat(64),
									judge_sha256: "e".repeat(64),
									created_at: new Date().toISOString(),
									expires_at: new Date(Date.now() + 86400000).toISOString(),
								},
								items: [],
							},
						}),
						{ status: 200 },
					);
				}
			}
			return new Response("not found", { status: 404 });
		};
		const backend = createWorkBackend(
			{ baseUrl: "http://127.0.0.1:9999", workspaceId, ownerId },
			() => "mock-token",
			mockFetch as unknown as typeof fetch,
			tempDir,
		);
		return { backend, beginExecutionCommands };
	}

	test("case a: sends the host-minted grant_id verbatim", async () => {
		const { backend, beginExecutionCommands } = await makeBackend();
		const grantId = "00000000-0000-7000-8000-0000000000aa";
		await backend.beginExecution({
			grantId,
			provenance,
			remoteRef: "refs/heads/execution/omp-1",
			mode: "single",
			items,
			expectedFocusVersion: 1,
			judgeSha256: "e".repeat(64),
			judgeManifest,
		});
		expect(beginExecutionCommands).toHaveLength(1);
		expect(beginExecutionCommands[0]?.grant_id).toBe(grantId);
	});

	test("case b: rejects a non-UUID grantId before any request", async () => {
		const { backend, beginExecutionCommands } = await makeBackend();
		await expect(
			backend.beginExecution({
				grantId: "grant-1",
				provenance,
				remoteRef: "refs/heads/execution/omp-1",
				mode: "single",
				items,
				expectedFocusVersion: 1,
				judgeSha256: "e".repeat(64),
				judgeManifest,
			}),
		).rejects.toThrow();
		expect(beginExecutionCommands).toHaveLength(0);
	});
});
