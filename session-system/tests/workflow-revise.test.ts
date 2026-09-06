import { describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { z } from "zod";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { WorkError, type WorkItemView } from "@oh-my-pi/pi-work-client";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { resetConfirmations } from "../extensions/workflow/confirm";
import { createWorkflowHost } from "../extensions/workflow/host";
import { createWorkBackend } from "../extensions/workflow/work";

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
});
