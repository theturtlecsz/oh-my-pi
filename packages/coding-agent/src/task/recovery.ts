import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { AgentToolResult } from "@oh-my-pi/pi-agent-core";
import { isRecord, pathIsWithin, stableStringifyJson } from "@oh-my-pi/pi-utils";
import { resolveAgentAdvisorSelection, resolveAgentPrewalkPattern } from "../config/model-resolver";
import type { AgentRef } from "../registry/agent-registry";
import type { AgentSession } from "../session/agent-session";
import type { PromptOptions } from "../session/agent-session-types";
import type { SessionEntry, SessionInitEntry } from "../session/session-entries";
import type { ToolSession } from "../tools";
import { resolveAgentPrewalkDefault } from "./prewalk";
import { repairTaskParams } from "./repair-args";
import type { AgentDefinition, TaskParams } from "./types";

export const TASK_RUN_BINDING = "task-run-binding";
export const PROMPT_PREPARATION = "prompt-preparation";

export interface PersistedTaskCallRef {
	bindingId: string;
	sessionId: string;
	promptEntryId: string;
	assistantEntryId: string;
	toolCallId: string;
	argumentsSha256: string;
}

export interface PromptPreparationRecordV1 {
	version: 1;
	sessionId: string;
	anchorEntryId: string;
	batchId: string;
	preparationEntryIds: string[];
	taskBindingId?: string;
}

export type TaskInitializationContract = Pick<
	SessionInitEntry,
	| "systemPrompt"
	| "task"
	| "tools"
	| "agent"
	| "modelRole"
	| "resolvedModel"
	| "readOnly"
	| "outputSchema"
	| "outputSchemaMode"
	| "restrictToolNames"
	| "spawns"
	| "readSummarize"
	| "advisor"
>;

export interface TaskRecoveryPolicy {
	async: false;
	batch: false;
	isolation: "none";
	parentDepth: number;
	parentSpawns: string | null;
	maxDepth: number;
	disabledAgents: string[];
	approval: unknown;
	approvalMode: string;
	modelOverride: unknown;
	executionPolicy: unknown;
}

export interface TaskRecoveryContract {
	agent: AgentDefinition;
	args: TaskParams;
	assignment: string;
	rawAssignment?: string;
	initialization: TaskInitializationContract;
	policy: TaskRecoveryPolicy;
	runtime: {
		model: { provider: string; api: string; id: string };
		thinking: string | undefined;
		tiers: unknown;
		tools: Array<{ name: string; parameters: unknown; approval: unknown }>;
	};
}

export interface PersistedTaskBindingV1 {
	version: 1;
	mode: "sync-flat";
	call: PersistedTaskCallRef;
	child: { registryId: string; sessionId: string; sessionFile: string; initEntryId: string; cwd: string };
	contract: TaskRecoveryContract;
	contractSha256: string;
}

/** Core-owned result identity survives extension replacement of tool details. */
export interface PersistedTaskResultRef {
	bindingId: string;
	contractSha256: string;
	toolCallId: string;
	childSessionId: string;
}

export interface TaskCallCapture {
	call: PersistedTaskCallRef;
	bindChild(binding: PersistedTaskBindingV1): Promise<void>;
}

export interface PreparedTaskChild {
	session: AgentSession;
	init: SessionInitEntry;
	registryId: string;
}

export interface BoundTaskRecoveryRequest {
	binding: PersistedTaskBindingV1;
	signal: AbortSignal;
	validateAuthority(): Promise<void>;
	setPolicyGuard(guard: () => Promise<void>): void;
	validateChild(ref: AgentRef, session: AgentSession): Promise<void>;
}

export interface RecoveredTaskResult {
	result: AgentToolResult<unknown>;
	binding: PersistedTaskBindingV1;
}

export function taskRecoveryHash(value: unknown): string {
	return new Bun.CryptoHasher("sha256")
		.update("omp-task-recovery-v1\0")
		.update(stableStringifyJson(value))
		.digest("hex");
}

