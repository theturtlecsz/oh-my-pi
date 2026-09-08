import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { SessionEntry } from "../src/session/session-entries";
import { SessionManager } from "../src/session/session-manager";
import {
	assertTaskChildPath,
	effectiveTaskArguments,
	type PersistedTaskBindingV1,
	type PromptPreparationRecordV1,
	preparedEntryIds,
	readPreparationRecord,
	readTaskBinding,
	taskRecoveryHash,
} from "../src/task/recovery";

let root: string;
let journal: SessionManager;
const managers: SessionManager[] = [];

beforeEach(async () => {
	root = await fs.mkdtemp(path.join(os.tmpdir(), "task-recovery-contract-"));
	journal = SessionManager.create(root, path.join(root, "sessions"));
	managers.push(journal);
});

afterEach(async () => {
	for (const manager of managers.splice(0)) await manager.close();
	await fs.rm(root, { recursive: true, force: true });
});

async function reload(): Promise<SessionManager> {
	const file = journal.getSessionFile();
	if (!file) throw new Error("Fixture has no session file");
	// These parser fixtures contain no assistant turn to materialize the draft.
	await journal.ensureOnDisk();
	await journal.close();
	const restored = await SessionManager.open(file);
	managers.push(restored);
	return restored;
}

async function persistedEntry(id: string): Promise<SessionEntry> {
	const entry = (await reload()).getBranch().find(candidate => candidate.id === id);
	if (!entry) throw new Error("Fixture entry is not on reloaded branch");
	return entry;
}

function preparation(anchor: string, members: string[], overrides: Partial<PromptPreparationRecordV1> = {}): string {
	return journal.appendCustomEntry("prompt-preparation", {
		version: 1,
		sessionId: journal.getSessionId(),
		anchorEntryId: anchor,
		batchId: "batch-one",
		preparationEntryIds: members,
		taskBindingId: "binding-one",
		...overrides,
	} satisfies PromptPreparationRecordV1);
}

function anchor(): string {
	return journal.appendMessage({ role: "user", content: "input", attribution: "agent", timestamp: 1 });
}

function binding(): PersistedTaskBindingV1 {
	const args = effectiveTaskArguments({ task: "inspect", agent: "task", name: "Child" });
	const contract: PersistedTaskBindingV1["contract"] = {
		agent: { name: "task", description: "fixture", systemPrompt: "", source: "bundled" },
		args,
		assignment: "inspect",
		initialization: { systemPrompt: "", task: "inspect", tools: ["read"] },
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
			executionPolicy: { prewalk: false, agentPrewalk: {}, agentAdvisor: {}, roleModels: {}, autoApprove: false },
		},
		runtime: {
			model: { provider: "fixture", api: "openai-completions", id: "child" },
			thinking: "off",
			tiers: {},
			tools: [],
		},
	};
	return {
		version: 1,
		mode: "sync-flat",
		call: {
			bindingId: "binding-one",
			sessionId: journal.getSessionId(),
			promptEntryId: "prompt",
			assistantEntryId: "assistant",
			toolCallId: "call",
			argumentsSha256: taskRecoveryHash(args),
		},
		child: {
			registryId: "Child",
			sessionId: "child-session",
			initEntryId: "init",
			sessionFile: path.join(root, "artifacts", "Child.jsonl"),
			cwd: root,
		},
		contract,
		contractSha256: taskRecoveryHash(contract),
	};
}

