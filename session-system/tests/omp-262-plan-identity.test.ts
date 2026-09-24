/**
 * OMP-262-s01: Deterministic planned candidate identity across manual and execution plans.
 *
 * This test verifies:
 * 1. Fixed vector determinism: plannedCandidateId("work-fixed", "revision-fixed", planSha) is deterministic.
 * 2. Unified identity: manual stampPlan (append_evidence) and execution-plan host command (stamp_execution_plan)
 *    emit identical candidate IDs for the same work ID, revision ID, and plan hash.
 * 3. Separate candidate content SHA: candidate_sha256 is kept distinct from candidate_id.
 * 4. Input sensitivity: changed revision ID or plan hash emits different candidate IDs in both command payloads.
 *
 * Note: OMP-262-s01 unifies client-side candidate identity across manual and execution host commands;
 * server-side candidate reuse in _stamp_execution_plan is governed by slice OMP-262-s02 as Python WorkService files
 * are explicitly excluded from this slice ("Do not touch: Python WorkService files").
 */
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
	type WorkItemView,
} from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import { createWorkflowHost } from "../extensions/workflow/host";
import * as pendingOps from "../extensions/workflow/pending-ops";
import { plannedCandidateId } from "../extensions/workflow/plan-identity";
import { createWorkBackend } from "../extensions/workflow/work";

const WORK_ID = "00000000-0000-7000-8000-000000000262";
const REVISION_ID = "00000000-0000-7000-8000-000000000263";
const REVISION_ID_CHANGED = "00000000-0000-7000-8000-000000000299";
const GRANT_ID = "00000000-0000-7000-8000-000000000264";
const WORKSPACE_ID = "00000000-0000-7000-8000-000000000265";
const OWNER_ID = "00000000-0000-7000-8000-000000000266";
const PLAN_BODY = "## Approach\n- Share planned identity\n\n## Verification\n- Compare command payloads\n";
const PLAN_BODY_CHANGED = "## Approach\n- Revised approach\n\n## Verification\n- Revised verification\n";

const cleanupPaths: string[] = [];

afterEach(() => {
	vi.restoreAllMocks();
	for (const target of cleanupPaths.splice(0)) {
		try {
			fs.rmSync(target, { recursive: true, force: true });
		} catch {
			// ignore cleanup errors in read-only sandboxes
		}
	}
});

