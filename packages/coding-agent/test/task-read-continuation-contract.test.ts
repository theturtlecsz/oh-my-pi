// Persisted classifier fixtures, not native-read producer or installed proof.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { ToolResultMessage } from "@oh-my-pi/pi-ai";
import { SessionManager } from "../src/session/session-manager";
import {
	buildTaskReadContinuationRecord,
	claimTaskReadContinuation,
	classifyTaskChildContinuation,
	type PersistedTaskBindingV1,
	TASK_READ_CONTINUATION_PROTOCOL,
	TASK_READ_CONTINUATION_READY,
	TASK_READ_CONTINUATION_STARTED,
	type TaskReadContinuationReadyV1,
	type TaskReadNativeProof,
	taskRecoveryHash,
} from "../src/task/recovery";
import { createAssistantMessage } from "./helpers/agent-session-setup";

let root: string;
let manager: SessionManager;
let binding: PersistedTaskBindingV1;
let anchor: string;
const managers: SessionManager[] = [];

beforeEach(async () => {
	root = await fs.mkdtemp(path.join(os.tmpdir(), "read-continuation-contract-"));
	manager = SessionManager.create(root, path.join(root, "sessions"));
	managers.push(manager);
	const call: PersistedTaskBindingV1["call"] = {
		bindingId: "binding",
		sessionId: "parent",
		promptEntryId: "parent-prompt",
		assistantEntryId: "parent-assistant",
		toolCallId: "parent-call",
		argumentsSha256: taskRecoveryHash({ task: "inspect" }),
	};
	const initialization = { systemPrompt: "", task: "inspect", tools: ["read", "yield"] };
	const init = manager.appendSessionInit({ ...initialization, taskCall: call });
	const contract: PersistedTaskBindingV1["contract"] = {
		agent: { name: "task", description: "fixture", systemPrompt: "", source: "bundled" },
		args: { task: "inspect" },
		assignment: "inspect",
		rawAssignment: "inspect",
		initialization,
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
		call,
		child: {
			registryId: "Child",
			sessionId: manager.getSessionId(),
			sessionFile: manager.getSessionFile()!,
			initEntryId: init,
			cwd: root,
		},
		contract,
		contractSha256: taskRecoveryHash(contract),
	};
	anchor = manager.appendMessage({ role: "user", attribution: "agent", content: "inspect", timestamp: 1 });
	manager.appendCustomEntry("prompt-preparation", {
		version: 1,
		sessionId: manager.getSessionId(),
		anchorEntryId: anchor,
		batchId: "initial",
		preparationEntryIds: [],
		taskBindingId: call.bindingId,
	});
});

afterEach(async () => {
	for (const session of managers.splice(0)) await session.close();
	await fs.rm(root, { recursive: true, force: true });
});

function readStep(
	options: { failed?: boolean; image?: boolean; secondCall?: boolean; wrongResult?: boolean } = {},
): TaskReadNativeProof {
	const args = { path: "plain.txt" };
	const assistant = manager.appendMessage({
		...createAssistantMessage(""),
		stopReason: "toolUse",
		content: [
			{ type: "toolCall", id: "read-call", name: "read", arguments: args },
			...(options.secondCall
				? [{ type: "toolCall" as const, id: "other-call", name: "read", arguments: args }]
				: []),
		],
	});
	manager.appendCustomEntry("tool_execution_start", { toolName: "read", toolCallId: "read-call" });
	const result: ToolResultMessage = {
		role: "toolResult",
		toolName: "read",
		toolCallId: options.wrongResult ? "foreign-call" : "read-call",
		content: options.image
			? [{ type: "image", data: "AA==", mimeType: "image/png" }]
			: [{ type: "text", text: "value" }],
		isError: options.failed ?? false,
		timestamp: 2,
	};
	manager.appendMessage(result);
	return {
		assistantEntryId: assistant,
		toolCallId: "read-call",
		arguments: args,
		native: { kind: "local-file-text", resolvedPath: path.join(root, "plain.txt"), fileSize: 5 },
		nativeResultSha256: taskRecoveryHash({ content: result.content, details: {} }),
	};
}

