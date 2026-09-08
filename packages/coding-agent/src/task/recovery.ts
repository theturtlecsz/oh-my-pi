import * as fs from "node:fs";
import * as path from "node:path";
import type { AgentToolResult } from "@oh-my-pi/pi-agent-core";
import { isEnoent, isRecord, pathIsWithin, stableStringifyJson } from "@oh-my-pi/pi-utils";
import { resolveAgentAdvisorSelection, resolveAgentPrewalkPattern } from "../config/model-resolver";
import { type AgentRef, getAgentTombstonePath } from "../registry/agent-registry";
import type { AgentSession } from "../session/agent-session";
import type { PromptOptions } from "../session/agent-session-types";
import type { SessionEntry, SessionInitEntry } from "../session/session-entries";
import type { ToolSession } from "../tools";
import { resolveAgentPrewalkDefault } from "./prewalk";
import { repairTaskParams } from "./repair-args";
import type { AgentDefinition, SingleResult, TaskParams, TaskToolDetails } from "./types";

export const TASK_RUN_BINDING = "task-run-binding";
export const PROMPT_PREPARATION = "prompt-preparation";
export const TASK_NATIVE_RESULT_READY = "task-native-result-ready";
export const TASK_RESULT_PROCESSING_STARTED = "task-result-processing-started";
export const TASK_RESULT_PROCESSING_PROTOCOL = "claim-before-parent-processing-v1";

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
	completion?: TaskResultProcessingRef;
}

export interface NativeTaskOutputIdentity {
	path: string;
	bytes: number;
	sha256: string;
}
export type NativeTaskResultObserver = (
	result: SingleResult,
	output: NativeTaskOutputIdentity | undefined,
) => Promise<void>;

export interface NativeTaskResultReadyV1 {
	version: 1;
	producer: "recovered-sync-task-v1";
	processingProtocol: typeof TASK_RESULT_PROCESSING_PROTOCOL;
	call: PersistedTaskCallRef;
	contractSha256: string;
	child: {
		sessionId: string;
		initEntryId: string;
		promptEntryId: string;
		leafId: string;
		branchSha256: string;
		entriesSha256: string;
		fileSha256: string;
		yieldResultEntryId: string;
	};
	output: NativeTaskOutputIdentity;
	/** Immutable bytes; processors receive a freshly parsed value, never this journal's nested objects. */
	payloadJson: string;
	payloadSha256: string;
}

export interface NativeTaskResultReadyCheckpoint {
	entryId: string;
	sha256: string;
	record: NativeTaskResultReadyV1;
}

export interface TaskResultProcessingStartedV1 {
	version: 1;
	protocol: typeof TASK_RESULT_PROCESSING_PROTOCOL;
	call: PersistedTaskCallRef;
	contractSha256: string;
	readyEntryId: string;
	readySha256: string;
}

export interface TaskResultProcessingRef {
	readyEntryId: string;
	readySha256: string;
	processingEntryId: string;
	processingSha256: string;
}

export interface TaskResultRecoveryState {
	ready?: NativeTaskResultReadyCheckpoint;
	processing?: { entryId: string; sha256: string; record: TaskResultProcessingStartedV1 };
}

export interface NativeRecoveredTaskResult {
	result: AgentToolResult<TaskToolDetails>;
	completion: NativeTaskResultReadyCheckpoint;
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
	findNativeResultReady(): NativeTaskResultReadyCheckpoint | undefined;
	recordNativeResultReady(record: NativeTaskResultReadyV1): Promise<NativeTaskResultReadyCheckpoint>;
	claimResultProcessing(ready: NativeTaskResultReadyCheckpoint): Promise<TaskResultProcessingRef>;
	assertProcessingOwnership(completion: TaskResultProcessingRef): void;
	pinCompletedChild(ref: AgentRef): void;
	setCompletionGuard(guard: () => Promise<() => void>): void;
}

export interface RecoveredTaskResult {
	completion?: TaskResultProcessingRef;
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
		fs.promises.realpath(artifactsDir),
		fs.promises.realpath(binding.child.sessionFile),
		fs.promises.realpath(cwd),
	]);
	if (
		!pathIsWithin(root, child) ||
		child !== path.join(root, `${binding.child.registryId}.jsonl`) ||
		(await fs.promises.realpath(binding.child.cwd)) !== workspace
	)
		throw new Error("Task child path or workspace differs from original artifact owner");
}

/** Capabilities held only by the native monitored driver, not extension callbacks. */
export interface BoundTaskDriverControls {
	prompt(text: string, options?: PromptOptions): Promise<boolean>;
	abort(): Promise<void>;
}