export function effectiveTaskArguments(params: unknown): TaskParams {
	if (!isRecord(params)) throw new Error("Task recovery arguments are not an object");
	const repaired = repairTaskParams(params as TaskParams);
	if (typeof repaired.task !== "string" || repaired.tasks !== undefined || repaired.context !== undefined)
		throw new Error("Task recovery requires one flat assignment");
	return {
		...repaired,
		task: repaired.task.trim(),
		agent: repaired.agent?.trim() || "task",
		name: repaired.name?.trim() || undefined,
	};
}

/** Unsupported execution policies keep their normal path without recovery metadata. */
export function supportsTaskRecoveryAgent(session: ToolSession, agent: AgentDefinition): boolean {
	return (
		agent.name === "task" &&
		agent.source === "bundled" &&
		!resolveAgentPrewalkPattern({
			settingsOverride: session.settings.get("task.agentPrewalk")[agent.name],
			agentPrewalk: resolveAgentPrewalkDefault(agent, session.settings.get("task.prewalk")),
		}) &&
		!resolveAgentAdvisorSelection({
			settingsOverride: session.settings.get("task.agentAdvisor")[agent.name],
			agentAdvisor: agent.advisor,
		})
	);
}

export function taskRecoveryPolicy(session: ToolSession): TaskRecoveryPolicy {
	const settings = session.settings;
	if (
		settings.get("async.enabled") !== false ||
		settings.get("task.batch") !== false ||
		settings.get("task.isolation.mode") !== "none"
	)
		throw new Error("Task recovery requires explicit synchronous, flat, non-isolated execution");
	return {
		async: false,
		batch: false,
		isolation: "none",
		parentDepth: session.taskDepth ?? 0,
		parentSpawns: session.getSessionSpawns(),
		maxDepth: settings.get("task.maxRecursionDepth"),
		disabledAgents: settings.get("task.disabledAgents"),
		approval: settings.get("tools.approval"),
		approvalMode: settings.get("tools.approvalMode"),
		modelOverride: settings.get("task.agentModelOverrides"),
		executionPolicy: {
			prewalk: settings.get("task.prewalk"),
			agentPrewalk: settings.get("task.agentPrewalk"),
			agentAdvisor: settings.get("task.agentAdvisor"),
			roleModels: settings.getModelRoles(),
			autoApprove: session.getToolContext?.()?.autoApprove === true,
		},
	};
}

export function taskRuntimeContract(session: AgentSession): TaskRecoveryContract["runtime"] {
	const model = session.model;
	if (!model) throw new Error("Task recovery has no resolved child model");
	return {
		model: { provider: model.provider, api: model.api, id: model.id },
		thinking: session.thinkingLevel,
		tiers: session.serviceTierByFamily,
		tools: session.agent.state.tools
			.map(tool => ({
				name: tool.name,
				parameters: tool.parameters,
				approval: typeof tool.approval === "function" ? "dynamic" : tool.approval,
			}))
			.sort((a, b) => a.name.localeCompare(b.name)),
	};
}

export function initializationContract(init: SessionInitEntry): TaskInitializationContract {
	return {
		systemPrompt: init.systemPrompt,
		task: init.task,
		tools: init.tools,
		agent: init.agent,
		modelRole: init.modelRole,
		resolvedModel: init.resolvedModel,
		readOnly: init.readOnly,
		outputSchema: init.outputSchema,
		outputSchemaMode: init.outputSchemaMode,
		restrictToolNames: init.restrictToolNames,
		spawns: init.spawns,
		readSummarize: init.readSummarize,
		advisor: init.advisor,
	};
}

