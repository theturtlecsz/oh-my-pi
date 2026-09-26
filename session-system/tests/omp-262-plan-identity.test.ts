import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test, vi } from "bun:test";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { EvidenceReceipt, ExecutionGrantItemView } from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import type { ExecutionSnapshot, NowRef, PlanStamp, WorkflowBackend } from "../extensions/workflow/backend";
import { executionRemoteRef } from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";
import { createWorkBackend, plannedCandidateId } from "../extensions/workflow/work";

describe("OMP-262 planned candidate identity", () => {
	const fixedWork = "work-fixed";
	const fixedRevision = "revision-fixed";
	const fixedPlanSha = "0".repeat(64);
	const expectedFixedId = "c2fe47bf-1f4c-5571-917f-99164bbf7be4";

	test("fixed vector equals pre-change stableId literal UUID", () => {
		const id = plannedCandidateId(fixedWork, fixedRevision, fixedPlanSha);
		expect(id).toBe(expectedFixedId);
	});

	test("changing only revision gives a different ID", () => {
		const baseId = plannedCandidateId(fixedWork, fixedRevision, fixedPlanSha);
		const changedRevisionId = plannedCandidateId(fixedWork, "revision-changed", fixedPlanSha);
		expect(changedRevisionId).not.toBe(baseId);
	});

	test("changing only plan hash gives a different ID", () => {
		const baseId = plannedCandidateId(fixedWork, fixedRevision, fixedPlanSha);
		const changedPlanId = plannedCandidateId(fixedWork, fixedRevision, "1".repeat(64));
		expect(changedPlanId).not.toBe(baseId);
	});
});