/** Reject lossy/non-JSON values; native optional object fields may be omitted normally. */
export function serializeNativeTaskResult(result: AgentToolResult<TaskToolDetails>): string {
	const ancestors = new Set<object>();
	const check = (value: unknown, arrayMember = false): void => {
		if (value === null || typeof value === "string" || typeof value === "boolean") return;
		if (value === undefined && !arrayMember) return;
		if (typeof value === "number" && Number.isFinite(value)) return;
		if (
			typeof value !== "object" ||
			value === null ||
			ancestors.has(value) ||
			(!Array.isArray(value) &&
				Object.getPrototypeOf(value) !== Object.prototype &&
				Object.getPrototypeOf(value) !== null) ||
			Object.getOwnPropertySymbols(value).length > 0
		)
			throw new Error("Native task result is not losslessly JSON serializable");
		ancestors.add(value);
		if (Array.isArray(value)) for (const item of value) check(item, true);
		else for (const item of Object.values(value)) check(item);
		ancestors.delete(value);
	};
	check(result);
	return stableStringifyJson(result);
}

export function nativeTaskResultPayload(ready: NativeTaskResultReadyCheckpoint): AgentToolResult<TaskToolDetails> {
	if (
		taskRecoveryHash(ready.record) !== ready.sha256 ||
		taskRecoveryHash(ready.record.payloadJson) !== ready.record.payloadSha256
	)
		throw new Error("Native task result checkpoint hash changed");
	const result: unknown = JSON.parse(ready.record.payloadJson);
	if (
		!isRecord(result) ||
		!Array.isArray(result.content) ||
		result.isError ||
		!isRecord(result.details) ||
		!Array.isArray(result.details.results) ||
		result.details.results.length !== 1
	)
		throw new Error("Native task result checkpoint has no successful original result");
	const child = result.details.results[0];
	if (
		!isRecord(child) ||
		child.exitCode !== 0 ||
		child.aborted ||
		child.error ||
		child.agent !== "task" ||
		child.agentSource !== "bundled" ||
		!isRecord(child.extractedToolData) ||
		!Array.isArray(child.extractedToolData.yield) ||
		!child.extractedToolData.yield.some(value => isRecord(value) && value.status === "success")
	)
		throw new Error("Native task result checkpoint is not successful finalized task output");
	return result as unknown as AgentToolResult<TaskToolDetails>;
}

export function taskResultRecoveryState(
	entries: readonly SessionEntry[],
	branch: readonly SessionEntry[],
	binding: PersistedTaskBindingV1,
): TaskResultRecoveryState {
	const byId = new Map(entries.map(entry => [entry.id, entry]));
	for (const entry of entries) {
		if (
			entry.type !== "branch_summary" ||
			!isRecord(entry.details) ||
			entry.details.kind !== "discarded-entry-branch"
		)
			continue;
		const visited = new Set<string>();
		let ancestor: SessionEntry | undefined = entry;
		while (ancestor && !visited.has(ancestor.id)) {
			if (ancestor.id === binding.call.promptEntryId || ancestor.id === binding.call.assistantEntryId)
				throw new Error("Task result processing history was discarded; unstarted processing cannot be proved");
			visited.add(ancestor.id);
			ancestor = ancestor.parentId ? byId.get(ancestor.parentId) : undefined;
		}
	}
	const ready: NativeTaskResultReadyCheckpoint[] = [];
	const processing: NonNullable<TaskResultRecoveryState["processing"]>[] = [];
	for (const entry of entries) {
		if (
			entry.type !== "custom" ||
			(entry.customType !== TASK_NATIVE_RESULT_READY && entry.customType !== TASK_RESULT_PROCESSING_STARTED)
		)
			continue;
		const data = entry.data;
		if (!isRecord(data) || data.version !== 1 || !isRecord(data.call))
			throw new Error("Malformed task result recovery record");
		if (
			data.call.bindingId !== binding.call.bindingId &&
			!(data.call.sessionId === binding.call.sessionId && data.call.toolCallId === binding.call.toolCallId)
		)
			continue;
		if (
			taskRecoveryHash(data.call) !== taskRecoveryHash(binding.call) ||
			data.contractSha256 !== binding.contractSha256
		)
			throw new Error("Task completion belongs to a different original call or contract");
		if (entry.customType === TASK_NATIVE_RESULT_READY) {
			if (
				data.producer !== "recovered-sync-task-v1" ||
				data.processingProtocol !== TASK_RESULT_PROCESSING_PROTOCOL ||
				!isRecord(data.child) ||
				!isRecord(data.output) ||
				typeof data.payloadJson !== "string" ||
				typeof data.payloadSha256 !== "string"
			)
				throw new Error("Malformed native task completion protocol");
			for (const field of [
				"sessionId",
				"initEntryId",
				"promptEntryId",
				"leafId",
				"branchSha256",
				"entriesSha256",
				"fileSha256",
				"yieldResultEntryId",
			])
				if (typeof data.child[field] !== "string" || !data.child[field])
					throw new Error("Malformed completed child identity");
			if (
				data.child.sessionId !== binding.child.sessionId ||
				data.child.initEntryId !== binding.child.initEntryId ||
				typeof data.output.path !== "string" ||
				typeof data.output.sha256 !== "string" ||
				typeof data.output.bytes !== "number" ||
				!Number.isSafeInteger(data.output.bytes) ||
				data.output.bytes < 0
			)
				throw new Error("Completed task child/output identity differs");
			const checkpoint = {
				entryId: entry.id,
				sha256: taskRecoveryHash(data),
				record: data as unknown as NativeTaskResultReadyV1,
			};
			const payload = nativeTaskResultPayload(checkpoint);
			if (
				payload.details?.results[0].id !== binding.child.registryId ||
				payload.details?.results[0].outputPath !== data.output.path
			)
				throw new Error("Native task payload names a different child/output");
			ready.push(checkpoint);
		} else {
			if (
				data.protocol !== TASK_RESULT_PROCESSING_PROTOCOL ||
				typeof data.readyEntryId !== "string" ||
				typeof data.readySha256 !== "string"
			)
				throw new Error("Malformed task result processing claim");
			processing.push({
				entryId: entry.id,
				sha256: taskRecoveryHash(data),
				record: data as unknown as TaskResultProcessingStartedV1,
			});
		}
	}
	if (ready.length > 1 || processing.length > 1)
		throw new Error("Conflicting retained task completion/processing records");
	if (ready.length && !branch.some(entry => entry.id === ready[0].entryId))
		throw new Error("Native task completion is outside the authorized branch");
	if (
		processing.length &&
		(!ready.length ||
			processing[0].record.readyEntryId !== ready[0].entryId ||
			processing[0].record.readySha256 !== ready[0].sha256)
	)
		throw new Error("Task result processing claim has no exact retained completion");
	return { ready: ready[0], processing: processing[0] };
}

