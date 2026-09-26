import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import * as taskModule from "@oh-my-pi/pi-coding-agent/task";
import type { ExecutionGrantItemView } from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import { executionRemoteRef } from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";

const GRANT_ID = "00000000-0000-7000-8000-000000000237";
const ANCHOR_KEY = "OMP-237";
const CANONICAL_REF = executionRemoteRef(ANCHOR_KEY, GRANT_ID);
const ALIAS_REF = "refs/heads/execution/omp-238";
const WORK_UUID = "00000000-0000-7000-8000-000000000010";

type ToolResult = { content: Array<{ type: "text"; text: string }>; details: { success: boolean } };
type ToolHandler = (
	id: string,
	params: { action: string; work?: string; criteria?: string[]; plan_file?: string; paths?: string[] },
	signal: AbortSignal,
	onUpdate: undefined,
	ctx: ExtensionContext,
) => Promise<ToolResult>;

const directories: string[] = [];

beforeEach(() => {
	vi.spyOn(taskModule, "discoverAgents").mockResolvedValue({
		agents: [{
			name: "auditor",
			description: "Auditor agent",
			systemPrompt: "Audit prompt",
			model: ["@audit"],
			output: { properties: { report: { type: "string" } } },
			source: "bundled",
		}],
		projectAgentsDir: null,
	});
});

afterEach(async () => {
	vi.restoreAllMocks();
	await Promise.all(directories.splice(0).map(directory => fs.promises.rm(directory, { recursive: true, force: true })));
});

async function makeHarness(options: {
	remoteRef: string;
	state?: ExecutionSnapshot["grant"]["state"];
	workId?: string;
	phase?: ExecutionGrantItemView["phase"];
	findIssue?: WorkflowBackend["findIssue"];
}) {
	const directory = await fs.promises.mkdtemp(path.join(os.tmpdir(), "execution-remote-ref-planning-"));
	directories.push(directory);
	await fs.promises.mkdir(path.join(directory, "src"));
	const planPath = path.join(directory, "plan.md");
	await fs.promises.writeFile(planPath, "## Approach\n1. Write the change\n\n## Verification\n1. Run the focused test\n");
	const workId = options.workId ?? ANCHOR_KEY;
	const item: ExecutionGrantItemView = {
		item_id: "item-1",
		workspace_id: "00000000-0000-7000-8000-000000000001",
		grant_id: GRANT_ID,
		work_id: workId,
		position: 0,
		phase: options.phase ?? "criteria_pending",
		claimed_revision_id: "00000000-0000-7000-8000-000000000020",
		initial_git_baseline: "1".repeat(40),
		original_request: "Ship the change",
		original_request_sha256: "2".repeat(64),
		close_attempts_started: 0,
		consecutive_no_progress: 0,
	};
	const execution: ExecutionSnapshot = {
		grant: {
			grant_id: GRANT_ID,
			workspace_id: "00000000-0000-7000-8000-000000000001",
			owner_id: "00000000-0000-7000-8000-000000000002",
			repository: directory,
			remote_ref: options.remoteRef,
			state: options.state ?? "active",
			mode: "single",
			grant_version: 1,
			max_continuations: 8,
			max_close_attempts: 5,
			max_no_progress: 3,
			continuations_scheduled: 0,
			authorization_hash: "3".repeat(64),
			judge_sha256: "4".repeat(64),
			created_at: "2026-09-26T00:00:00Z",
			expires_at: "2026-09-27T00:00:00Z",
		},
		items: [item],
		activeItem: item,
	};
	const sealExecutionCriteria = vi.fn(async () => ({
		sealedCriteria: ["AC-1"],
		grant: structuredClone(execution.grant),
		items: structuredClone(execution.items),
		activeItem: structuredClone(execution.activeItem),
	}));
	const stampExecutionPlan = vi.fn(async () => structuredClone(execution));
	const healthReady = vi.fn(async () => ({ ready: true, service_fingerprint: "5".repeat(64) }));
	const backend = {
		name: "work",
		cacheFile: path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(directory, "cache.json")),
		markerFile: ".work-project",
		evidenceKinds: ["verification", "closeout"],
		scopeFix: "",
		getExecution: async () => structuredClone(execution),
		findIssue: options.findIssue ?? (async () => {
			throw new Error("findIssue should not run for a key-shaped position-0 work id");
		}),
		sealExecutionCriteria,
		stampExecutionPlan,
		workClient: { healthReady },
	} as unknown as WorkflowBackend;
	let executeTool: ToolHandler | undefined;
	const pi = {
		zod: z,
		registerTool: (definition: { execute: ToolHandler }) => {
			executeTool = definition.execute;
		},
		registerCommand: () => {},
		registerFlag: () => {},
		registerMessageRenderer: () => {},
		on: () => {},
		appendEntry: () => {},
		sendMessage: () => {},
		getSessionId: () => "session-1",
		logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
	} as unknown as ExtensionAPI;
	createWorkflowHost({
		backend,
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: () => true,
	})(pi);
	const context = {
		cwd: directory,
		taskDepth: 0,
		ui: { notify: () => {}, setStatus: () => {}, theme: { fg: (_color: string, text: string) => text } },
	} as unknown as ExtensionContext;
	const call = async (action: "seal_execution_criteria" | "stamp_execution_plan") => {
		if (!executeTool) throw new Error("work tool missing");
		return executeTool(
			"planning-1",
			{ action, work: ANCHOR_KEY, criteria: ["AC-1"], plan_file: planPath, paths: ["src/feat.ts"] },
			new AbortController().signal,
			undefined,
			context,
		);
	};
	return { execution, item, planPath, sealExecutionCriteria, stampExecutionPlan, healthReady, call };
}