describe("stamp payload parity", () => {
	const tempDirs: string[] = [];

	afterEach(async () => {
		vi.restoreAllMocks();
		await Promise.all(tempDirs.splice(0).map(d => fs.rm(d, { recursive: true, force: true })));
	});

	const fixedWorkId = "00000000-0000-7000-8000-000000000010";
	const fixedRevisionId = "00000000-0000-7000-8000-000000000020";
	const fixedPlanBytes = "## Approach\n- Step 1\n- Step 2\n\n## Verification\n- Check 1\n";
	const fixedPlanSha = Bun.SHA256.hash(fixedPlanBytes, "hex");

	async function runManual(workId: string, revisionId: string, planBytes: string) {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-262-manual-"));
		tempDirs.push(tempDir);
		const key = "OMP-1";
		let capturedReceipt: EvidenceReceipt | undefined;

		const mockFetch = async (url: RequestInfo | URL, init?: RequestInit) => {
			const u = String(url);
			if (u.includes(`/v1/work-items/${key}`)) {
				return new Response(
					JSON.stringify({
						work_id: workId,
						workspace_id: "00000000-0000-7000-8000-000000000001",
						alias: { work_id: workId, key, primary: true, origin: "local" },
						state: "IN_PROGRESS",
						revision: {
							revision_id: revisionId,
							work_id: workId,
							revision_number: 1,
							title: "Work title",
							description: "Work description",
							scope: "scope",
							acceptance_criteria: ["Criteria 1"],
							created_at: new Date().toISOString(),
						},
						candidate: null,
						project_id: null,
						archived: false,
					}),
					{ status: 200 },
				);
			}
			if (u.includes("/v1/commands")) {
				const body = JSON.parse(String(init?.body)) as {
					command: { type: string; payload: { receipt?: EvidenceReceipt } };
				};
				if (body.command.type === "append_evidence") {
					capturedReceipt = body.command.payload.receipt;
					return new Response(
						JSON.stringify({
							receipt: { state: "applied", operation_id: crypto.randomUUID() },
							result: null,
						}),
						{ status: 200 },
					);
				}
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{
				baseUrl: "http://127.0.0.1:9999",
				workspaceId: "00000000-0000-7000-8000-000000000001",
				ownerId: "00000000-0000-7000-8000-000000000002",
			},
			() => "mock-token",
			mockFetch as unknown as typeof fetch,
			tempDir,
		);

		const planSha = Bun.SHA256.hash(planBytes, "hex");
		const stamp: PlanStamp = {
			hash: planSha,
			body: planBytes,
			title: "Plan title",
			planFilePath: "/fake/plan.md",
			approach: ["Step 1", "Step 2"],
			verification: ["Check 1"],
			paths: ["main.ts"],
		};
		const nowRef: NowRef = { id: workId, key, title: "Work title" };
		const outcome = await backend.stampPlan(nowRef, stamp);
		if (!capturedReceipt) throw new Error("append_evidence command was not captured");
		return { receipt: capturedReceipt, outcome };
	}

	async function runExecution(workId: string, revisionId: string, planBytes: string) {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-262-exec-"));
		tempDirs.push(tempDir);
		// computeAuditTcb discovers the installed `auditor` agent from the
		// filesystem; provide a project-level definition in the temp cwd so the
		// test never depends on the user's real ~/.omp installation.
		const agentsDir = path.join(tempDir, ".omp", "agents");
		await fs.mkdir(agentsDir, { recursive: true });
		await fs.writeFile(
			path.join(agentsDir, "auditor.md"),
			[
				"---",
				"name: auditor",
				"description: Test fixture auditor",
				"output:",
				"  properties:",
				"    report:",
				"      type: string",
				"---",
				"",
				"Test fixture auditor.",
				"",
			].join("\n"),
			"utf-8",
		);
		const planFilePath = path.join(tempDir, "plan.md");
		await fs.writeFile(planFilePath, planBytes, "utf-8");

		const execItem: ExecutionGrantItemView = {
			item_id: "item-1",
			workspace_id: "00000000-0000-7000-8000-000000000001",
			grant_id: "grant-1",
			work_id: workId,
			position: 0,
			phase: "planning",
			claimed_revision_id: revisionId,
			initial_git_baseline: "1".repeat(40),
			original_request: "Plan",
			original_request_sha256: "2".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};
		const execution: ExecutionSnapshot = {
			grant: {
				grant_id: "grant-1",
				workspace_id: "00000000-0000-7000-8000-000000000001",
				owner_id: "owner-1",
				repository: tempDir,
				remote_ref: executionRemoteRef("OMP-1", "grant-1"),
				state: "active",
				mode: "single",
				grant_version: 1,
				max_continuations: 8,
				max_close_attempts: 5,
				max_no_progress: 3,
				continuations_scheduled: 0,
				authorization_hash: "3".repeat(64),
				judge_sha256: "4".repeat(64),
				created_at: new Date().toISOString(),
				expires_at: new Date(Date.now() + 86400000).toISOString(),
			},
			items: [execItem],
			activeItem: execItem,
		};

		let executeTool:
			| ((
					id: string,
					params: { action: string; work?: string; plan_file?: string; paths?: string[] },
					signal: AbortSignal,
					onUpdate: undefined,
					ctx: ExtensionContext,
			  ) => Promise<{ content: Array<{ type: "text"; text: string }>; details: { success: boolean } }>)
			| undefined;

		const pi = {
			zod: z,
			registerTool: (def: { execute: typeof executeTool }) => {
				executeTool = def.execute;
			},
			registerCommand: () => {},
			registerFlag: () => {},
			registerMessageRenderer: () => {},
			on: () => {},
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
		} as unknown as ExtensionAPI;

		const hostBackend = {
			cacheFile: path.join(tempDir, "cache.json"),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			scopeFix: "",
			getExecution: async () => structuredClone(execution),
			findIssue: async () => ({ id: workId, key: "OMP-1", title: "Work title" }),
			stampExecutionPlan: async () => structuredClone(execution),
			workClient: {
				healthReady: async () => ({
					ready: true,
					contract_sha256: "contract-sha",
					service_fingerprint: "5".repeat(64),
					judge_manifest: { judge_sha256: "4".repeat(64) },
				}),
			},
		} as unknown as WorkflowBackend;

		const stampSpy = vi.spyOn(hostBackend, "stampExecutionPlan");

		createWorkflowHost({
			backend: hostBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(pi);

		const context = {
			cwd: tempDir,
			taskDepth: 0,
			ui: { notify: () => {}, setStatus: () => {}, theme: { fg: (_c: string, t: string) => t } },
		} as unknown as ExtensionContext;

		if (!executeTool) throw new Error("executeTool was not registered");

		const result = await executeTool(
			"planning-1",
			{
				action: "stamp_execution_plan",
				work: "OMP-1",
				plan_file: planFilePath,
				paths: ["main.ts"],
			},
			new AbortController().signal,
			undefined,
			context,
		);

		if (!result.details?.success) {
			throw new Error(`stamp_execution_plan failed: ${result.content?.[0]?.text}`);
		}

		type StampExecutionPlanInput = Parameters<WorkflowBackend["stampExecutionPlan"]>[0];
		const input = stampSpy.mock.calls[0]?.[0] as StampExecutionPlanInput | undefined;
		if (!input) throw new Error("stampExecutionPlan was not called");

		return { input, result };
	}

	test("case 1: manual payload captures candidate_id from append_evidence", async () => {
		const { receipt } = await runManual(fixedWorkId, fixedRevisionId, fixedPlanBytes);
		expect(receipt.candidate_id).toBeDefined();
		expect(typeof receipt.candidate_id).toBe("string");
	});

	test("case 2: execution payload drives stamp_execution_plan and captures candidateId", async () => {
		const { input } = await runExecution(fixedWorkId, fixedRevisionId, fixedPlanBytes);
		expect(input.candidateId).toBeDefined();
		expect(typeof input.candidateId).toBe("string");
	});

	test("case 3: 1 and 2 give the same ID, equal to plannedCandidateId(work, revision, sha256 hex of P)", async () => {
		const manual = await runManual(fixedWorkId, fixedRevisionId, fixedPlanBytes);
		const execution = await runExecution(fixedWorkId, fixedRevisionId, fixedPlanBytes);
		const expectedId = plannedCandidateId(fixedWorkId, fixedRevisionId, fixedPlanSha);

		expect(manual.receipt.candidate_id).toBe(execution.input.candidateId);
		expect(manual.receipt.candidate_id).toBe(expectedId);
		expect(execution.input.candidateId).toBe(expectedId);
	});

	test("case 4: changing only revision ID gives a different ID on both paths; changing only P gives a different ID on both paths", async () => {
		const baseManual = await runManual(fixedWorkId, fixedRevisionId, fixedPlanBytes);
		const baseExec = await runExecution(fixedWorkId, fixedRevisionId, fixedPlanBytes);

		// Changing only the revision ID
		const changedRevisionId = "00000000-0000-7000-8000-000000000099";
		const revManual = await runManual(fixedWorkId, changedRevisionId, fixedPlanBytes);
		const revExec = await runExecution(fixedWorkId, changedRevisionId, fixedPlanBytes);

		expect(revManual.receipt.candidate_id).not.toBe(baseManual.receipt.candidate_id);
		expect(revExec.input.candidateId).not.toBe(baseExec.input.candidateId);

		// Changing only P
		const changedPlanBytes = "## Approach\n- Changed approach\n\n## Verification\n- Changed verify\n";
		const planManual = await runManual(fixedWorkId, fixedRevisionId, changedPlanBytes);
		const planExec = await runExecution(fixedWorkId, fixedRevisionId, changedPlanBytes);

		expect(planManual.receipt.candidate_id).not.toBe(baseManual.receipt.candidate_id);
		expect(planExec.input.candidateId).not.toBe(baseExec.input.candidateId);
	});

	test("case 5: content SHA separate: captured candidate_sha256/candidateSha256 differs from candidate ID on both paths", async () => {
		const manual = await runManual(fixedWorkId, fixedRevisionId, fixedPlanBytes);
		const execution = await runExecution(fixedWorkId, fixedRevisionId, fixedPlanBytes);

		expect(manual.receipt.candidate_sha256).toBeDefined();
		expect(typeof manual.receipt.candidate_sha256).toBe("string");
		expect(manual.receipt.candidate_sha256).not.toBe(manual.receipt.candidate_id);

		expect(execution.input.candidateSha256).toBeDefined();
		expect(typeof execution.input.candidateSha256).toBe("string");
		expect(execution.input.candidateSha256).not.toBe(execution.input.candidateId);
	});
});
