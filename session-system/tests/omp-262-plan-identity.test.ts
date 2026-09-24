import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test, vi } from "bun:test";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import {
	canonicalJson,
	sha256Hex,
	type CommandEnvelope,
	type ExecutionGrantItemView,
	type ExecutionGrantView,
	type EvidenceReceipt,
} from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import { createWorkflowHost } from "../extensions/workflow/host";
import { createWorkBackend, plannedCandidateId } from "../extensions/workflow/work";

const WORK_ID = "00000000-0000-7000-8000-000000000262";
const REVISION_ID = "00000000-0000-7000-8000-000000000263";
const GRANT_ID = "00000000-0000-7000-8000-000000000264";
const WORKSPACE_ID = "00000000-0000-7000-8000-000000000265";
const OWNER_ID = "00000000-0000-7000-8000-000000000266";
const PLAN_BODY = "## Approach\n- Share planned identity\n\n## Verification\n- Compare command payloads\n";

const cleanupPaths: string[] = [];

afterEach(() => {
	vi.restoreAllMocks();
	for (const target of cleanupPaths.splice(0)) fs.rmSync(target, { recursive: true, force: true });
});

describe("OMP-262 planned candidate identity", () => {
	test("fixed vector is deterministic and changes with revision or plan hash", () => {
		const planSha = "0123456789abcdef".repeat(4);
		const fixed = plannedCandidateId("work-fixed", "revision-fixed", planSha);

		expect(fixed).toBe("0b2dcd24-6fe5-5280-ab02-c06c2eb7516c");
		expect(plannedCandidateId("work-fixed", "revision-fixed", planSha)).toBe(fixed);
		expect(plannedCandidateId("work-fixed", "revision-changed", planSha)).not.toBe(fixed);
		expect(plannedCandidateId("work-fixed", "revision-fixed", "fedcba9876543210".repeat(4))).not.toBe(fixed);
	});

	test("manual and execution plan commands bind identical plan bytes to one candidate", async () => {
		const pendingDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-262-plan-identity-"));
		cleanupPaths.push(pendingDir);
		const planPath = path.join(pendingDir, "plan.md");
		await Bun.write(planPath, PLAN_BODY);
		const planSha = Bun.SHA256.hash(PLAN_BODY, "hex");
		let manualPayload: { receipt: EvidenceReceipt } | undefined;

		const item = {
			work_id: WORK_ID,
			workspace_id: WORKSPACE_ID,
			alias: { work_id: WORK_ID, key: "OMP-262", primary: true, origin: "local" },
			state: "IN_PROGRESS",
			revision: {
				revision_id: REVISION_ID,
				revision_number: 1,
				title: "Plan identity",
				description: "",
				scope: "",
				acceptance_criteria: [],
				content_sha256: "0".repeat(64),
				created_by: OWNER_ID,
				created_at: "2026-09-24T00:00:00.000Z",
			},
			candidate: null,
			project_id: null,
			archived: false,
		};
		const fetcher = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (url.endsWith("/v1/work-items/OMP-262")) return Response.json(item);
			if (url.endsWith("/v1/health/ready")) return Response.json({ service_fingerprint: "service-fingerprint" });
			if (url.endsWith("/v1/commands")) {
				const envelope = JSON.parse(String(init?.body)) as CommandEnvelope;
				if (envelope.command.type !== "append_evidence") throw new Error(`unexpected command ${envelope.command.type}`);
				manualPayload = envelope.command.payload;
				return Response.json({ applied: true, result: { type: "append_evidence", receipt: manualPayload.receipt } });
			}
			return new Response("not found", { status: 404 });
		};
		const manualBackend = createWorkBackend(
			{ baseUrl: "http://127.0.0.1:9999", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
			() => "token",
			fetcher,
			pendingDir,
		);
		await manualBackend.stampPlan(
			{ id: WORK_ID, key: "OMP-262", title: "Manual title" },
			{
				title: "Manual title",
				body: PLAN_BODY,
				planFilePath: "manual-plan.md",
				hash: planSha,
				approach: ["Share planned identity"],
				verification: ["Compare command payloads"],
			},
		);

		const grant: ExecutionGrantView = {
			grant_id: GRANT_ID,
			workspace_id: WORKSPACE_ID,
			owner_id: OWNER_ID,
			repository: path.resolve(import.meta.dir, "../.."),
			remote_ref: "refs/heads/main",
			state: "active",
			mode: "single",
			grant_version: 1,
			max_continuations: 8,
			max_close_attempts: 5,
			max_no_progress: 3,
			continuations_scheduled: 0,
			authorization_hash: "authorization-hash",
			judge_sha256: "judge-sha",
			created_at: "2026-09-24T00:00:00.000Z",
			expires_at: "2026-09-25T00:00:00.000Z",
		};
		const activeItem: ExecutionGrantItemView = {
			item_id: "00000000-0000-7000-8000-000000000267",
			workspace_id: WORKSPACE_ID,
			grant_id: GRANT_ID,
			work_id: WORK_ID,
			position: 0,
			phase: "planning",
			claimed_revision_id: REVISION_ID,
			criteria_revision_id: REVISION_ID,
			initial_git_baseline: "0".repeat(40),
			original_request: "Plan identity",
			original_request_sha256: "1".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};
		const execution: ExecutionSnapshot = { grant, items: [activeItem], activeItem };
		let executionPayload: Parameters<WorkflowBackend["stampExecutionPlan"]>[0] | undefined;
		const executionBackend = {
			...manualBackend,
			getExecution: async () => execution,
			stampExecutionPlan: async (payload: Parameters<WorkflowBackend["stampExecutionPlan"]>[0]) => {
				executionPayload = payload;
				return execution;
			},
		} as WorkflowBackend;
		let executeTool: ((id: string, params: unknown, signal: AbortSignal, onUpdate: unknown, ctx: ExtensionContext) => Promise<{ content: Array<{ text: string }> }>) | undefined;
		const pi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			registerTool: (definition: { execute: typeof executeTool }) => { executeTool = definition.execute; },
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
			zod: z,
		} as unknown as ExtensionAPI;
		vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
			agents: [{
				name: "auditor",
				description: "Auditor",
				systemPrompt: "Audit",
				model: ["@audit"],
				output: { properties: { report: { type: "string" } } },
				source: "bundled",
			}],
			projectAgentsDir: null,
		});
		createWorkflowHost({ backend: executionBackend, teamNoun: "ledger", entryType: "work-now", acceptEntry: () => true })(pi);
		const result = await executeTool!(
			"stamp-execution-plan",
			{ action: "stamp_execution_plan", work: "OMP-262", plan_file: planPath, paths: [] },
			new AbortController().signal,
			undefined,
			{ cwd: grant.repository, taskDepth: 0, ui: { notify: () => {}, theme: { fg: (_color: string, text: string) => text }, setStatus: () => {} } } as unknown as ExtensionContext,
		);

		expect(result.content[0]?.text).toContain("plan stamped successfully");
		expect(manualPayload?.receipt.candidate_id).toBeDefined();
		expect(executionPayload?.candidateId).toBe(manualPayload?.receipt.candidate_id);
		expect(executionPayload?.candidateId).toBe(plannedCandidateId(WORK_ID, REVISION_ID, planSha));
		expect(executionPayload?.candidateSha256).toBe(
			sha256Hex(canonicalJson({ planSha, candidateId: executionPayload?.candidateId })),
		);
		expect(executionPayload?.candidateSha256).not.toBe(executionPayload?.candidateId);
	});
});
