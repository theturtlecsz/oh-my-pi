// Parser/selector component fixtures: journals are written through SessionManager.
// Native completion and real result-hook execution are qualified in separate tests.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentToolResult } from "@oh-my-pi/pi-agent-core";
import { SessionManager } from "../src/session/session-manager";
import {
	assertNativeTaskOutput,
	type NativeTaskResultReadyV1,
	nativeTaskResultPayload,
	type PersistedTaskBindingV1,
	serializeNativeTaskResult,
	TASK_NATIVE_RESULT_READY,
	TASK_RESULT_PROCESSING_PROTOCOL,
	TASK_RESULT_PROCESSING_STARTED,
	type TaskResultProcessingStartedV1,
	taskRecoveryHash,
	taskResultRecoveryState,
} from "../src/task/recovery";
import type { TaskToolDetails } from "../src/task/types";
import { createAssistantMessage } from "./helpers/agent-session-setup";

let root: string;
let parent: SessionManager;
let binding: PersistedTaskBindingV1;
let ready: NativeTaskResultReadyV1;
let payload: AgentToolResult<TaskToolDetails>;
const managers: SessionManager[] = [];

beforeEach(async () => {
	root = await fs.mkdtemp(path.join(os.tmpdir(), "completed-task-contract-"));
	parent = SessionManager.create(root, path.join(root, "parent"));
	const child = SessionManager.create(root, path.join(root, "child"));
	managers.push(parent, child);
	const prompt = parent.appendCustomMessageEntry("execution", "inspect", false);
	const args = { name: "Child", agent: "task", task: "inspect" };
	const assistant = parent.appendMessage({
		...createAssistantMessage(""),
		stopReason: "toolUse",
		content: [{ type: "toolCall", id: "call", name: "task", arguments: args }],
	});
	const init = child.appendSessionInit({ systemPrompt: "", task: "inspect", tools: ["yield"] });
	const childPrompt = child.appendMessage({ role: "user", content: "inspect", attribution: "agent", timestamp: 1 });
	child.appendMessage({
		...createAssistantMessage(""),
		stopReason: "toolUse",
		content: [{ type: "toolCall", id: "yield-call", name: "yield", arguments: { result: { data: "observed" } } }],
	});
	const yieldResult = child.appendMessage({
		role: "toolResult",
		toolName: "yield",
		toolCallId: "yield-call",
		content: [{ type: "text", text: "Result submitted." }],
		isError: false,
		timestamp: 2,
	});
	await child.ensureOnDisk();
	await child.flush();
	const childFile = child.getSessionFile();
	if (!childFile) throw new Error("Child fixture journal missing");
	const contract: PersistedTaskBindingV1["contract"] = {
		agent: { name: "task", description: "fixture", systemPrompt: "", source: "bundled" },
		args,
		assignment: "inspect",
		initialization: { systemPrompt: "", task: "inspect", tools: ["yield"] },
		policy: {
			async: false,
			batch: false,
			isolation: "none",
			parentDepth: 0,
			parentSpawns: "*",
			maxDepth: 1,
			disabledAgents: [],
			approval: null,
			approvalMode: "default",
			modelOverride: null,
			executionPolicy: null,
		},
		runtime: {
			model: { provider: "fixture", api: "openai-completions", id: "child" },
			thinking: "off",
			tiers: {},
			tools: [],
		},
	};
	binding = {
		version: 1,
		mode: "sync-flat",
		call: {
			bindingId: "binding",
			sessionId: parent.getSessionId(),
			promptEntryId: prompt,
			assistantEntryId: assistant,
			toolCallId: "call",
			argumentsSha256: taskRecoveryHash(args),
		},
		child: {
			registryId: "Child",
			sessionId: child.getSessionId(),
			sessionFile: childFile,
			initEntryId: init,
			cwd: root,
		},
		contract,
		contractSha256: taskRecoveryHash(contract),
	};
	parent.appendCustomEntry("task-run-binding", binding);
	const outputPath = path.join(root, "Child.md");
	await Bun.write(outputPath, "observed\n");
	payload = {
		content: [{ type: "text", text: "observed" }],
		details: {
			projectAgentsDir: null,
			totalDurationMs: 2,
			results: [
				{
					index: 0,
					id: "Child",
					agent: "task",
					agentSource: "bundled",
					task: "inspect",
					exitCode: 0,
					output: "observed",
					outputPath,
					stderr: "",
					truncated: false,
					durationMs: 2,
					tokens: 1,
					requests: 1,
					extractedToolData: { yield: [{ status: "success", data: { observed: "value" } }] },
				},
			],
		},
	};
	const payloadJson = serializeNativeTaskResult(payload);
	ready = {
		version: 1,
		producer: "recovered-sync-task-v1",
		processingProtocol: TASK_RESULT_PROCESSING_PROTOCOL,
		call: binding.call,
		contractSha256: binding.contractSha256,
		child: {
			sessionId: child.getSessionId(),
			initEntryId: init,
			promptEntryId: childPrompt,
			leafId: yieldResult,
			branchSha256: taskRecoveryHash(child.getBranch()),
			entriesSha256: taskRecoveryHash(child.getEntries()),
			fileSha256: new Bun.CryptoHasher("sha256").update(await Bun.file(childFile).bytes()).digest("hex"),
			yieldResultEntryId: yieldResult,
		},
		output: { path: outputPath, bytes: 9, sha256: new Bun.CryptoHasher("sha256").update("observed\n").digest("hex") },
		payloadJson,
		payloadSha256: taskRecoveryHash(payloadJson),
	};
});