export function readTaskBinding(entry: SessionEntry): PersistedTaskBindingV1 | undefined {
	if (entry.type !== "custom" || entry.customType !== TASK_RUN_BINDING) return undefined;
	const value = entry.data;
	if (
		!isRecord(value) ||
		value.version !== 1 ||
		value.mode !== "sync-flat" ||
		!isRecord(value.call) ||
		!isRecord(value.child) ||
		!isRecord(value.contract)
	)
		throw new Error("Malformed task recovery binding");
	for (const key of ["bindingId", "sessionId", "promptEntryId", "assistantEntryId", "toolCallId", "argumentsSha256"])
		if (typeof value.call[key] !== "string" || !value.call[key]) throw new Error("Malformed task call identity");
	for (const key of ["registryId", "sessionId", "sessionFile", "initEntryId", "cwd"])
		if (typeof value.child[key] !== "string" || !value.child[key]) throw new Error("Malformed child task identity");
	if (value.contractSha256 !== taskRecoveryHash(value.contract))
		throw new Error("Task recovery contract hash mismatch");
	return value as unknown as PersistedTaskBindingV1;
}

export function readPreparationRecord(entry: SessionEntry): PromptPreparationRecordV1 | undefined {
	if (entry.type !== "custom" || entry.customType !== PROMPT_PREPARATION) return undefined;
	const record = entry.data;
	if (
		!isRecord(record) ||
		record.version !== 1 ||
		typeof record.sessionId !== "string" ||
		typeof record.anchorEntryId !== "string" ||
		typeof record.batchId !== "string" ||
		!Array.isArray(record.preparationEntryIds) ||
		record.preparationEntryIds.some(id => typeof id !== "string") ||
		(record.taskBindingId !== undefined && typeof record.taskBindingId !== "string")
	)
		throw new Error("Malformed core prompt preparation record");
	return record as unknown as PromptPreparationRecordV1;
}

/** Exact journal membership; content/customType never confers preparation ownership. */
export function preparedEntryIds(
	branch: readonly SessionEntry[],
	sessionId: string,
	anchorId: string,
	taskBindingId?: string,
): Set<string> {
	const ids = new Set<string>();
	const anchorIndex = branch.findIndex(entry => entry.id === anchorId);
	if (anchorIndex < 0) throw new Error("Prompt preparation anchor is absent from active branch");
	const batches = new Set<string>();
	for (const [index, entry] of branch.entries()) {
		const record = readPreparationRecord(entry);
		if (!record || record.anchorEntryId !== anchorId) continue;
		if (
			record.sessionId !== sessionId ||
			record.taskBindingId !== taskBindingId ||
			index <= anchorIndex ||
			batches.has(record.batchId)
		)
			throw new Error("Conflicting prompt preparation identity");
		batches.add(record.batchId);
		for (const id of record.preparationEntryIds) {
			const memberIndex = branch.findIndex(candidate => candidate.id === id);
			const member = branch[memberIndex];
			if (
				id === anchorId ||
				ids.has(id) ||
				memberIndex < 0 ||
				memberIndex >= index ||
				(member.type !== "custom_message" && member.type !== "message")
			)
				throw new Error("Invalid core preparation membership");
			if (
				(member.type === "custom_message"
					? member.attribution
					: "attribution" in member.message
						? member.message.attribution
						: undefined) !== "agent"
			)
				throw new Error("Owner input cannot be task preparation");
			ids.add(id);
		}
	}
	if (batches.size === 0) throw new Error("Original prompt has no complete core preparation association");
	return ids;
}

export async function assertTaskChildPath(
	binding: PersistedTaskBindingV1,
	artifactsDir: string,
	cwd: string,
): Promise<void> {
	const [root, child, workspace] = await Promise.all([
		fs.realpath(artifactsDir),
		fs.realpath(binding.child.sessionFile),
		fs.realpath(cwd),
	]);
	if (
		!pathIsWithin(root, child) ||
		child !== path.join(root, `${binding.child.registryId}.jsonl`) ||
		(await fs.realpath(binding.child.cwd)) !== workspace
	)
		throw new Error("Task child path or workspace differs from original artifact owner");
}

/** Capabilities held only by the native monitored driver, not extension callbacks. */
export interface BoundTaskDriverControls {
	prompt(text: string, options?: PromptOptions): Promise<boolean>;
	abort(): Promise<void>;
}
