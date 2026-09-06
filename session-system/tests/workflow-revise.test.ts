import { describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { z } from "zod";
import { loadExtensions, type ExtensionAPI, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { WorkError, type EvidenceReceipt, type WorkItemView, type WorkflowView } from "@oh-my-pi/pi-work-client";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { resetConfirmations } from "../extensions/workflow/confirm";
import { createWorkflowHost } from "../extensions/workflow/host";
import { buildPlanPacket, createWorkBackend } from "../extensions/workflow/work";

interface RegisteredToolSpec {
	name: string;
	description: string;
	parameters: z.ZodObject<Record<string, z.ZodTypeAny>>;
	execute: (
		toolCallId: string,
		params: Record<string, unknown>,
		signal?: unknown,
		onUpdate?: unknown,
		ctx?: ExtensionContext,
	) => Promise<{ content: { type: string; text: string }[]; details?: Record<string, unknown> }>;
}

interface RevisePayloadData {
	work_id: string;
	expected_revision_id: string;
	revision: {
		work_id: string;
		revision_id: string;
		revision_number: number;
		title: string;
		description: string;
		scope: string;
		acceptance_criteria: string[];
		content_sha256: string;
	};
}

function temporaryCacheFile(): string {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-revise-cache-"));
	return path.join(dir, "cache.json");
}

describe("workflow revise_work structured amendment", () => {
	test("work-now extension schema exposes scope, acceptance_criteria, and expected_revision_id (AC-6)", async () => {
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const extPath = path.join(repoRoot, "session-system/extensions/work-now.ts");
		const tempConfig = fs.mkdtempSync(path.join(os.tmpdir(), "work-ext-schema-test-"));
		try {
			const workDir = path.join(tempConfig, "omp-work");
			fs.mkdirSync(workDir, { recursive: true });
			fs.writeFileSync(
				path.join(workDir, "client.json"),
				JSON.stringify({
					base_url: "http://127.0.0.1:54322",
					workspace_id: "00000000-0000-7000-8000-000000000001",
					owner_id: "00000000-0000-7000-8000-000000000002",
				}),
			);
			const oldXdg = process.env.XDG_CONFIG_HOME;
			process.env.XDG_CONFIG_HOME = tempConfig;
			try {
				const result = await loadExtensions([extPath], repoRoot);
				expect(result.errors).toEqual([]);
				expect(result.extensions).toHaveLength(1);
				const ext = result.extensions[0]!;
				expect(ext.tools.has("work")).toBe(true);
				const workTool = ext.tools.get("work");
				expect(workTool).toBeDefined();
				const parsed = workTool!.definition.parameters({
					action: "revise_work",
					work: "OMP-100",
					scope: "Live Scope",
					acceptance_criteria: ["Live AC 1", "Live AC 2"],
					expected_revision_id: "00000000-0000-7000-8000-000000000010",
				});
				expect(parsed).toEqual(
					expect.objectContaining({
						action: "revise_work",
						work: "OMP-100",
						scope: "Live Scope",
						acceptance_criteria: ["Live AC 1", "Live AC 2"],
						expected_revision_id: "00000000-0000-7000-8000-000000000010",
					}),
				);
			} finally {
				if (oldXdg === undefined) delete process.env.XDG_CONFIG_HOME;
				else process.env.XDG_CONFIG_HOME = oldXdg;
			}
		} finally {
			fs.rmSync(tempConfig, { recursive: true, force: true });
		}
	});

	test("tool parameters schema includes scope, acceptance_criteria, and expected_revision_id", () => {
		let registeredTool: RegisteredToolSpec | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: RegisteredToolSpec) => {
				if (spec.name === "work") registeredTool = spec;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
		} as unknown as ExtensionAPI;

		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			queueNoun: "decision queue",
			markerFile: ".work-project",
			reviewKind: "review",
			findIssue: vi.fn(),
			reviseWork: vi.fn(),
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		expect(registeredTool).toBeDefined();
		const shape = registeredTool!.parameters.shape;
		expect(shape.scope).toBeDefined();
		expect(shape.acceptance_criteria).toBeDefined();
		expect(shape.expected_revision_id).toBeDefined();
	});

	test("revise_work rejects when no modifiable field is provided", async () => {
		let registeredTool: RegisteredToolSpec | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: RegisteredToolSpec) => {
				if (spec.name === "work") registeredTool = spec;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
		} as unknown as ExtensionAPI;

		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			queueNoun: "decision queue",
			markerFile: ".work-project",
			reviewKind: "review",
			findIssue: vi.fn().mockResolvedValue({ id: "w-1", key: "OMP-100", title: "Original Title" }),
			reviseWork: vi.fn(),
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = { taskDepth: 0 } as unknown as ExtensionContext;
		const result = await registeredTool!.execute("call-1", { action: "revise_work", work: "OMP-100" }, undefined, undefined, fakeCtx);
		expect(result.content[0].text).toContain("title, description, scope, and/or acceptance_criteria required");
	});

	test("revise_work generates two-phase preview for structured scope and criteria", async () => {
		resetConfirmations({ resetShared: true });
		let registeredTool: RegisteredToolSpec | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: RegisteredToolSpec) => {
				if (spec.name === "work") registeredTool = spec;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
		} as unknown as ExtensionAPI;

		const mockWorkItem: WorkItemView = {
			work_id: "00000000-0000-7000-8000-000000000001",
			workspace_id: "00000000-0000-7000-8000-000000000000",
			state: "BACKLOG",
			project_id: null,
			archived: false,
			current_candidate_id: null,
			alias: {
				work_id: "00000000-0000-7000-8000-000000000001",
				key: "OMP-100",
				primary: true,
				origin: "local",
			},
			revision: {
				revision_id: "00000000-0000-7000-8000-000000000010",
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_number: 1,
				title: "Original Title",
				description: "Original Description",
				scope: "Original Scope",
				acceptance_criteria: ["Criterion 1"],
				content_sha256: "0000000000000000000000000000000000000000000000000000000000000000",
				created_by: "test",
				created_at: new Date().toISOString(),
			},
		};
		const mockWorkClient = {
			workItem: vi.fn().mockResolvedValue(mockWorkItem),
		};
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			queueNoun: "decision queue",
			markerFile: ".work-project",
			reviewKind: "review",
			findIssue: vi.fn().mockResolvedValue({ id: mockWorkItem.work_id, key: "OMP-100", title: "Original Title" }),
			reviseWork: vi.fn().mockResolvedValue(undefined),
			workClient: mockWorkClient,
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = { taskDepth: 0 } as unknown as ExtensionContext;

		// Phase 1: Preview call
		const previewParams = {
			action: "revise_work",
			work: "OMP-100",
			scope: "Updated Scope",
			acceptance_criteria: ["New AC 1", "New AC 2"],
		};
		const previewRes = await registeredTool!.execute("call-1", previewParams, undefined, undefined, fakeCtx);
		const previewText = previewRes.content[0].text;
		expect(previewText).toContain("CONFIRM REQUIRED");
		expect(previewText).toContain("OMP-100 Original Title");
		expect(previewText).toContain("[bound revision: 00000000-0000-7000-8000-000000000010]");
		expect(previewText).toContain('→ new scope: "Updated Scope"');
		expect(previewText).toContain("→ new acceptance criteria:\n- New AC 1\n- New AC 2");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(previewText);
		expect(match).not.toBeNull();
		const confirmationId = match![1];

		// Phase 2: Confirm call
		const confirmRes = await registeredTool!.execute(
			"call-2",
			{
				...previewParams,
				confirm: true,
				confirmation_id: confirmationId,
			},
			undefined,
			undefined,
			fakeCtx,
		);
		expect(confirmRes.content[0].text).toBe("OMP-100 revised");
		expect(mockBackend.reviseWork).toHaveBeenCalledWith(
			expect.objectContaining({ key: "OMP-100" }),
			expect.objectContaining({
				scope: "Updated Scope",
				acceptance_criteria: ["New AC 1", "New AC 2"],
				expected_revision_id: "00000000-0000-7000-8000-000000000010",
			}),
		);
	});

	test("revise_work handles stale preview conflict by issuing fresh preview", async () => {
		resetConfirmations({ resetShared: true });
		let registeredTool: RegisteredToolSpec | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: RegisteredToolSpec) => {
				if (spec.name === "work") registeredTool = spec;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
		} as unknown as ExtensionAPI;

		let currentRevId = "00000000-0000-7000-8000-000000000010";
		const mockWorkItem = {
			work_id: "00000000-0000-7000-8000-000000000001",
			workspace_id: "00000000-0000-7000-8000-000000000000",
			state: "BACKLOG",
			project_id: null,
			archived: false,
			current_candidate_id: null,
			alias: {
				work_id: "00000000-0000-7000-8000-000000000001",
				key: "OMP-100",
				primary: true,
				origin: "local" as const,
			},
			revision: {
				get revision_id() {
					return currentRevId;
				},
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_number: 1,
				title: "Original Title",
				description: "Original Description",
				scope: "Original Scope",
				acceptance_criteria: ["Criterion 1"],
				content_sha256: "0000000000000000000000000000000000000000000000000000000000000000",
				created_by: "test",
				created_at: new Date().toISOString(),
			},
		} as unknown as WorkItemView;
		const mockWorkClient = {
			workItem: vi.fn().mockImplementation(async () => mockWorkItem),
		};
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			queueNoun: "decision queue",
			markerFile: ".work-project",
			reviewKind: "review",
			findIssue: vi.fn().mockResolvedValue({ id: mockWorkItem.work_id, key: "OMP-100", title: "Original Title" }),
			reviseWork: vi.fn().mockImplementation(async (_issue: unknown, fields: { expected_revision_id?: string }) => {
				if (fields.expected_revision_id && fields.expected_revision_id !== currentRevId) {
					throw new WorkError("revision_conflict", 409, ["revision conflict"]);
				}
			}),
			workClient: mockWorkClient,
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = { taskDepth: 0 } as unknown as ExtensionContext;

		// Phase 1: Preview generated on revision 00000000-0000-7000-8000-000000000010
		const previewParams = {
			action: "revise_work",
			work: "OMP-100",
			title: "New Title",
		};
		const previewRes = await registeredTool!.execute("call-1", previewParams, undefined, undefined, fakeCtx);
		const previewText = previewRes.content[0].text;
		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(previewText);
		const confirmationId = match![1];

		// Revision changes in background before confirm lands
		currentRevId = "00000000-0000-7000-8000-000000000020";

		// Phase 2: Confirm call fails with revision_conflict and returns a fresh preview
		const confirmRes = await registeredTool!.execute(
			"call-2",
			{
				...previewParams,
				confirm: true,
				confirmation_id: confirmationId,
			},
			undefined,
			undefined,
			fakeCtx,
		);
		const conflictText = confirmRes.content[0].text;
		expect(conflictText).toContain("revision_conflict: target revision moved since preview was generated");
		expect(conflictText).toContain("[bound revision: 00000000-0000-7000-8000-000000000020]");
		expect(conflictText).toContain("CONFIRM REQUIRED");

		const matchConflict = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(conflictText);
		expect(matchConflict).not.toBeNull();
		const replacementConfirmationId = matchConflict![1];

		// Phase 3: Caller repeats the exact original parameters with confirm:true and the replacement confirmation_id
		const confirmRes2 = await registeredTool!.execute(
			"call-3",
			{
				...previewParams,
				confirm: true,
				confirmation_id: replacementConfirmationId,
			},
			undefined,
			undefined,
			fakeCtx,
		);
		expect(confirmRes2.content[0].text).toBe("OMP-100 revised");
		expect(mockBackend.reviseWork).toHaveBeenCalledWith(
			expect.objectContaining({ key: "OMP-100" }),
			expect.objectContaining({
				title: "New Title",
				expected_revision_id: "00000000-0000-7000-8000-000000000020",
			}),
		);
	});

	test("revise_work handles stale preview conflict when expected_revision_id was explicitly supplied in initial params", async () => {
		resetConfirmations({ resetShared: true });
		let registeredTool: RegisteredToolSpec | undefined;
		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: RegisteredToolSpec) => {
				if (spec.name === "work") registeredTool = spec;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: () => {},
			sendMessage: () => {},
		} as unknown as ExtensionAPI;

		let currentRevId = "00000000-0000-7000-8000-000000000010";
		const mockWorkItem = {
			work_id: "00000000-0000-7000-8000-000000000001",
			workspace_id: "00000000-0000-7000-8000-000000000000",
			state: "BACKLOG",
			project_id: null,
			archived: false,
			current_candidate_id: null,
			alias: {
				work_id: "00000000-0000-7000-8000-000000000001",
				key: "OMP-100",
				primary: true,
				origin: "local" as const,
			},
			revision: {
				get revision_id() {
					return currentRevId;
				},
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_number: 1,
				title: "Original Title",
				description: "Original Description",
				scope: "Original Scope",
				acceptance_criteria: ["Criterion 1"],
				content_sha256: "0000000000000000000000000000000000000000000000000000000000000000",
				created_by: "test",
				created_at: new Date().toISOString(),
			},
		} as unknown as WorkItemView;
		const mockWorkClient = {
			workItem: vi.fn().mockImplementation(async () => mockWorkItem),
		};
		const mockBackend = {
			cacheFile: temporaryCacheFile(),
			queueNoun: "decision queue",
			markerFile: ".work-project",
			reviewKind: "review",
			findIssue: vi.fn().mockResolvedValue({ id: mockWorkItem.work_id, key: "OMP-100", title: "Original Title" }),
			reviseWork: vi.fn().mockImplementation(async (_issue: unknown, fields: { expected_revision_id?: string }) => {
				if (fields.expected_revision_id && fields.expected_revision_id !== currentRevId) {
					throw new WorkError("revision_conflict", 409, ["revision conflict"]);
				}
			}),
			workClient: mockWorkClient,
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		const fakeCtx = { taskDepth: 0 } as unknown as ExtensionContext;

		// Phase 1: Caller explicitly supplies expected_revision_id R1 in initial parameters
		const previewParams = {
			action: "revise_work",
			work: "OMP-100",
			title: "New Title",
			expected_revision_id: "00000000-0000-7000-8000-000000000010",
		};
		const previewRes = await registeredTool!.execute("call-1", previewParams, undefined, undefined, fakeCtx);
		const previewText = previewRes.content[0].text;
		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(previewText);
		const confirmationId = match![1];

		// Revision moves to R2 in background
		currentRevId = "00000000-0000-7000-8000-000000000020";

		// Phase 2: Confirm call fails with revision_conflict and returns replacement preview
		const confirmRes = await registeredTool!.execute(
			"call-2",
			{
				...previewParams,
				confirm: true,
				confirmation_id: confirmationId,
			},
			undefined,
			undefined,
			fakeCtx,
		);
		const conflictText = confirmRes.content[0].text;
		expect(conflictText).toContain("revision_conflict: target revision moved since preview was generated");
		expect(conflictText).toContain("[bound revision: 00000000-0000-7000-8000-000000000020]");

		const matchConflict = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(conflictText);
		expect(matchConflict).not.toBeNull();
		const replacementConfirmationId = matchConflict![1];

		// Phase 3: Caller repeats the original parameters (which still had expected_revision_id: R1) with the replacement confirmation_id
		const confirmRes2 = await registeredTool!.execute(
			"call-3",
			{
				...previewParams,
				confirm: true,
				confirmation_id: replacementConfirmationId,
			},
			undefined,
			undefined,
			fakeCtx,
		);
		expect(confirmRes2.content[0].text).toBe("OMP-100 revised");
		expect(mockBackend.reviseWork).toHaveBeenCalledWith(
			expect.objectContaining({ key: "OMP-100" }),
			expect.objectContaining({
				title: "New Title",
				expected_revision_id: "00000000-0000-7000-8000-000000000020",
			}),
		);
	});

	test("createWorkBackend.reviseWork preserves unamended fields and updates scope and criteria", async () => {
		const WS = "00000000-0000-7000-8000-000000000000";
		const OWNER = "00000000-0000-7000-8000-000000000002";
		const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-backend-revise-"));

		try {
			const originalItem: WorkItemView = {
				work_id: "00000000-0000-7000-8000-000000000001",
				workspace_id: WS,
				state: "BACKLOG",
				project_id: null,
				archived: false,
				current_candidate_id: null,
				alias: {
					work_id: "00000000-0000-7000-8000-000000000001",
					key: "OMP-100",
					primary: true,
					origin: "local",
				},
				revision: {
					revision_id: "00000000-0000-7000-8000-000000000010",
					work_id: "00000000-0000-7000-8000-000000000001",
					revision_number: 1,
					title: "Original Title",
					description: "Original Description",
					scope: "Original Scope",
					acceptance_criteria: ["Criterion 1", "Criterion 2"],
					content_sha256: "0000000000000000000000000000000000000000000000000000000000000000",
					created_by: "test",
					created_at: new Date().toISOString(),
				},
			};

			let lastPayload: RevisePayloadData | undefined;

			const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
				const url = String(input);
				const json = (body: unknown) =>
					new Response(JSON.stringify(body), {
						status: 200,
						headers: { "Content-Type": "application/json" },
					});
				if (url.includes("/work-items/OMP-100")) {
					return json(originalItem);
				}
				if (url.includes("/commands") && init?.method === "POST") {
					const bodyText = init.body;
					if (typeof bodyText === "string") {
						const parsed = JSON.parse(bodyText) as { command: { payload: RevisePayloadData } };
						lastPayload = parsed.command.payload;
					}
					return json({
						receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000099" },
						result: {
							type: "revise_work",
							revision_id: "00000000-0000-7000-8000-000000000020",
							changed: true,
						},
					});
				}
				return new Response("not found", { status: 404 });
			};

			const backend = createWorkBackend(
				{ baseUrl: "http://127.0.0.1:9999", workspaceId: WS, ownerId: OWNER },
				() => "mock-token",
				mockFetch as unknown as typeof fetch,
				tempDir,
			);

			const nowRef = {
				id: originalItem.work_id,
				key: "OMP-100",
				title: "Original Title",
			};

			// Case 1: Only amend scope — title, description, acceptance_criteria must be preserved
			await backend.reviseWork(nowRef, { scope: "New Scope Only" });
			expect(lastPayload).toBeDefined();
			expect(lastPayload!.revision.scope).toBe("New Scope Only");
			expect(lastPayload!.revision.title).toBe("Original Title");
			expect(lastPayload!.revision.description).toBe("Original Description");
			expect(lastPayload!.revision.acceptance_criteria).toEqual(["Criterion 1", "Criterion 2"]);
			expect(lastPayload!.revision.revision_number).toBe(2);

			// Case 2: Only amend acceptance_criteria — title, description, scope must be preserved
			await backend.reviseWork(nowRef, { acceptance_criteria: ["Updated Criterion A"] });
			expect(lastPayload).toBeDefined();
			expect(lastPayload!.revision.acceptance_criteria).toEqual(["Updated Criterion A"]);
			expect(lastPayload!.revision.title).toBe("Original Title");
			expect(lastPayload!.revision.description).toBe("Original Description");
			expect(lastPayload!.revision.scope).toBe("Original Scope");

			// Case 3: Revision conflict on expected_revision_id mismatch
			await expect(
				backend.reviseWork(nowRef, {
					title: "Failing Revision",
					expected_revision_id: "00000000-0000-7000-8000-999999999999",
				}),
			).rejects.toThrow("revision_conflict");
		} finally {
			fs.rmSync(tempDir, { recursive: true, force: true });
		}
	});

	test("causal integration: structured amendment invalidates candidate, clears plan packet, seals amended criteria, and rejects old-revision grants (AC-4)", async () => {
		const WS = "00000000-0000-7000-8000-000000000000";
		const OWNER = "00000000-0000-7000-8000-000000000002";
		const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-causal-revise-"));

		try {
			let currentRevisionId = "00000000-0000-7000-8000-000000000010";
			let currentCandidateId: string | null = "00000000-0000-7000-8000-000000000030";
			let currentScope = "Original Scope";
			let currentCriteria = ["Original AC 1"];
			let currentRevisionNumber = 1;

			const planReceipt1: EvidenceReceipt = {
				receipt_id: "00000000-0000-7000-8000-000000000031",
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_id: "00000000-0000-7000-8000-000000000010",
				candidate_id: "00000000-0000-7000-8000-000000000030",
				kind: "plan",
				payload: { body: "plan body", paths: ["src/index.ts"] },
				payload_sha256: "1".repeat(64),
				artifact_sha256: null,
				issuer: "test",
				issued_at: "2026-09-06T00:00:00Z",
				candidate_sha256: "3".repeat(64),
				independent: false,
			};

			const mockFetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
				const url = String(input);
				const json = (body: unknown, status = 200) =>
					new Response(JSON.stringify(body), {
						status,
						headers: { "Content-Type": "application/json" },
					});

				if (url.includes("/workflow")) {
					const candidate = currentCandidateId
						? {
								candidate_id: currentCandidateId,
								work_id: "00000000-0000-7000-8000-000000000001",
								revision_id: currentRevisionId,
								candidate_sha256: "3".repeat(64),
								commit_sha: null,
								kind: "planned" as const,
								allocated_at: "2026-09-06T00:00:00Z",
							}
						: null;
					const view: WorkflowView = {
						item: {
							work_id: "00000000-0000-7000-8000-000000000001",
							workspace_id: WS,
							state: "BACKLOG",
							project_id: null,
							archived: false,
							current_candidate_id: currentCandidateId,
							alias: {
								work_id: "00000000-0000-7000-8000-000000000001",
								key: "OMP-100",
								primary: true,
								origin: "local",
							},
							revision: {
								revision_id: currentRevisionId,
								work_id: "00000000-0000-7000-8000-000000000001",
								revision_number: currentRevisionNumber,
								title: "Item Title",
								description: "## Acceptance criteria\n- AC old",
								scope: currentScope,
								acceptance_criteria: currentCriteria,
								content_sha256: "0".repeat(64),
								created_by: "test",
								created_at: "2026-09-06T00:00:00Z",
							},
							candidate,
						},
						receipts: [
							planReceipt1,
							...(currentCandidateId === "00000000-0000-7000-8000-000000000040"
								? [
										{
											receipt_id: "00000000-0000-7000-8000-000000000041",
											work_id: "00000000-0000-7000-8000-000000000001",
											revision_id: currentRevisionId,
											candidate_id: "00000000-0000-7000-8000-000000000040",
											kind: "plan" as const,
											payload: { body: "plan body 2", paths: ["src/index.ts"] },
											payload_sha256: "2".repeat(64),
											artifact_sha256: null,
											issuer: "test",
											issued_at: "2026-09-06T00:01:00Z",
											candidate_sha256: "4".repeat(64),
											independent: false,
										},
									]
								: []),
						],
						relations: [],
					};
					return json(view);
				}

				if (url.includes("/work-items/OMP-100")) {
					const item: WorkItemView = {
						work_id: "00000000-0000-7000-8000-000000000001",
						workspace_id: WS,
						state: "BACKLOG",
						project_id: null,
						archived: false,
						current_candidate_id: currentCandidateId,
						alias: {
							work_id: "00000000-0000-7000-8000-000000000001",
							key: "OMP-100",
							primary: true,
							origin: "local",
						},
						revision: {
							revision_id: currentRevisionId,
							work_id: "00000000-0000-7000-8000-000000000001",
							revision_number: currentRevisionNumber,
							title: "Item Title",
							description: "## Acceptance criteria\n- AC old",
							scope: currentScope,
							acceptance_criteria: currentCriteria,
							content_sha256: "0".repeat(64),
							created_by: "test",
							created_at: "2026-09-06T00:00:00Z",
						},
					};
					return json(item);
				}

				if (url.includes("/execution")) {
					return json({
						grant: { grant_id: "00000000-0000-7000-8000-000000000077", grant_version: 1 },
						items: [],
					});
				}

				if (url.includes("/commands") && init?.method === "POST") {
					const bodyText = init.body;
					const parsed = typeof bodyText === "string" ? JSON.parse(bodyText) : {};
					const cmd = parsed.command;

					if (cmd.type === "revise_work") {
						if (cmd.payload.expected_revision_id !== currentRevisionId) {
							return json({ error: { code: "revision_conflict", message: "revision conflict" } }, 409);
						}
						currentRevisionId = "00000000-0000-7000-8000-000000000020";
						currentCandidateId = null; // store._revise invalidates candidate
						currentScope = cmd.payload.revision.scope;
						currentCriteria = cmd.payload.revision.acceptance_criteria;
						currentRevisionNumber = 2;
						return json({
							receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000099" },
							result: { type: "revise_work", revision_id: currentRevisionId, changed: true },
						});
					}

					if (cmd.type === "seal_execution_criteria") {
						if (cmd.payload.expected_revision_id !== currentRevisionId) {
							return json({ error: { code: "revision_conflict", message: "grant bound to old revision" } }, 409);
						}
						return json({
							receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000098" },
							result: {
								type: "seal_execution_criteria",
								grant: { grant_id: cmd.payload.grant_id, grant_version: 2 },
								item: { work_id: cmd.payload.work_id, phase: "planning" },
								revision: { revision_id: currentRevisionId, acceptance_criteria: currentCriteria },
							},
						});
					}
				}
				return new Response("not found", { status: 404 });
			};

			const backend = createWorkBackend(
				{ baseUrl: "http://127.0.0.1:9999", workspaceId: WS, ownerId: OWNER },
				() => "mock-token",
				mockFetch as unknown as typeof fetch,
				tempDir,
			);

			// Step 1: Initial state has candidate and valid plan packet
			const initialWorkflow = await backend.workClient!.workflow("OMP-100");
			const initialPacket = buildPlanPacket(initialWorkflow);
			expect(initialPacket).toBeDefined();
			expect(initialPacket!.acceptanceCriteria).toEqual(["Original AC 1"]);

			// Step 2: Structured amendment is applied
			const nowRef = { id: "00000000-0000-7000-8000-000000000001", key: "OMP-100", title: "Item Title" };
			await backend.reviseWork(nowRef, {
				scope: "Amended Scope",
				acceptance_criteria: ["Amended AC 1", "Amended AC 2"],
			});

			// Step 3: Post-amendment state: candidate is invalidated and plan packet is undefined
			const postAmendmentWorkflow = await backend.workClient!.workflow("OMP-100");
			expect(postAmendmentWorkflow.item.current_candidate_id).toBeNull();
			expect(postAmendmentWorkflow.item.candidate).toBeNull();
			expect(postAmendmentWorkflow.item.revision.revision_id).toBe("00000000-0000-7000-8000-000000000020");
			expect(postAmendmentWorkflow.item.revision.scope).toBe("Amended Scope");
			expect(postAmendmentWorkflow.item.revision.acceptance_criteria).toEqual(["Amended AC 1", "Amended AC 2"]);

			const postAmendmentPacket = buildPlanPacket(postAmendmentWorkflow);
			expect(postAmendmentPacket).toBeUndefined(); // Candidate invalidation enforces replan

			// Step 4: Sealing criteria for old revision fails with revision_conflict
			await expect(
				backend.sealExecutionCriteria({
					grantId: "00000000-0000-7000-8000-000000000077",
					expectedGrantVersion: 1,
					workId: "00000000-0000-7000-8000-000000000001",
					expectedRevisionId: "00000000-0000-7000-8000-000000000010", // stale revision
					criteria: ["Amended AC 1", "Amended AC 2"],
					descriptionSha256: "0".repeat(64),
					judgeSha256: "0".repeat(64),
				}),
			).rejects.toThrow("revision_conflict");

			// Step 5: Sealing criteria for new revision succeeds and binds amended criteria
			const sealRes = await backend.sealExecutionCriteria({
				grantId: "00000000-0000-7000-8000-000000000077",
				expectedGrantVersion: 1,
				workId: "00000000-0000-7000-8000-000000000001",
				expectedRevisionId: "00000000-0000-7000-8000-000000000020", // new revision
				criteria: ["Amended AC 1", "Amended AC 2"],
				descriptionSha256: "0".repeat(64),
				judgeSha256: "0".repeat(64),
			});
			expect(sealRes.sealedCriteria).toEqual(["Amended AC 1", "Amended AC 2"]);
			currentCandidateId = "00000000-0000-7000-8000-000000000040";
			const restampedWorkflow = await backend.workClient!.workflow("OMP-100");
			const restampedPacket = buildPlanPacket(restampedWorkflow);
			expect(restampedPacket).toBeDefined();
			expect(restampedPacket!.acceptanceCriteria).toEqual(["Amended AC 1", "Amended AC 2"]);
		} finally {
			fs.rmSync(tempDir, { recursive: true, force: true });
		}
	});
});
