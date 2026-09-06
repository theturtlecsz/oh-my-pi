import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { type WorkItemView, type WorkflowView } from "@oh-my-pi/pi-work-client";
import { confirmWrite, resetConfirmations } from "../extensions/workflow/confirm";
import { buildPlanPacket, createWorkBackend } from "../extensions/workflow/work";
import { createWorkflowHost } from "../extensions/workflow/host";
import { WorkError } from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

describe("revise_work structured amendments and preview binding", () => {
	test("confirmWrite binds expectedRevisionId and approves when revision matches", () => {
		resetConfirmations({ resetShared: true });
		const action = "revise_work";
		const question = "Model wants to revise this work in place";
		const detail = "OMP-1 Title (revision 00000000-0000-7000-8000-000000000001)\n→ new scope:\npackages/core";
		const params = { action, work: "OMP-1", scope: "packages/core" };

		const preview = confirmWrite(action, question, detail, params, {
			expectedRevisionId: "00000000-0000-7000-8000-000000000001",
			currentRevisionId: "00000000-0000-7000-8000-000000000001",
		});
		expect(preview.approved).toBe(false);
		if (preview.approved) throw new Error("expected unapproved preview");
		expect(preview.preview).toContain("CONFIRM REQUIRED");
		expect(preview.preview).toContain("→ new scope:\npackages/core");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(preview.preview);
		expect(match).not.toBeNull();
		const confirmationId = match![1];

		// Second call with same revision approves
		const confirmed = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: confirmationId,
		}, {
			expectedRevisionId: "00000000-0000-7000-8000-000000000001",
			currentRevisionId: "00000000-0000-7000-8000-000000000001",
		});
		expect(confirmed.approved).toBe(true);
	});

	test("confirmWrite refuses stale preview with revision conflict and emits fresh preview", () => {
		resetConfirmations({ resetShared: true });
		const action = "revise_work";
		const question = "Model wants to revise this work in place";
		const detail = "OMP-1 Title (revision 00000000-0000-7000-8000-000000000001)\n→ new scope:\npackages/core";
		const params = { action, work: "OMP-1", scope: "packages/core" };

		const preview = confirmWrite(action, question, detail, params, {
			expectedRevisionId: "00000000-0000-7000-8000-000000000001",
			currentRevisionId: "00000000-0000-7000-8000-000000000001",
		});
		expect(preview.approved).toBe(false);
		if (preview.approved) throw new Error("expected unapproved preview");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(preview.preview);
		const oldConfirmationId = match![1];

		// Second call happens after the item was revised concurrently to revision 00000000-0000-7000-8000-000000000002
		const staleResult = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: oldConfirmationId,
		}, {
			currentRevisionId: "00000000-0000-7000-8000-000000000002",
		});
		expect(staleResult.approved).toBe(false);
		if (staleResult.approved) throw new Error("expected refusal on stale revision");
		expect(staleResult.preview).toContain("REFUSED — revision conflict");
		expect(staleResult.preview).toContain("00000000-0000-7000-8000-000000000001 to 00000000-0000-7000-8000-000000000002");
		expect(staleResult.preview).toContain("CONFIRM REQUIRED — nothing written.");

		const freshMatch = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(staleResult.preview);
		expect(freshMatch).not.toBeNull();
		const freshConfirmationId = freshMatch![1];
		expect(freshConfirmationId).not.toBe(oldConfirmationId);

		// Confirming with fresh confirmation id succeeds against the updated revision
		const freshConfirm = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: freshConfirmationId,
		}, {
			expectedRevisionId: "00000000-0000-7000-8000-000000000002",
			currentRevisionId: "00000000-0000-7000-8000-000000000002",
		});
		expect(freshConfirm.approved).toBe(true);
	});

	test("backend.reviseWork preserves unchanged fields and passes structured scope and criteria", async () => {
		let sentCommand: { type: string; payload: Record<string, unknown> } | undefined;
		const initialItem: WorkItemView = {
			work_id: "00000000-0000-7000-8000-000000000010",
			workspace_id: "00000000-0000-7000-8000-000000000001",
			alias: { work_id: "00000000-0000-7000-8000-000000000010", key: "OMP-1", primary: true, origin: "local" },
			state: "BACKLOG",
			revision: {
				revision_id: "00000000-0000-7000-8000-000000000011",
				work_id: "00000000-0000-7000-8000-000000000010",
				revision_number: 1,
				title: "Original Title",
				description: "Original description markdown",
				scope: "packages/original-scope",
				acceptance_criteria: ["AC-1 initial criterion", "AC-2 second criterion"],
				content_sha256: "0".repeat(64),
				created_by: "system",
				created_at: new Date().toISOString(),
			},
			candidate: null,
			project_id: null,
			archived: false,
		};

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.endsWith("/v1/work-items/OMP-1")) {
				return new Response(JSON.stringify(initialItem), { status: 200 });
			}
			if (url.endsWith("/v1/tree")) {
				return new Response(JSON.stringify({ workspace_id: initialItem.workspace_id, items: [initialItem], relations: [], projects: [] }), { status: 200 });
			}
			if (url.endsWith("/v1/commands")) {
				const body = JSON.parse(String(init?.body)) as { command: { type: string; payload: Record<string, unknown> } };
				sentCommand = body.command;
				return new Response(JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000099" },
					result: { type: "revise_work", revision_id: "00000000-0000-7000-8000-000000000012", changed: true },
				}), { status: 200 });
			}
			return new Response("not found", { status: 404 });
		};

		const pendingDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-pending-test-"));
		const backend = createWorkBackend({
			baseUrl: "http://127.0.0.1:54322",
			workspaceId: initialItem.workspace_id,
			ownerId: "00000000-0000-7000-8000-000000000002",
		}, () => "test-token", mockFetch, pendingDir);

		const nowRef = {
			id: initialItem.work_id,
			key: "OMP-1",
			title: initialItem.revision.title,
		};

		// 1. Amend only scope
		await backend.reviseWork(nowRef, {
			scope: "packages/new-scope",
			expected_revision_id: "00000000-0000-7000-8000-000000000011",
		});
		expect(sentCommand?.type).toBe("revise_work");
		const rev1 = sentCommand?.payload.revision as Record<string, unknown>;
		expect(rev1.scope).toBe("packages/new-scope");
		expect(rev1.title).toBe("Original Title");
		expect(rev1.description).toBe("Original description markdown");
		expect(rev1.acceptance_criteria).toEqual(["AC-1 initial criterion", "AC-2 second criterion"]);
		expect(rev1.revision_number).toBe(2);

		// 2. Amend only acceptance criteria
		await backend.reviseWork(nowRef, {
			criteria: ["AC-1 updated", "AC-3 added"],
			expected_revision_id: "00000000-0000-7000-8000-000000000011",
		});
		const rev2 = sentCommand?.payload.revision as Record<string, unknown>;
		expect(rev2.scope).toBe("packages/original-scope");
		expect(rev2.title).toBe("Original Title");
		expect(rev2.description).toBe("Original description markdown");
		expect(rev2.acceptance_criteria).toEqual(["AC-1 updated", "AC-3 added"]);

		// 3. Stale expected_revision_id throws WorkError revision_conflict
		await expect(
			backend.reviseWork(nowRef, {
				expected_revision_id: "00000000-0000-7000-8000-000000000000",
				scope: "packages/failed-scope",
			}),
		).rejects.toThrow(WorkError);
		try {
			await backend.reviseWork(nowRef, {
				expected_revision_id: "00000000-0000-7000-8000-000000000000",
				scope: "packages/failed-scope",
			});
			expect.unreachable("expected revision_conflict WorkError");
		} catch (err) {
			expect(err).toBeInstanceOf(WorkError);
			expect((err as WorkError).code).toBe("revision_conflict");
		}

		// 4. Missing expected_revision_id throws WorkError invalid_request
		await expect(
			backend.reviseWork(nowRef, {
				scope: "packages/failed-scope",
			}),
		).rejects.toThrow(WorkError);
		try {
			await backend.reviseWork(nowRef, {
				scope: "packages/failed-scope",
			});
			expect.unreachable("expected invalid_request WorkError");
		} catch (err) {
			expect(err).toBeInstanceOf(WorkError);
			expect((err as WorkError).code).toBe("invalid_request");
		}
		fs.rmSync(pendingDir, { recursive: true, force: true });
	});

	test("host revise_work fails closed on missing workClient or failed revision fetch", async () => {
		resetConfirmations({ resetShared: true });
		let lookupShouldFail = false;
		let currentRev = "00000000-0000-7000-8000-000000000011";

		const initialItem = (): WorkItemView => ({
			work_id: "00000000-0000-7000-8000-000000000010",
			workspace_id: "00000000-0000-7000-8000-000000000001",
			alias: { work_id: "00000000-0000-7000-8000-000000000010", key: "OMP-1", primary: true, origin: "local" },
			state: "BACKLOG",
			revision: {
				revision_id: currentRev,
				work_id: "00000000-0000-7000-8000-000000000010",
				revision_number: 1,
				title: "Original Title",
				description: "Original description",
				scope: "packages/scope",
				acceptance_criteria: ["AC-1"],
				content_sha256: "0".repeat(64),
				created_by: "system",
				created_at: new Date().toISOString(),
			},
			candidate: null,
			project_id: null,
			archived: false,
		});

		const mockFetch = async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url.includes("/v1/work-items/OMP-1")) {
				if (lookupShouldFail) {
					const itemWithoutRev = { ...initialItem(), revision: { ...initialItem().revision, revision_id: "" } };
					return new Response(JSON.stringify(itemWithoutRev), { status: 200 });
				}
				return new Response(JSON.stringify(initialItem()), { status: 200 });
			}
			if (url.includes("/tree")) {
				return new Response(JSON.stringify({ workspace_id: "00000000-0000-7000-8000-000000000001", items: [initialItem()], relations: [], projects: [] }), { status: 200 });
			}
			if (url.endsWith("/v1/commands")) {
				return new Response(JSON.stringify({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000099" },
					result: { type: "revise_work", revision_id: "00000000-0000-7000-8000-000000000012", changed: true },
				}), { status: 200 });
			}
			return new Response("not found", { status: 404 });
		};

		const pendingDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-pending-host-test-"));
		const backend = createWorkBackend({
			baseUrl: "http://127.0.0.1:54322",
			workspaceId: "00000000-0000-7000-8000-000000000001",
			ownerId: "00000000-0000-7000-8000-000000000002",
		}, () => "test-token", mockFetch, pendingDir);
		let registeredTool: { execute: (id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: () => void, ctx: Record<string, unknown>) => Promise<{ content: { type: string; text: string }[] }> } | null = null;
		const fakeExtensionApi = {
			zod: z,
			registerTool(tool: typeof registeredTool) {
				registeredTool = tool;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
		};

		const initHost = createWorkflowHost({
			backend,
			teamNoun: "the ledger",
			entryType: "work-now",
		});
		initHost(fakeExtensionApi as unknown as ExtensionAPI);

		expect(registeredTool).not.toBeNull();

		// 1. Failed revision lookup fails closed (no preview minted)
		lookupShouldFail = true;
		const failResult = await registeredTool!.execute("call-1", {
			action: "revise_work",
			work: "OMP-1",
			scope: "new-scope",
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		expect(failResult.content[0].text).toContain("revision lookup failed");

		// 2. Successful preview binds revision
		lookupShouldFail = false;
		const previewResult = await registeredTool!.execute("call-2", {
			action: "revise_work",
			work: "OMP-1",
			scope: "new-scope",
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		expect(previewResult.content[0].text).toContain("CONFIRM REQUIRED");
		expect(previewResult.content[0].text).toContain('→ new scope: "new-scope"');
		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(previewResult.content[0].text);
		expect(match).not.toBeNull();
		const confId = match![1];

		// 3. Concurrent revision drift triggers conflict and emits fresh preview
		currentRev = "00000000-0000-7000-8000-000000000022";
		const driftResult = await registeredTool!.execute("call-3", {
			action: "revise_work",
			work: "OMP-1",
			scope: "new-scope",
			confirm: true,
			confirmation_id: confId,
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		expect(driftResult.content[0].text).toContain("REFUSED — revision conflict");
		expect(driftResult.content[0].text).toContain("00000000-0000-7000-8000-000000000011 to 00000000-0000-7000-8000-000000000022");
		expect(driftResult.content[0].text).toContain("CONFIRM REQUIRED — nothing written.");

		const freshMatch = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(driftResult.content[0].text);
		expect(freshMatch).not.toBeNull();
		const freshConfId = freshMatch![1];
		expect(freshConfId).not.toBe(confId);

		// 4. Confirming fresh preview succeeds
		const successResult = await registeredTool!.execute("call-4", {
			action: "revise_work",
			work: "OMP-1",
			scope: "new-scope",
			confirm: true,
			confirmation_id: freshConfId,
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		expect(successResult.content[0].text).toBe("OMP-1 revised");

		fs.rmSync(pendingDir, { recursive: true, force: true });
	});

	test("buildPlanPacket uses structured acceptance_criteria from revised item", () => {
		const workflowView: WorkflowView = {
			item: {
				work_id: "00000000-0000-7000-8000-000000000010",
				workspace_id: "00000000-0000-7000-8000-000000000001",
				alias: { work_id: "00000000-0000-7000-8000-000000000010", key: "OMP-1", primary: true, origin: "local" },
				state: "IN_PROGRESS",
				revision: {
					revision_id: "00000000-0000-7000-8000-000000000012",
					work_id: "00000000-0000-7000-8000-000000000010",
					revision_number: 2,
					title: "Revised Work",
					description: "Description without criteria markdown headers",
					scope: "session-system/extensions/workflow",
					acceptance_criteria: ["AC-1 structured criterion 1", "AC-2 structured criterion 2"],
					content_sha256: "0".repeat(64),
					created_by: "system",
					created_at: new Date().toISOString(),
				},
				candidate: {
					candidate_id: "00000000-0000-7000-8000-000000000020",
					candidate_sha256: "1".repeat(64),
					commit_sha: "2".repeat(40),
				},
				project_id: null,
				archived: false,
			},
			relations: [],
			receipts: [
				{
					receipt_id: "00000000-0000-7000-8000-000000000030",
					kind: "plan",
					candidate_id: "00000000-0000-7000-8000-000000000020",
					payload_sha256: "3".repeat(64),
					payload: { body: "## Approach\n- step 1\n## Verification\n- test 1" },
					issued_at: new Date().toISOString(),
					issued_by: "agent",
				},
			],
			close_attempts: [],
			audit_manifest: null,
			auditor_launches: [],
			close_attempt_events: [],
			checkpoint_deliveries: [],
			project: null,
		};

		const packet = buildPlanPacket(workflowView);
		expect(packet).toBeDefined();
		expect(packet?.acceptanceCriteria).toEqual([
			"AC-1 structured criterion 1",
			"AC-2 structured criterion 2",
		]);
	});
	test("get_work renders SCOPE and ACCEPTANCE CRITERIA when present on item", async () => {
		const itemWithScopeAndCriteria: WorkItemView = {
			work_id: "00000000-0000-7000-8000-000000000010",
			workspace_id: "00000000-0000-7000-8000-000000000001",
			alias: { work_id: "00000000-0000-7000-8000-000000000010", key: "OMP-10", primary: true, origin: "local" },
			state: "BACKLOG",
			revision: {
				revision_id: "00000000-0000-7000-8000-000000000012",
				work_id: "00000000-0000-7000-8000-000000000010",
				revision_number: 2,
				title: "Readback Target",
				description: "Readback description text",
				scope: "packages/scoped-area",
				acceptance_criteria: ["AC-1 first criteria item", "AC-2 second criteria item"],
				content_sha256: "0".repeat(64),
				created_by: "system",
				created_at: new Date().toISOString(),
			},
			candidate: null,
			project_id: null,
			archived: false,
		};

		const mockWorkflowView: WorkflowView = {
			item: itemWithScopeAndCriteria,
			relations: [],
			receipts: [],
			close_attempts: [],
			audit_manifest: null,
			auditor_launches: [],
			close_attempt_events: [],
			checkpoint_deliveries: [],
			project: null,
		};

		const mockFetch = async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes("/v1/work-items/OMP-10/workflow")) {
				return new Response(JSON.stringify(mockWorkflowView), { status: 200 });
			}
			if (url.includes("/v1/work-items/OMP-10")) {
				return new Response(JSON.stringify(itemWithScopeAndCriteria), { status: 200 });
			}
			if (url.includes("/tree")) {
				return new Response(JSON.stringify({ workspace_id: "00000000-0000-7000-8000-000000000001", items: [itemWithScopeAndCriteria], relations: [], projects: [] }), { status: 200 });
			}
			return new Response("not found", { status: 404 });
		};

		const pendingDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-pending-getwork-test-"));
		const backend = createWorkBackend({
			baseUrl: "http://127.0.0.1:54322",
			workspaceId: "00000000-0000-7000-8000-000000000001",
			ownerId: "00000000-0000-7000-8000-000000000002",
		}, () => "test-token", mockFetch, pendingDir);

		let registeredTool: { execute: (id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: () => void, ctx: Record<string, unknown>) => Promise<{ content: { type: string; text: string }[] }> } | null = null;
		const fakeExtensionApi = {
			zod: z,
			registerTool(tool: typeof registeredTool) {
				registeredTool = tool;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
		};

		const initHost = createWorkflowHost({
			backend,
			teamNoun: "the ledger",
			entryType: "work-now",
		});
		initHost(fakeExtensionApi as unknown as ExtensionAPI);

		const result = await registeredTool!.execute("get-1", {
			action: "get_work",
			work: "OMP-10",
		}, new AbortController().signal, () => {}, { taskDepth: 0 });

		const text = result.content[0].text;
		expect(text).toContain("OMP-10 Readback Target");
		expect(text).toContain("Readback description text");
		expect(text).toContain("SCOPE: packages/scoped-area");
		expect(text).toContain("ACCEPTANCE CRITERIA:");
		expect(text).toContain("- AC-1 first criteria item");
		expect(text).toContain("- AC-2 second criteria item");

		fs.rmSync(pendingDir, { recursive: true, force: true });
	});
	test("get_work renders explicit empty SCOPE and ACCEPTANCE CRITERIA (none) when cleared", async () => {
		const itemWithEmptyFields: WorkItemView = {
			work_id: "00000000-0000-7000-8000-000000000010",
			workspace_id: "00000000-0000-7000-8000-000000000001",
			alias: { work_id: "00000000-0000-7000-8000-000000000010", key: "OMP-11", primary: true, origin: "local" },
			state: "BACKLOG",
			revision: {
				revision_id: "00000000-0000-7000-8000-000000000013",
				work_id: "00000000-0000-7000-8000-000000000010",
				revision_number: 3,
				title: "Cleared Target",
				description: "Cleared description text",
				scope: "",
				acceptance_criteria: [],
				content_sha256: "0".repeat(64),
				created_by: "system",
				created_at: new Date().toISOString(),
			},
			candidate: null,
			project_id: null,
			archived: false,
		};

		const mockWorkflowView: WorkflowView = {
			item: itemWithEmptyFields,
			relations: [],
			receipts: [],
			close_attempts: [],
			audit_manifest: null,
			auditor_launches: [],
			close_attempt_events: [],
			checkpoint_deliveries: [],
			project: null,
		};

		const mockFetch = async (input: RequestInfo | URL) => {
			const url = String(input);
			if (url.includes("/v1/work-items/OMP-11/workflow")) {
				return new Response(JSON.stringify(mockWorkflowView), { status: 200 });
			}
			if (url.includes("/v1/work-items/OMP-11")) {
				return new Response(JSON.stringify(itemWithEmptyFields), { status: 200 });
			}
			if (url.includes("/tree")) {
				return new Response(JSON.stringify({ workspace_id: "00000000-0000-7000-8000-000000000001", items: [itemWithEmptyFields], relations: [], projects: [] }), { status: 200 });
			}
			return new Response("not found", { status: 404 });
		};

		const pendingDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-pending-cleared-test-"));
		const backend = createWorkBackend({
			baseUrl: "http://127.0.0.1:54322",
			workspaceId: "00000000-0000-7000-8000-000000000001",
			ownerId: "00000000-0000-7000-8000-000000000002",
		}, () => "test-token", mockFetch, pendingDir);

		let registeredTool: { execute: (id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: () => void, ctx: Record<string, unknown>) => Promise<{ content: { type: string; text: string }[] }> } | null = null;
		const fakeExtensionApi = {
			zod: z,
			registerTool(tool: typeof registeredTool) {
				registeredTool = tool;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
		};

		const initHost = createWorkflowHost({
			backend,
			teamNoun: "the ledger",
			entryType: "work-now",
		});
		initHost(fakeExtensionApi as unknown as ExtensionAPI);

		const result = await registeredTool!.execute("get-empty", {
			action: "get_work",
			work: "OMP-11",
		}, new AbortController().signal, () => {}, { taskDepth: 0 });

		const text = result.content[0].text;
		expect(text).toContain("OMP-11 Cleared Target");
		expect(text).toContain("SCOPE: ");
		expect(text).toContain("ACCEPTANCE CRITERIA:\n(none)");

		fs.rmSync(pendingDir, { recursive: true, force: true });
	});

	test("host revise_work handles backend CAS conflict by refetching and returning fresh preview, or failing closed if refetch fails", async () => {
		resetConfirmations({ resetShared: true });
		let currentRevisionId = "00000000-0000-7000-8000-000000000010";
		let shouldThrowCasConflict = false;
		let shouldThrowNonConflict = false;
		let refetchFails = false;
		let inReviseWork = false;

		const testItem = (revId: string): WorkItemView => ({
			work_id: "00000000-0000-7000-8000-000000000001",
			workspace_id: "00000000-0000-7000-8000-000000000000",
			alias: { work_id: "00000000-0000-7000-8000-000000000001", key: "OMP-100", primary: true, origin: "local" },
			state: "BACKLOG",
			revision: {
				revision_id: revId,
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_number: 1,
				title: "Original Title",
				description: "Original Description",
				scope: "Original Scope",
				acceptance_criteria: ["Criterion 1"],
				content_sha256: "0".repeat(64),
				created_by: "system",
				created_at: new Date().toISOString(),
			},
			candidate: null,
			project_id: null,
			archived: false,
		});

		const mockBackend = {
			markerFile: ".work-project",
			cacheFile: "work-now.json",
			evidenceKinds: ["plan", "verification", "closeout"],
			queueNoun: "TRIAGE",
			scopeFix: "fix",
			bookendTitle: "title",
			findIssue: async () => ({ id: "00000000-0000-7000-8000-000000000001", key: "OMP-100", title: "Original Title" }),
			workClient: {
				workItem: async () => {
					if (inReviseWork && refetchFails) {
						throw new Error("refetch network failure");
					}
					if (inReviseWork) {
						return testItem("00000000-0000-7000-8000-000000000020");
					}
					return testItem(currentRevisionId);
				},
			},
			reviseWork: async (_issue: unknown, _fields: { expected_revision_id?: string }) => {
				inReviseWork = true;
				if (shouldThrowNonConflict) {
					throw new WorkError("invalid_request", 400, ["diagnostic mentions revision_conflict"]);
				}
				if (shouldThrowCasConflict) {
					throw new WorkError("revision_conflict", 409, ["backend CAS mismatch"]);
				}
			},
		};

		let registeredTool: { execute: (id: string, params: Record<string, unknown>, signal: AbortSignal, onUpdate: () => void, ctx: Record<string, unknown>) => Promise<{ content: { type: string; text: string }[] }> } | null = null;
		const fakeExtensionApi = {
			zod: z,
			registerTool(tool: typeof registeredTool) {
				registeredTool = tool;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
		};

		const initHost = createWorkflowHost({
			backend: mockBackend as unknown as Parameters<typeof createWorkflowHost>[0]["backend"],
			teamNoun: "the ledger",
			entryType: "work-now",
		});
		initHost(fakeExtensionApi as unknown as ExtensionAPI);

		// 1. Get initial preview bound to rev-10
		inReviseWork = false;
		currentRevisionId = "00000000-0000-7000-8000-000000000010";
		const p1 = await registeredTool!.execute("call-1", {
			action: "revise_work",
			work: "OMP-100",
			scope: "New Scope",
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		const m1 = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(p1.content[0].text);
		expect(m1).not.toBeNull();
		const confId1 = m1![1];

		// 2. Pre-confirm lookup sees rev-10 (passes confirmWrite), then reviseWork throws CAS conflict,
		// and catch block refetch succeeds with rev-20 -> returns fresh preview bound to rev-20
		shouldThrowCasConflict = true;
		inReviseWork = false;
		const c1 = await registeredTool!.execute("call-2", {
			action: "revise_work",
			work: "OMP-100",
			scope: "New Scope",
			confirm: true,
			confirmation_id: confId1,
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		expect(c1.content[0].text).toContain("REFUSED — revision conflict");
		expect(c1.content[0].text).toContain("00000000-0000-7000-8000-000000000020");
		expect(c1.content[0].text).toContain("CONFIRM REQUIRED — nothing written.");

		const m2 = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(c1.content[0].text);
		expect(m2).not.toBeNull();
		const confId2 = m2![1];

		// 3. Confirming fresh preview succeeds when CAS conflict resolves
		shouldThrowCasConflict = false;
		inReviseWork = false;
		currentRevisionId = "00000000-0000-7000-8000-000000000020";
		const c2 = await registeredTool!.execute("call-3", {
			action: "revise_work",
			work: "OMP-100",
			scope: "New Scope",
			confirm: true,
			confirmation_id: confId2,
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		expect(c2.content[0].text).toBe("OMP-100 revised");

		// 4. Pre-confirm lookup sees rev-20, then reviseWork throws CAS conflict and refetch fails -> fails closed
		inReviseWork = false;
		const p3 = await registeredTool!.execute("call-4", {
			action: "revise_work",
			work: "OMP-100",
			scope: "Another Scope",
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		const m3 = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(p3.content[0].text);
		const confId3 = m3![1];

		shouldThrowCasConflict = true;
		refetchFails = true;
		inReviseWork = false;
		const failCloseResult = await registeredTool!.execute("call-5", {
			action: "revise_work",
			work: "OMP-100",
			scope: "Another Scope",
			confirm: true,
			confirmation_id: confId3,
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		expect(failCloseResult.content[0].text).toContain("REFUSED — revision conflict");
		expect(failCloseResult.content[0].text).toContain("refetch network failure");
		expect(failCloseResult.content[0].text).not.toContain("CONFIRM REQUIRED");

		// 5. Non-conflict WorkError passes through without minting replacement preview
		inReviseWork = false;
		shouldThrowCasConflict = false;
		shouldThrowNonConflict = true;
		refetchFails = false;
		const p4 = await registeredTool!.execute("call-6", {
			action: "revise_work",
			work: "OMP-100",
			title: "Non Conflict Test",
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		const m4 = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(p4.content[0].text);
		const confId4 = m4![1];

		const nonConflictResult = await registeredTool!.execute("call-7", {
			action: "revise_work",
			work: "OMP-100",
			title: "Non Conflict Test",
			confirm: true,
			confirmation_id: confId4,
		}, new AbortController().signal, () => {}, { taskDepth: 0 });
		expect(nonConflictResult.content[0].text).toContain("invalid_request");
		expect(nonConflictResult.content[0].text).not.toContain("CONFIRM REQUIRED");
	});
});