afterEach(async () => {
	for (const manager of managers.splice(0)) await manager.close();
	await fs.rm(root, { recursive: true, force: true });
});

async function reload(): Promise<SessionManager> {
	await parent.ensureOnDisk();
	const file = parent.getSessionFile();
	if (!file) throw new Error("Parent fixture journal missing");
	await parent.close();
	const reopened = await SessionManager.open(file);
	managers.push(reopened);
	return reopened;
}

function select(manager: SessionManager) {
	return taskResultRecoveryState(manager.getEntries(), manager.getBranch(), binding);
}

function claim(readyEntryId: string): string {
	return parent.appendCustomEntry(TASK_RESULT_PROCESSING_STARTED, {
		version: 1,
		protocol: TASK_RESULT_PROCESSING_PROTOCOL,
		call: binding.call,
		contractSha256: binding.contractSha256,
		readyEntryId,
		readySha256: taskRecoveryHash(ready),
	} satisfies TaskResultProcessingStartedV1);
}

describe("completed task journal consumer contracts", () => {
	test("ready snapshot survives mutation of native input and fresh processor copies", async () => {
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, ready);
		payload.details!.results[0].output = "native mutation after snapshot";
		const reopened = await reload();
		const checkpoint = select(reopened).ready;
		if (!checkpoint) throw new Error("Expected ready checkpoint");
		const processor = nativeTaskResultPayload(checkpoint);
		processor.details!.results[0].output = "processor mutation";
		processor.details!.results[0].extractedToolData!.yield.push({ status: "aborted" });
		const next = nativeTaskResultPayload(checkpoint);
		expect(next.details!.results[0].output).toBe("observed");
		expect(next.details!.results[0].extractedToolData!.yield).toEqual([
			{ status: "success", data: { observed: "value" } },
		]);
		expect(select(reopened).ready?.record.payloadJson).toBe(ready.payloadJson);
	});

	test("malformed completion protocol never falls back to child resume", async () => {
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, { ...ready, processingProtocol: "unknown-protocol" });
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("Malformed native task completion protocol");
	});

	test("same binding with a changed original call is refused", async () => {
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, {
			...ready,
			call: { ...ready.call, assistantEntryId: "other-assistant" },
		});
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("different original call or contract");
	});

	test("wrong completed child session cannot certify this binding", async () => {
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, {
			...ready,
			child: { ...ready.child, sessionId: "another-child" },
		});
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("child/output identity differs");
	});

	test("changed serialized payload is refused with the original payload hash", async () => {
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, {
			...ready,
			payloadJson: ready.payloadJson.replace("observed", "changed"),
		});
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("checkpoint hash changed");
	});

	test("a hash-consistent failed final result cannot become cached success", async () => {
		payload.details!.results[0].exitCode = 1;
		const payloadJson = serializeNativeTaskResult(payload);
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, {
			...ready,
			payloadJson,
			payloadSha256: taskRecoveryHash(payloadJson),
		});
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("not successful finalized task output");
	});

	test("non-finite native metadata cannot be silently coerced into a ready snapshot", () => {
		payload.details!.results[0].durationMs = Number.POSITIVE_INFINITY;
		expect(() => serializeNativeTaskResult(payload)).toThrow("not losslessly JSON serializable");
	});

	test("rewinding before ready refuses retained off-branch completion", async () => {
		const prior = parent.getLeafId()!;
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, ready);
		parent.branch(prior);
		parent.appendCustomEntry("branch-owner", {});
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("outside the authorized branch");
	});

	test("competing ready records across sibling branches cannot select one silently", async () => {
		const prior = parent.getLeafId()!;
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, ready);
		parent.branch(prior);
		parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, ready);
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("Conflicting retained task completion/processing records");
	});

	test("rewinding before processing preserves the retained started claim", async () => {
		const readyId = parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, ready);
		const processingId = claim(readyId);
		parent.branch(readyId);
		parent.appendCustomEntry("branch-owner", {});
		const reopened = await reload();
		const state = select(reopened);
		expect(state.processing?.entryId).toBe(processingId);
		expect(state.processing?.record.readyEntryId).toBe(state.ready?.entryId);
		expect(reopened.getBranch().some(entry => entry.id === processingId)).toBe(false);
	});

	test("orphan processing claim cannot become an absent-ready fallback", async () => {
		claim("missing-ready");
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("no exact retained completion");
	});

	test("discarding a started claim and rewinding before discard still refuses ambiguity", async () => {
		const readyId = parent.appendCustomEntry(TASK_NATIVE_RESULT_READY, ready);
		const processingId = claim(readyId);
		await parent.ensureOnDisk();
		await parent.discardEntryDurably(processingId);
		parent.branch(readyId);
		parent.appendCustomEntry("branch-owner", {});
		const reopened = await reload();
		expect(() => select(reopened)).toThrow("processing history was discarded");
	});

	test("output bytes changed after certification refuse cached processing", async () => {
		await Bun.write(ready.output.path, "tampered\n");
		await expect(assertNativeTaskOutput(binding, root, ready.output)).rejects.toThrow("changed or is incomplete");
	});
});