describe("task recovery persisted-consumer contracts", () => {
	test("repaired and trimmed flat arguments hash identically despite key order", () => {
		const repaired = effectiveTaskArguments({ name: " Child ", task: '  inspect\\n\\"file\\"  ', agent: " task " });
		expect(repaired).toEqual({ name: "Child", task: 'inspect\n"file"', agent: "task" });
		expect(taskRecoveryHash(repaired)).toBe(
			taskRecoveryHash({ task: 'inspect\n"file"', agent: "task", name: "Child" }),
		);
		expect(taskRecoveryHash(repaired)).not.toBe(taskRecoveryHash({ ...repaired, task: "different assignment" }));
	});

	test("batch and shared-context inputs cannot become a flat recovery call", () => {
		expect(() => effectiveTaskArguments({ tasks: [{ task: "inspect" }] })).toThrow("one flat assignment");
		expect(() => effectiveTaskArguments({ task: "inspect", context: "shared" })).toThrow("one flat assignment");
	});

	test("reloaded binding refuses an altered assignment with its old contract hash", async () => {
		const original = binding();
		const id = journal.appendCustomEntry("task-run-binding", {
			...original,
			contract: { ...original.contract, assignment: "different assignment" },
		});
		const awaitedEntry = await persistedEntry(id);
		expect(() => readTaskBinding(awaitedEntry)).toThrow("contract hash mismatch");
	});

	test("reloaded binding refuses a missing original assistant identity", async () => {
		const original = binding();
		const id = journal.appendCustomEntry("task-run-binding", {
			...original,
			call: { ...original.call, assistantEntryId: "" },
		});
		const entry = await persistedEntry(id);
		expect(() => readTaskBinding(entry)).toThrow("Malformed task call identity");
	});

	test("preparation parser refuses non-string member IDs loaded from disk", async () => {
		const id = journal.appendCustomEntry("prompt-preparation", {
			version: 1,
			sessionId: journal.getSessionId(),
			anchorEntryId: anchor(),
			batchId: "batch",
			preparationEntryIds: [42],
		});
		const entry = await persistedEntry(id);
		expect(() => readPreparationRecord(entry)).toThrow("Malformed core prompt preparation record");
	});

	test("membership unions distinct batches and excludes unregistered conversation", async () => {
		const input = anchor();
		const first = journal.appendCustomMessageEntry("context-a", "first", false);
		preparation(input, [first]);
		journal.appendCustomMessageEntry("unregistered", "unregistered", false);
		const second = journal.appendCustomMessageEntry("context-b", "second", false);
		preparation(input, [second], { batchId: "batch-two" });
		const restored = await reload();
		expect(preparedEntryIds(restored.getBranch(), restored.getSessionId(), input, "binding-one")).toEqual(
			new Set([first, second]),
		);
	});

	test("foreign-session preparation cannot authorize current-session context", async () => {
		const input = anchor();
		const member = journal.appendCustomMessageEntry("context", "context", false);
		preparation(input, [member], { sessionId: "another-session" });
		const restored = await reload();
		expect(() => preparedEntryIds(restored.getBranch(), restored.getSessionId(), input, "binding-one")).toThrow(
			"Conflicting prompt preparation identity",
		);
	});

	test("reused batch identity is refused even when its member list is empty", async () => {
		const input = anchor();
		preparation(input, []);
		preparation(input, []);
		const restored = await reload();
		expect(() => preparedEntryIds(restored.getBranch(), restored.getSessionId(), input, "binding-one")).toThrow(
			"Conflicting prompt preparation identity",
		);
	});

	test("one context entry cannot be claimed by two preparation batches", async () => {
		const input = anchor();
		const member = journal.appendCustomMessageEntry("context", "context", false);
		preparation(input, [member]);
		preparation(input, [member], { batchId: "batch-two" });
		const restored = await reload();
		expect(() => preparedEntryIds(restored.getBranch(), restored.getSessionId(), input, "binding-one")).toThrow(
			"Invalid core preparation membership",
		);
	});

	test("preparation cannot reference a context entry on a discarded sibling branch", async () => {
		const input = anchor();
		const orphan = journal.appendCustomMessageEntry("context", "orphan", false);
		journal.branch(input);
		preparation(input, [orphan]);
		const restored = await reload();
		expect(() => preparedEntryIds(restored.getBranch(), restored.getSessionId(), input, "binding-one")).toThrow(
			"Invalid core preparation membership",
		);
	});

	test("owner conversation cannot become preparation through a matching receipt", async () => {
		const input = anchor();
		const owner = journal.appendCustomMessageEntry("context", "owner input", false, undefined, "user");
		preparation(input, [owner]);
		const restored = await reload();
		expect(() => preparedEntryIds(restored.getBranch(), restored.getSessionId(), input, "binding-one")).toThrow(
			"Owner input cannot be task preparation",
		);
	});

	test("actual symlink cannot escape the original task artifact directory", async () => {
		const record = binding();
		const artifacts = path.join(root, "artifacts");
		await fs.mkdir(artifacts);
		const outside = path.join(root, "outside.jsonl");
		await Bun.write(outside, "{}\n");
		await fs.symlink(outside, record.child.sessionFile);
		await expect(assertTaskChildPath(record, artifacts, root)).rejects.toThrow("original artifact owner");
	});

	test("a different child basename inside the artifact root is refused", async () => {
		const record = binding();
		const artifacts = path.join(root, "artifacts");
		record.child.sessionFile = path.join(artifacts, "Other.jsonl");
		await Bun.write(record.child.sessionFile, "{}\n");
		await expect(assertTaskChildPath(record, artifacts, root)).rejects.toThrow("original artifact owner");
	});

	test("a confined child cannot resume in a different workspace", async () => {
		const record = binding();
		await Bun.write(record.child.sessionFile, "{}\n");
		const otherWorkspace = path.join(root, "other-workspace");
		await fs.mkdir(otherWorkspace);
		await expect(assertTaskChildPath(record, path.join(root, "artifacts"), otherWorkspace)).rejects.toThrow(
			"original artifact owner",
		);
	});
});