function certificate(proof = readStep()): TaskReadContinuationReadyV1 {
	return buildTaskReadContinuationRecord(manager.getEntries(), manager.getBranch(), binding, proof);
}

async function reopen(): Promise<SessionManager> {
	await manager.ensureOnDisk();
	const file = manager.getSessionFile()!;
	await manager.close();
	const restored = await SessionManager.open(file);
	managers.push(restored);
	return restored;
}

function classify(session = manager) {
	return classifyTaskChildContinuation(session.getEntries(), session.getBranch(), binding);
}

describe("certified read prefix consumer", () => {
	test("only certification changes completed read from unsupported history to resumable read", async () => {
		expect(classify().kind).toBe("unanswered");
		const ready = certificate();
		expect(() => classify()).toThrow("assistant/tool/effect history");
		manager.appendCustomEntry(TASK_READ_CONTINUATION_READY, ready);
		const restored = await reopen();
		const selected = classify(restored);
		expect(selected.kind).toBe("read");
		if (selected.kind !== "read") throw new Error("Expected certified read");
		expect(selected.anchorEntryId).toBe(anchor);
		const result = restored.getEntry(selected.ready.record.read.resultEntryId);
		if (result?.type !== "message") throw new Error("Selected read result disappeared");
		expect(selected.ready.record.read.finalResultSha256).toBe(taskRecoveryHash(result.message));
	});

	test.each([
		{ label: "failed", options: { failed: true }, reason: "failed tool history" },
		{ label: "nontext", options: { image: true }, reason: "non-text" },
		{ label: "multiple calls", options: { secondCall: true }, reason: "assistant call differs" },
		{ label: "mismatched result", options: { wrongResult: true }, reason: "foreign" },
	])("does not certify $label read history", ({ options, reason }) => {
		const proof = readStep(options);
		expect(() => certificate(proof)).toThrow(reason);
	});

	test("orphan preparation metadata cannot hide behind the exempt custom type", () => {
		manager.appendCustomEntry("prompt-preparation", {
			version: 1,
			sessionId: "foreign",
			anchorEntryId: "missing-foreign-anchor",
			batchId: "foreign",
			preparationEntryIds: [],
			taskBindingId: "another-binding",
		});
		const proof = readStep();
		expect(() => certificate(proof)).toThrow("foreign or late preparation metadata");
	});

	test("late preparation cannot retrospectively certify effects after read", () => {
		const proof = readStep();
		manager.appendCustomEntry("prompt-preparation", {
			version: 1,
			sessionId: manager.getSessionId(),
			anchorEntryId: anchor,
			batchId: "late",
			preparationEntryIds: [],
			taskBindingId: binding.call.bindingId,
		});
		expect(() => certificate(proof)).toThrow("late preparation metadata");
	});

	test("second initialization cannot substitute another child contract", () => {
		manager.appendSessionInit({ ...binding.contract.initialization, taskCall: binding.call });
		const proof = readStep();
		expect(() => certificate(proof)).toThrow("duplicate or foreign child initialization");
	});

	test.each(["prefix", "arguments", "final result", "call binding"] as const)(
		"rejects tampered %s certificate after reload",
		async field => {
			const ready = certificate();
			if (field === "prefix") ready.prefix.entriesSha256 = "0".repeat(64);
			if (field === "arguments") {
				ready.read.arguments.path = "different.txt";
				ready.read.native = { ...ready.read.native, resolvedPath: path.join(root, "different.txt") };
			}
			if (field === "final result") ready.read.finalResultSha256 = "0".repeat(64);
			if (field === "call binding") ready.call = { ...ready.call, toolCallId: "different-parent-call" };
			manager.appendCustomEntry(TASK_READ_CONTINUATION_READY, ready);
			const restored = await reopen();
			expect(() => classify(restored)).toThrow(
				field === "arguments" ? "assistant call differs" : "integrity changed",
			);
		},
	);

	test("off-branch ready cannot fall back to unanswered input", async () => {
		manager.appendCustomEntry(TASK_READ_CONTINUATION_READY, certificate());
		manager.branch(anchor);
		manager.appendCustomEntry("prompt-preparation", {
			version: 1,
			sessionId: manager.getSessionId(),
			anchorEntryId: anchor,
			batchId: "rewind",
			preparationEntryIds: [],
			taskBindingId: binding.call.bindingId,
		});
		const restored = await reopen();
		expect(() => classify(restored)).toThrow("outside active child branch");
	});

	test("orphan claim cannot authorize an unmarked child", async () => {
		manager.appendCustomEntry(TASK_READ_CONTINUATION_STARTED, {
			version: 1,
			protocol: TASK_READ_CONTINUATION_PROTOCOL,
			call: binding.call,
			contractSha256: binding.contractSha256,
			readyEntryId: "missing",
			readySha256: "0".repeat(64),
			reason: "cold-resume",
		});
		const restored = await reopen();
		expect(() => classify(restored)).toThrow("orphaned or conflicting retained markers");
	});

	test("competing retained certificates cannot select a preferred copy", async () => {
		const ready = certificate();
		manager.appendCustomEntry(TASK_READ_CONTINUATION_READY, ready);
		manager.appendCustomEntry(TASK_READ_CONTINUATION_READY, ready);
		const restored = await reopen();
		expect(() => classify(restored)).toThrow("orphaned or conflicting retained markers");
	});

	test("claim for another ready hash cannot borrow the current read certificate", async () => {
		const ready = certificate();
		const readyId = manager.appendCustomEntry(TASK_READ_CONTINUATION_READY, ready);
		manager.appendCustomEntry(TASK_READ_CONTINUATION_STARTED, {
			version: 1,
			protocol: TASK_READ_CONTINUATION_PROTOCOL,
			call: binding.call,
			contractSha256: binding.contractSha256,
			readyEntryId: readyId,
			readySha256: "0".repeat(64),
			reason: "cold-resume",
		});
		const restored = await reopen();
		expect(() => classify(restored)).toThrow("Malformed or detached read continuation processing claim");
	});

	test("live claim admits its owner while fresh process cannot replay started work", async () => {
		manager.appendCustomEntry(TASK_READ_CONTINUATION_READY, certificate());
		await manager.ensureOnDisk();
		const selected = classify();
		if (selected.kind !== "read") throw new Error("Expected read");
		let expectedLeaf = manager.getLeafId();
		const claim = await claimTaskReadContinuation(
			manager,
			binding,
			selected.ready,
			"cold-resume",
			async () => {},
			() => {
				expect(manager.getLeafId()).toBe(expectedLeaf);
			},
			id => {
				expectedLeaf = id;
			},
		);
		expect(classifyTaskChildContinuation(manager.getEntries(), manager.getBranch(), binding, claim).kind).toBe(
			"read",
		);
		const restored = await reopen();
		expect(() => classify(restored)).toThrow("prior response/startup processing is not replayable");
	});

	test("discarding claim and rewinding cannot erase ambiguous processing", async () => {
		const ready = certificate();
		const readyId = manager.appendCustomEntry(TASK_READ_CONTINUATION_READY, ready);
		const claimId = manager.appendCustomEntry(TASK_READ_CONTINUATION_STARTED, {
			version: 1,
			protocol: TASK_READ_CONTINUATION_PROTOCOL,
			call: binding.call,
			contractSha256: binding.contractSha256,
			readyEntryId: readyId,
			readySha256: taskRecoveryHash(ready),
			reason: "cold-resume",
		});
		await manager.ensureOnDisk();
		await manager.discardEntryDurably(claimId);
		manager.branch(readyId);
		manager.appendCustomEntry("rewind", {});
		const restored = await reopen();
		expect(() => classify(restored)).toThrow("later or discarded history");
	});
});