describe("execution remote ref before criteria sealing and plan stamping (OMP-233 AC6)", () => {
	test("canonical ref seals and stamps", async () => {
		const harness = await makeHarness({ remoteRef: CANONICAL_REF });
		const sealed = await harness.call("seal_execution_criteria");
		expect(sealed.details.success).toBe(true);
		expect(sealed.content[0].text).toContain("criteria sealed successfully");
		expect(harness.sealExecutionCriteria).toHaveBeenCalledTimes(1);

		harness.item.phase = "planning";
		const stamped = await harness.call("stamp_execution_plan");
		expect(stamped.details.success).toBe(true);
		expect(stamped.content[0].text).toContain("plan stamped successfully");
		expect(harness.stampExecutionPlan).toHaveBeenCalledTimes(1);
	});

	test("uuid position-0 work id resolves through findIssue before sealing", async () => {
		const findIssue = vi.fn(async () => ({ id: WORK_UUID, key: ANCHOR_KEY, title: "Anchor" }));
		const harness = await makeHarness({ remoteRef: CANONICAL_REF, workId: WORK_UUID, findIssue });
		const sealed = await harness.call("seal_execution_criteria");
		expect(sealed.details.success).toBe(true);
		expect(findIssue).toHaveBeenCalledWith(WORK_UUID);
		expect(harness.sealExecutionCriteria).toHaveBeenCalledTimes(1);
	});

	test("refs/heads/execution/omp-238 on an OMP-237 grant refuses seal and stamp", async () => {
		const harness = await makeHarness({ remoteRef: ALIAS_REF, phase: "criteria_pending" });
		const read = vi.spyOn(fs, "readFileSync");
		const sealed = await harness.call("seal_execution_criteria");
		expect(sealed.details.success).toBe(false);
		expect(sealed.content[0].text).toContain(ALIAS_REF);
		expect(sealed.content[0].text).toContain(CANONICAL_REF);
		expect(harness.sealExecutionCriteria).not.toHaveBeenCalled();
		expect(harness.healthReady).not.toHaveBeenCalled();

		harness.item.phase = "planning";
		const stamped = await harness.call("stamp_execution_plan");
		expect(stamped.details.success).toBe(false);
		expect(stamped.content[0].text).toContain(ALIAS_REF);
		expect(stamped.content[0].text).toContain(CANONICAL_REF);
		expect(harness.stampExecutionPlan).not.toHaveBeenCalled();
		expect(harness.healthReady).not.toHaveBeenCalled();
		expect(read.mock.calls.some(call => call[0] === harness.planPath)).toBe(false);
	});

	test("paused and terminal refusals keep their text ahead of a mismatched ref", async () => {
		const paused = await makeHarness({ remoteRef: ALIAS_REF, state: "paused", phase: "criteria_pending" });
		const pausedResult = await paused.call("seal_execution_criteria");
		expect(pausedResult.details.success).toBe(false);
		expect(pausedResult.content[0].text).toContain("execution grant is paused");
		expect(pausedResult.content[0].text).toContain("/execute resume OMP-237");
		expect(pausedResult.content[0].text).not.toContain("execution remote ref refusal");
		expect(paused.sealExecutionCriteria).not.toHaveBeenCalled();
		expect(paused.stampExecutionPlan).not.toHaveBeenCalled();

		const stopped = await makeHarness({ remoteRef: ALIAS_REF, state: "stopped", phase: "planning" });
		const stoppedResult = await stopped.call("stamp_execution_plan");
		expect(stoppedResult.details.success).toBe(false);
		expect(stoppedResult.content[0].text).toContain("execution grant is stopped");
		expect(stoppedResult.content[0].text).not.toContain("execution remote ref refusal");
		expect(stopped.sealExecutionCriteria).not.toHaveBeenCalled();
		expect(stopped.stampExecutionPlan).not.toHaveBeenCalled();
	});
});