export async function assertNativeTaskOutput(
	binding: PersistedTaskBindingV1,
	artifactsDir: string,
	output: NativeTaskOutputIdentity,
): Promise<void> {
	const root = await fs.promises.realpath(artifactsDir);
	if (
		output.path !== path.join(artifactsDir, `${binding.child.registryId}.md`) ||
		(await fs.promises.realpath(output.path)) !== path.join(root, `${binding.child.registryId}.md`)
	)
		throw new Error("Native task output escaped its original artifact owner");
	const bytes = await Bun.file(output.path).bytes();
	if (bytes.length !== output.bytes || new Bun.CryptoHasher("sha256").update(bytes).digest("hex") !== output.sha256)
		throw new Error("Native task output artifact changed or is incomplete");
}

/** Optimistic, scoped file witness. Hashes prove bytes; pinned metadata catches changes across later awaits. */
export function nativeTaskCompletionGuard(
	binding: PersistedTaskBindingV1,
	artifactsDir: string,
	ready: NativeTaskResultReadyCheckpoint,
): () => Promise<() => void> {
	const files = [binding.child.sessionFile, ready.record.output.path];
	const paths = [...files, artifactsDir, binding.child.cwd];
	const fingerprint = (stat: fs.BigIntStats): string => {
		if (!stat.isFile()) throw new Error("Certified task artifact is no longer a file");
		return [stat.dev, stat.ino, stat.size, stat.mtimeNs, stat.ctimeNs].join(":");
	};
	let pinned: string[] | undefined;
	const noTombstone = () => {
		try {
			fs.lstatSync(getAgentTombstonePath(binding.child.sessionFile));
		} catch (error) {
			if (isEnoent(error)) return;
			throw error;
		}
		throw new Error("Certified completed task was tombstoned");
	};
	const assertStable = () => {
		noTombstone();
		const current = [
			...files.map(file => fingerprint(fs.statSync(file, { bigint: true }))),
			...paths.map(file => fs.realpathSync(file)),
		];
		if (!pinned || taskRecoveryHash(current) !== taskRecoveryHash(pinned))
			throw new Error("Certified task files changed during result processing");
	};
	const observe = async () => [
		...(await Promise.all(files.map(async file => fingerprint(await fs.promises.stat(file, { bigint: true }))))),
		...(await Promise.all(paths.map(file => fs.promises.realpath(file)))),
	];
	return async () => {
		if (pinned) assertStable();
		const before = await observe();
		await assertTaskChildPath(binding, artifactsDir, binding.child.cwd);
		await assertNativeTaskOutput(binding, artifactsDir, ready.record.output);
		const bytes = await Bun.file(binding.child.sessionFile).bytes();
		if (new Bun.CryptoHasher("sha256").update(bytes).digest("hex") !== ready.record.child.fileSha256)
			throw new Error("Certified completed child journal changed");
		const after = await observe();
		if (
			taskRecoveryHash(before) !== taskRecoveryHash(after) ||
			(pinned && taskRecoveryHash(pinned) !== taskRecoveryHash(after))
		)
			throw new Error("Certified task files changed while verifying completion");
		pinned ??= after;
		assertStable();
		return assertStable;
	};
}