describe("OMP-262 planned candidate identity", () => {
	test("fixed vector is deterministic and changes with revision or plan hash", () => {
		const planSha = "0123456789abcdef".repeat(4);
		const fixed = plannedCandidateId("work-fixed", "revision-fixed", planSha);

		expect(fixed).toBe("0b2dcd24-6fe5-5280-ab02-c06c2eb7516c");
		expect(plannedCandidateId("work-fixed", "revision-fixed", planSha)).toBe(fixed);
		expect(plannedCandidateId("work-fixed", "revision-changed", planSha)).not.toBe(fixed);
		expect(plannedCandidateId("work-fixed", "revision-fixed", "fedcba9876543210".repeat(4))).not.toBe(fixed);
		expect(plannedCandidateId("work-changed", "revision-fixed", planSha)).not.toBe(fixed);
	});

	test("manual and execution plan commands bind identical plan bytes to one candidate", async () => {
		let tempDir: string | undefined;
		try {
			tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-262-plan-identity-"));
			cleanupPaths.push(tempDir);
		} catch {
			// fallback if /tmp is read-only in sandbox
		}

		const planPath = tempDir ? path.join(tempDir, "plan.md") : "/tmp/omp-262-virtual-plan.md";
		const planPathChanged = tempDir ? path.join(tempDir, "plan-changed.md") : "/tmp/omp-262-virtual-plan-changed.md";

		if (tempDir) {
			fs.writeFileSync(planPath, PLAN_BODY);
			fs.writeFileSync(planPathChanged, PLAN_BODY_CHANGED);
		} else {
			// Fallback: mock claimPendingOp and readFileSync for read-only sandboxes
			vi.spyOn(pendingOps, "claimPendingOp").mockImplementation(async (_dir, _intent, createEnvelope) => {
				const envelope = createEnvelope();
				return { path: "virtual", owner: true, record: { envelope } };
			});
			const originalReadFileSync = fs.readFileSync;
			vi.spyOn(fs, "readFileSync").mockImplementation((p, ...args) => {
				if (p === planPath) return PLAN_BODY;
				if (p === planPathChanged) return PLAN_BODY_CHANGED;
				return (originalReadFileSync as Function)(p, ...args);
			});
		}

		const planSha = Bun.SHA256.hash(PLAN_BODY, "hex");
		const planShaChanged = Bun.SHA256.hash(PLAN_BODY_CHANGED, "hex");

		let currentRevisionId = REVISION_ID;
		const workItem: WorkItemView = {
			work_id: WORK_ID,
			workspace_id: WORKSPACE_ID,
			alias: { work_id: WORK_ID, key: "OMP-262", primary: true, origin: "local" },
			state: "IN_PROGRESS",
			revision: {
				revision_id: currentRevisionId,
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

		let currentActiveItem: ExecutionGrantItemView = {
			item_id: "00000000-0000-7000-8000-000000000267",
			workspace_id: WORKSPACE_ID,
			grant_id: GRANT_ID,
			work_id: WORK_ID,
			position: 0,
			phase: "planning",
			claimed_revision_id: currentRevisionId,
			criteria_revision_id: currentRevisionId,
			initial_git_baseline: "0".repeat(40),
			original_request: "Plan identity",
			original_request_sha256: "1".repeat(64),
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};

		let lastManualPayload: { receipt: EvidenceReceipt } | undefined;
		let lastExecutionPayload:
			| {
					grant_id: string;
					expected_grant_version: number;
					work_id: string;
					revision_id: string;
					candidate_id: string;
					plan_file: string;
					plan_body: string;
					plan_sha256: string;
					approach: string[];
					verification: string[];
					paths: string[];
					candidate_sha256: string;
					judge_sha256: string;
			  }
			| undefined;

		const fetcher = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = String(input);
			if (url.includes("/v1/work-items/OMP-262")) {
				return Response.json({
					...workItem,
					revision: { ...workItem.revision, revision_id: currentRevisionId },
				});
			}
			if (url.endsWith("/v1/health/ready")) {
				return Response.json({ service_fingerprint: "service-fingerprint" });
			}
			if (url.includes("/execution")) {
				return Response.json({
					grant,
					items: [currentActiveItem],
					active_item: currentActiveItem,
				});
			}
			if (url.endsWith("/v1/commands")) {
				const envelope = JSON.parse(String(init?.body)) as CommandEnvelope;
				if (envelope.command.type === "append_evidence") {
					lastManualPayload = envelope.command.payload as { receipt: EvidenceReceipt };
					return Response.json({
						applied: true,
						result: { type: "append_evidence", receipt: lastManualPayload.receipt },
					});
				}
				if (envelope.command.type === "stamp_execution_plan") {
					lastExecutionPayload = envelope.command.payload as typeof lastExecutionPayload;
					currentActiveItem = { ...currentActiveItem, phase: "executing" };
					return Response.json({
						applied: true,
						result: {
							type: "stamp_execution_plan",
							grant: { ...grant, grant_version: grant.grant_version + 1 },
							item: currentActiveItem,
						},
					});
				}
				throw new Error(`unexpected command ${envelope.command.type}`);
			}
			return new Response("not found", { status: 404 });
		};

		const backend = createWorkBackend(
			{ baseUrl: "http://127.0.0.1:9999", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
			() => "token",
			fetcher,
			tempDir ?? "/tmp/omp-262-pending-ops",
		);

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

		createWorkflowHost({ backend, teamNoun: "ledger", entryType: "work-now", acceptEntry: () => true })(pi);

		const runHostExecutionStamp = async (targetPlanPath: string) => {
			return executeTool!(
				"stamp-execution-plan",
				{ action: "stamp_execution_plan", work: "OMP-262", plan_file: targetPlanPath, paths: [] },
				new AbortController().signal,
				undefined,
				{ cwd: grant.repository, taskDepth: 0, ui: { notify: () => {}, theme: { fg: (_c: string, text: string) => text }, setStatus: () => {} } } as unknown as ExtensionContext,
			);
		};

		// --- 1. Baseline: identical work/revision/plan bytes ---
		currentRevisionId = REVISION_ID;
		currentActiveItem = {
			...currentActiveItem,
			phase: "planning",
			claimed_revision_id: REVISION_ID,
			criteria_revision_id: REVISION_ID,
		};

		await backend.stampPlan(
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

		const baselineManual = lastManualPayload!;
		expect(baselineManual).toBeDefined();

		const baselineHostResult = await runHostExecutionStamp(planPath);
		expect(baselineHostResult.content[0]?.text).toContain("plan stamped successfully");
		const baselineExecution = lastExecutionPayload!;
		expect(baselineExecution).toBeDefined();

		// Prove identical work/revision/plan bytes emit identical candidate ID in both command payloads
		expect(baselineExecution.candidate_id).toBe(baselineManual.receipt.candidate_id);
		expect(baselineExecution.candidate_id).toBe(plannedCandidateId(WORK_ID, REVISION_ID, planSha));
		// Keep candidate content SHA separate from candidate ID
		expect(baselineExecution.candidate_sha256).toBe(
			sha256Hex(canonicalJson({ planSha, candidateId: baselineExecution.candidate_id })),
		);
		expect(baselineExecution.candidate_sha256).not.toBe(baselineExecution.candidate_id);
		expect(baselineManual.receipt.candidate_sha256).not.toBe(baselineManual.receipt.candidate_id);

		// --- 2. Changed revision emits different candidate IDs in both command payloads ---
		currentRevisionId = REVISION_ID_CHANGED;
		currentActiveItem = {
			...currentActiveItem,
			phase: "planning",
			claimed_revision_id: REVISION_ID_CHANGED,
			criteria_revision_id: REVISION_ID_CHANGED,
		};

		await backend.stampPlan(
			{ id: WORK_ID, key: "OMP-262", title: "Manual title rev changed" },
			{
				title: "Manual title rev changed",
				body: PLAN_BODY,
				planFilePath: "manual-plan.md",
				hash: planSha,
				approach: ["Share planned identity"],
				verification: ["Compare command payloads"],
			},
		);
		const revChangedManual = lastManualPayload!;

		await runHostExecutionStamp(planPath);
		const revChangedExecution = lastExecutionPayload!;

		expect(revChangedExecution.candidate_id).toBe(revChangedManual.receipt.candidate_id);
		expect(revChangedExecution.candidate_id).toBe(plannedCandidateId(WORK_ID, REVISION_ID_CHANGED, planSha));
		expect(revChangedExecution.candidate_id).not.toBe(baselineExecution.candidate_id);
		expect(revChangedManual.receipt.candidate_id).not.toBe(baselineManual.receipt.candidate_id);

		// --- 3. Changed plan hash emits different candidate IDs in both command payloads ---
		currentRevisionId = REVISION_ID;
		currentActiveItem = {
			...currentActiveItem,
			phase: "planning",
			claimed_revision_id: REVISION_ID,
			criteria_revision_id: REVISION_ID,
		};

		await backend.stampPlan(
			{ id: WORK_ID, key: "OMP-262", title: "Manual title plan changed" },
			{
				title: "Manual title plan changed",
				body: PLAN_BODY_CHANGED,
				planFilePath: "manual-plan-changed.md",
				hash: planShaChanged,
				approach: ["Revised approach"],
				verification: ["Revised verification"],
			},
		);
		const planChangedManual = lastManualPayload!;

		await runHostExecutionStamp(planPathChanged);
		const planChangedExecution = lastExecutionPayload!;

		expect(planChangedExecution.candidate_id).toBe(planChangedManual.receipt.candidate_id);
		expect(planChangedExecution.candidate_id).toBe(plannedCandidateId(WORK_ID, REVISION_ID, planShaChanged));
		expect(planChangedExecution.candidate_id).not.toBe(baselineExecution.candidate_id);
		expect(planChangedManual.receipt.candidate_id).not.toBe(baselineManual.receipt.candidate_id);
	});
});
