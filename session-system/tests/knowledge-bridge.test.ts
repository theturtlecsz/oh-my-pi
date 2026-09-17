/**
 * session-system/tests/knowledge-bridge.test.ts
 * Focused tests for observable knowledge bridge contracts:
 * 1. Capture: settled tool_result & agent_end, restart dedupe, no native lineage fields,
 *    workspace scoping, outage tolerance (503/ECONNREFUSED) with at most one notice,
 *    nested session without witness skipped, SingleResult.outputPath artifact_locator.
 * 2. Fresh-task delivery: compile 200 bundle hash, bundle custom message injection,
 *    uses recording, bundle replay dedupe, compile 503/422/403 fail-closed degradation,
 *    tool_call toolName='task' input context prepending with witness vs undefined without.
 * 3. Outcome: exact native audit receipt outcome write after settle, 409 replay tolerance,
 *    receipt-null / non-audit skip, 503 transient failure persistence and session_start retry.
 * 4. Config: loopback requirement, budget default, bearer loading with scope checks.
 */
import { describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
	canonicalJson,
	sha256Hex,
	type AuditorLaunch,
	type EvidenceReceipt,
	type Fetch,
	type WorkClient,
	type WorkflowView,
} from "@oh-my-pi/pi-work-client";
import type { AgentEndEvent, ExtensionContext, ToolResultEvent } from "@oh-my-pi/pi-coding-agent";
import type { CloseAttemptOutcome, CloseEventView } from "../extensions/workflow/backend";
import {
	DEFAULT_KNOWLEDGE_BUDGET_BYTES,
	loadKnowledgeBearer,
	loadKnowledgeConfig,
	type KnowledgeClientConfig,
	type WorkClientConfig,
} from "../extensions/workflow/config";
import {
	buildNativeRecordIngestRequest,
	buildSourceObservation,
	buildSourceRef,
	CONTEXT_BUNDLE_IDENTITY_ENCODING,
	extractArtifactLocator,
	findInjectedBundle,
	findPendingCapture,
	isBundleReplayed,
	isToolResultCaptured,
	pendingCapturesInBranch,
	recordedUsesForBundle,
	registerKnowledgeBridge,
	retryPendingCaptures,
	retryPendingOutcomes,
	selectAuditReceiptForLaunch,
	stableId,
	validateKnowledgeBundlePayload,
	wasWorkerUsed,
	type KnowledgeBridgeDeps,
	type KnowledgeBudgetActual,
	type KnowledgeBudgetSpec,
	type KnowledgeExecutionIdentity,
	type OwnExecutionWitness,
	type PersistedPendingCapture,
} from "../extensions/workflow/knowledge-bridge";

// ---- Test Fixtures & Harness ------------------------------------------------

interface MockRequest {
	url: string;
	method: string;
	headers: Record<string, string>;
	body?: any;
}

const defaultEvent: CloseEventView = {
	eventId: "event-1",
	eventType: "settled",
	reasonCode: "ok",
	renderedText: "settled",
	renderedSha256: "0".repeat(64),
	requiresDelivery: false,
	requiresFreshAuthorization: false,
};

function makeFakeFetch(router: (req: MockRequest) => { status: number; body?: any }) {
	const requests: MockRequest[] = [];
	const fetchImpl: Fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
		const urlStr = typeof input === "string" ? input : input.toString();
		const rawHeaders = init?.headers ?? {};
		const headers: Record<string, string> = {};
		if (rawHeaders instanceof Headers) {
			rawHeaders.forEach((v, k) => {
				headers[k] = v;
			});
		} else if (Array.isArray(rawHeaders)) {
			for (const [k, v] of rawHeaders) headers[k] = v;
		} else {
			Object.assign(headers, rawHeaders);
		}

		let parsedBody: any = undefined;
		if (init?.body && typeof init.body === "string") {
			try {
				parsedBody = JSON.parse(init.body);
			} catch {
				parsedBody = init.body;
			}
		}

		const reqRecord: MockRequest = {
			url: urlStr,
			method: init?.method ?? "GET",
			headers,
			body: parsedBody,
		};
		requests.push(reqRecord);

		const response = router(reqRecord);
		return new Response(response.body ? JSON.stringify(response.body) : "", {
			status: response.status,
			headers: { "Content-Type": "application/json" },
		});
	}) as Fetch;

	return { fetchImpl, requests };
}

function makeMockSessionManager(sessionId: string, initialBranch: any[] = []) {
	const branch = [...initialBranch];
	return {
		getSessionId: () => sessionId,
		getBranch: () => branch,
		appendEntry: (type: string, data: any) => {
			branch.push({ id: `entry-${branch.length + 1}`, type: "custom", customType: type, data });
		},
	};
}

function makeMockPi(sessionManager: ReturnType<typeof makeMockSessionManager>) {
	const handlers = new Map<string, Array<(event: any, ctx: any) => any>>();
	const entries: Array<{ type: string; data: any }> = [];
	const sentMessages: Array<{ message: any; options?: any }> = [];
	const warnings: string[] = [];

	const pi = {
		on(type: string, handler: (event: any, ctx: any) => any) {
			const list = handlers.get(type) ?? [];
			list.push(handler);
			handlers.set(type, list);
		},
		appendEntry(customType: string, data: any) {
			entries.push({ type: customType, data });
			sessionManager.appendEntry(customType, data);
		},
		sendMessage(message: any, options?: any) {
			sentMessages.push({ message, options });
		},
		logger: {
			warn(msg: string) {
				warnings.push(msg);
			},
			info() {},
			error() {},
		},
	};

	async function emit(type: string, event: any, ctx: any) {
		for (const h of handlers.get(type) ?? []) {
			const res = await h({ type, ...event }, ctx);
			if (res !== undefined) return res;
		}
		return undefined;
	}

	return { pi, emit, entries, sentMessages, warnings, handlers };
}

function persistCustomMessage(
	sessionManager: { getBranch: () => unknown[] },
	message: { customType: string; content?: string; details?: unknown },
): void {
	const branch = sessionManager.getBranch() as Array<Record<string, unknown>>;
	branch.push({
		id: `entry-${branch.length + 1}`,
		type: "custom_message",
		customType: message.customType,
		content: message.content,
		details: message.details,
	});
}

const TEST_WORK_CONFIG: WorkClientConfig = {
	baseUrl: "http://127.0.0.1:18080",
	workspaceId: "00000000-0000-0000-0000-000000000001",
	ownerId: "00000000-0000-0000-0000-000000000002",
};

const TEST_KNOWLEDGE_CONFIG: KnowledgeClientConfig = {
	baseUrl: "http://127.0.0.1:18090",
	budgetLimit: 32768,
};

const TEST_IDENTITY: KnowledgeExecutionIdentity = {
	workspaceId: "00000000-0000-0000-0000-000000000001",
	repositoryId: "00000000-0000-0000-0000-000000000010",
	workId: "00000000-0000-0000-0000-000000000020",
	revisionId: "00000000-0000-0000-0000-000000000030",
	candidateId: "00000000-0000-0000-0000-000000000040",
	candidateSha256: "a".repeat(64),
	sourceRevision: "b".repeat(40),
	stage: "execute",
};

const MOCK_BUDGET_SPEC: KnowledgeBudgetSpec = {
	method: "utf8_bytes",
	limit: 32768,
	tokenizer_id: null,
};

/**
 * Test-only stand-in for the Python compiler's identity_canonical_json.
 * Production TS has no identity reconstruction; it verifies the retained bytes.
 * Only valid for ASCII/int-only content where TS canonicalJson equals Python bytes.
 */
function mockIdentityCanonicalJson(fields: {
	workspace_id: string;
	repository_id: string;
	work_id: string;
	revision_id: string;
	candidate_id: string | null;
	stage: string;
	snapshot_id: string | null;
	budget_spec: KnowledgeBudgetSpec;
	content: { mandatory: Record<string, unknown>; optional: Record<string, unknown> };
}): string {
	return canonicalJson({
		...fields,
		identity_encoding: CONTEXT_BUNDLE_IDENTITY_ENCODING,
		budget_spec: {
			method: fields.budget_spec.method,
			limit: fields.budget_spec.limit,
			tokenizer_id: fields.budget_spec.tokenizer_id ?? null,
		},
	});
}

/** BudgetActual exactly as the Python compiler emits it: no tokenizer_id, counts from the served bytes. */
function mockBudgetActual(
	spec: KnowledgeBudgetSpec,
	contentBytes: string,
	mandatory: Record<string, unknown>,
): KnowledgeBudgetActual {
	return {
		method: spec.method,
		limit: spec.limit,
		used: new TextEncoder().encode(contentBytes).length,
		mandatory_used: new TextEncoder().encode(canonicalJson(mandatory)).length,
		dropped_optional: [],
	};
}

function buildMockBundle(opts: {
	workspaceId?: string;
	repositoryId?: string;
	workId?: string;
	revisionId?: string;
	candidateId?: string | null;
	stage?: string;
	snapshotId?: string | null;
	budget?: KnowledgeBudgetSpec;
	mandatory?: Record<string, unknown>;
	optional?: Record<string, unknown>;
} = {}) {
	const workspace_id = opts.workspaceId ?? TEST_WORK_CONFIG.workspaceId;
	const repository_id = opts.repositoryId ?? TEST_IDENTITY.repositoryId;
	const work_id = opts.workId ?? TEST_IDENTITY.workId;
	const revision_id = opts.revisionId ?? TEST_IDENTITY.revisionId;
	const candidate_id = opts.candidateId !== undefined ? opts.candidateId : TEST_IDENTITY.candidateId ?? null;
	const stage = opts.stage ?? "execute";
	const snapshot_id = opts.snapshotId !== undefined ? opts.snapshotId : null;
	const budget: KnowledgeBudgetSpec = {
		method: opts.budget?.method ?? "utf8_bytes",
		limit: opts.budget?.limit ?? MOCK_BUDGET_SPEC.limit,
		tokenizer_id: opts.budget?.tokenizer_id !== undefined ? opts.budget.tokenizer_id : null,
	};
	const mandatory = opts.mandatory ?? { item: "mandatory facts" };
	const optional = opts.optional ?? { tip: "optional lesson" };

	const identity_canonical_json = mockIdentityCanonicalJson({
		workspace_id,
		repository_id,
		work_id,
		revision_id,
		candidate_id,
		stage,
		snapshot_id,
		budget_spec: budget,
		content: { mandatory, optional },
	});
	const bundle_sha256 = sha256Hex(identity_canonical_json);
	// Mock content is ASCII/int-only, so TS canonicalJson equals the Python bytes here.
	const content_canonical_json = canonicalJson({ mandatory, optional });

	return {
		bundle_id: "00000000-0000-0000-0000-000000000099",
		bundle_sha256,
		identity_encoding: CONTEXT_BUNDLE_IDENTITY_ENCODING,
		identity_canonical_json,
		content_canonical_json,
		workspace_id,
		repository_id,
		work_id,
		revision_id,
		candidate_id,
		stage,
		snapshot_id,
		budget: mockBudgetActual(budget, content_canonical_json, mandatory),
		mandatory,
		optional,
		enrichment_status: "applied",
		proposal_lineage: [
			"00000000-0000-0000-0000-000000000051",
			"00000000-0000-0000-0000-000000000052",
		],
		receipt_lineage: ["00000000-0000-0000-0000-000000000061"],
	};
}

const MOCK_BUNDLE = buildMockBundle();

function makeMockBackend(overrides: Partial<KnowledgeBridgeDeps["backend"]> = {}): KnowledgeBridgeDeps["backend"] {
	return {
		name: "work",
		serviceLabel: "Work Ledger",
		markerFile: ".work",
		scopeFix: "fix",
		workspaceId: TEST_WORK_CONFIG.workspaceId,
		cacheFile: "work-now.json",
		...overrides,
	} as any;
}

const AUDIT_RECEIPT_ID = "00000000-0000-0000-0000-000000000088";

function makeMockWorkflowView(opts: {
	launchId?: string;
	attemptId?: string;
	receipts?: EvidenceReceipt[];
	launches?: AuditorLaunch[];
} = {}): WorkflowView {
	const launchId = opts.launchId ?? "launch-1";
	const attemptId = opts.attemptId ?? "attempt-1";
	return {
		item: {
			work_id: TEST_IDENTITY.workId,
			workspace_id: TEST_IDENTITY.workspaceId,
			alias: { work_id: TEST_IDENTITY.workId, key: "OMP-1", primary: true, origin: "local" },
			state: "IN_PROGRESS",
			revision: {
				revision_id: TEST_IDENTITY.revisionId,
				work_id: TEST_IDENTITY.workId,
				revision_number: 1,
				title: "Test Work",
				description: "Test",
				scope: "session-system",
				acceptance_criteria: [],
				content_sha256: "0".repeat(64),
				created_by: "system",
				created_at: new Date().toISOString(),
			},
			candidate: {
				candidate_id: TEST_IDENTITY.candidateId!,
				candidate_sha256: TEST_IDENTITY.candidateSha256!,
				commit_sha: null,
			},
			project_id: null,
			archived: false,
		},
		relations: [],
		receipts: opts.receipts ?? [
			{
				receipt_id: AUDIT_RECEIPT_ID,
				work_id: TEST_IDENTITY.workId,
				revision_id: TEST_IDENTITY.revisionId,
				candidate_id: TEST_IDENTITY.candidateId!,
				kind: "audit",
				issuer: "work-service/auditor-settle",
				issued_at: new Date().toISOString(),
				payload: { launch_id: launchId },
				payload_sha256: "0".repeat(64),
				verdict: "PASS",
			},
		],
		close_attempts: [],
		audit_manifest: null,
		auditor_launches: opts.launches ?? [
			{
				launch_id: launchId,
				attempt_id: attemptId,
				manifest_id: "00000000-0000-0000-0000-000000000001",
				launch_number: 1,
				task_sha256: "0".repeat(64),
				tool_call_id: "call-1",
				reserved_at: new Date().toISOString(),
			},
		],
		close_attempt_events: [],
		checkpoint_deliveries: [],
		project: null,
	};
}

function makeMockWorkClient(viewOrFn: WorkflowView | ((key: string) => Promise<WorkflowView>)): WorkClient {
	return {
		workflow: typeof viewOrFn === "function" ? viewOrFn : async () => viewOrFn,
	} as unknown as WorkClient;
}

// ---- Test Suite -------------------------------------------------------------

describe("Knowledge Bridge — Capture Adapter (Requirement 1)", () => {
	it("POSTs /v1/ingest per settled tool_result with exact SourceObservation and no native lineage fields", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/ingest")) {
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-100");
		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const toolEvent = {
			toolCallId: "call-1",
			toolName: "bash",
			input: { command: "echo hello" },
			content: [{ type: "text", text: "hello\n" }],
			details: undefined,
			isError: false,
		};
		const ctx = {
			taskDepth: 0,
			sessionManager,
		};

		await emit("tool_result", toolEvent, ctx);

		expect(requests.length).toBe(1);
		const req = requests[0];
		expect(req.url).toBe("http://127.0.0.1:18090/v1/ingest");
		expect(req.method).toBe("POST");

		const body = req.body;
		expect(body.kind).toBe("native_record");
		expect(body.workspace_id).toBe(TEST_WORK_CONFIG.workspaceId);
		expect(body.repository_id).toBe(TEST_IDENTITY.repositoryId);
		expect(body.operation_id).toBeDefined();

		// SourceRef assertions
		const source = body.source_ref;
		expect(source.workspace_id).toBe(TEST_WORK_CONFIG.workspaceId);
		expect(source.repository_id).toBe(TEST_IDENTITY.repositoryId);
		expect(source.work_id).toBe(TEST_IDENTITY.workId);
		expect(source.revision_id).toBe(TEST_IDENTITY.revisionId);
		expect(source.candidate_id).toBe(TEST_IDENTITY.candidateId);
		expect(source.candidate_sha256).toBe(TEST_IDENTITY.candidateSha256);
		expect(source.source_revision).toBe(TEST_IDENTITY.sourceRevision);
		expect(source.repository_bound).toBe(true);
		expect(source.run_id).toBe("session-100");
		expect(source.producer).toBe("session-system/work-now:tool_result");

		// Crucial constraint: native lineage fields MUST BE ABSENT
		expect(source.native_validity_ref).toBeUndefined();
		expect(source.native_event_sha256).toBeUndefined();
		expect(source.previous_event_sha256).toBeUndefined();
		expect(source.sequence).toBeUndefined();

		// Observation assertions
		const obs = body.observation;
		expect(obs.kind).toBe("tool_output");
		expect(obs.source).toEqual(source);
		expect(obs.native_payload_sha256).toBeUndefined();
		expect(obs.payload_sha256).toBe(source.content_sha256);

		// Transcript receipt appended
		const captureEntry = entries.find(e => e.type === "work-now-capture");
		expect(captureEntry).toBeDefined();
		expect(captureEntry?.data.toolCallId).toBe("call-1");
		expect(captureEntry?.data.content_sha256).toBe(source.content_sha256);
	});

	it("restart replay with 'work-now-capture' entry present -> zero POSTs (restart-idempotent)", async () => {
		const payload = {
			toolCallId: "call-replayed",
			toolName: "read",
			input: { path: "foo.ts" },
			content: [{ type: "text", text: "bar" }],
			details: null,
			isError: false,
		};
		const contentSha256 = sha256Hex(canonicalJson(payload));

		// Simulate restart: existing transcript already has work-now-capture
		const sessionManager = makeMockSessionManager("session-101", [
			{
				id: "e-1",
				type: "custom",
				customType: "work-now-capture",
				data: {
					toolCallId: "call-replayed",
					content_sha256: contentSha256,
				},
			},
		]);

		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		await emit("tool_result", payload, { taskDepth: 0, sessionManager });

		// Zero POSTs
		expect(requests.length).toBe(0);
	});

	it("knowledge endpoint returns 503/ECONNREFUSED -> tool result unchanged, exactly one notice queued, no throw", async () => {
		let callCount = 0;
		const { fetchImpl } = makeFakeFetch(() => {
			callCount++;
			return { status: 503, body: { error: { code: "native_unavailable" } } };
		});

		const sessionManager = makeMockSessionManager("session-102");
		const { pi, emit } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const event1 = {
			toolCallId: "call-err-1",
			toolName: "bash",
			input: { command: "ls" },
			content: [{ type: "text", text: "dir1" }],
			details: undefined,
			isError: false,
		};
		const event2 = {
			toolCallId: "call-err-2",
			toolName: "bash",
			input: { command: "pwd" },
			content: [{ type: "text", text: "/home" }],
			details: undefined,
			isError: false,
		};

		// Should not throw on either call
		await emit("tool_result", event1, { taskDepth: 0, sessionManager });
		await emit("tool_result", event2, { taskDepth: 0, sessionManager });

		expect(callCount).toBe(2);
		expect(event1.content[0].text).toBe("dir1");
		expect(event1.isError).toBe(false);
		// Exactly one notice queued
		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("capture degraded");
	});

	it("tool_result at taskDepth>0 without witness -> no POST", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-103");
		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => undefined, // NO witness
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		await emit(
			"tool_result",
			{
				toolCallId: "call-nested",
				toolName: "read",
				input: { path: "a.txt" },
				content: [{ type: "text", text: "nested" }],
				details: undefined,
				isError: false,
			},
			{ taskDepth: 1, sessionManager },
		);

		expect(requests.length).toBe(0);
	});

	it("'task' toolResult with SingleResult.outputPath -> artifact_locator set", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-104");
		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const taskResultEvent = {
			toolCallId: "call-task-1",
			toolName: "task",
			input: { prompt: "run child", subagent_type: "worker" },
			content: [{ type: "text", text: "task done" }],
			details: {
				results: [
					{
						outputPath: "/home/user/artifacts/child-output.md",
					},
				],
			},
			isError: false,
		};

		await emit("tool_result", taskResultEvent, { taskDepth: 0, sessionManager });

		expect(requests.length).toBe(1);
		const body = requests[0].body;
		expect(body.observation.kind).toBe("execution_trace");
		expect(body.source_ref.artifact_locator).toBe("/home/user/artifacts/child-output.md");
	});

	it("buildSourceRef sets repository_bound truthfully based on concrete sourceRevision (R6)", () => {
		const boundRef = buildSourceRef(TEST_IDENTITY, "session-r6", "c".repeat(64));
		expect(boundRef.repository_bound).toBe(true);
		expect(boundRef.source_revision).toBe(TEST_IDENTITY.sourceRevision);
		expect(boundRef.native_validity_ref).toBeUndefined();
		expect(boundRef.sequence).toBeUndefined();

		const unboundIdentity: KnowledgeExecutionIdentity = {
			...TEST_IDENTITY,
			sourceRevision: null,
		};
		const unboundRef = buildSourceRef(unboundIdentity, "session-r6", "c".repeat(64));
		expect(unboundRef.repository_bound).toBe(false);
		expect(unboundRef.source_revision).toBeUndefined();
		expect(unboundRef.native_validity_ref).toBeUndefined();
		expect(unboundRef.sequence).toBeUndefined();
	});

	it("default resolver returns null and never calls repositories() when work item lacks repository_id (R5)", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-r5-null");
		const { pi, emit } = makeMockPi(sessionManager);

		let repositoriesCalled = false;
		const mockWorkClient = {
			workItem: async (_key: string) => ({
				work_id: "00000000-0000-0000-0000-000000000020",
				revision: { revision_id: "00000000-0000-0000-0000-000000000030" },
				repository_id: null,
			}),
			repositories: async () => {
				repositoriesCalled = true;
				throw new Error("repositories() must never be called when workItem lacks repository_id");
			},
		};

		const fakeWitness: OwnExecutionWitness = {
			sessionId: "session-r5-null",
			cwd: "/repo",
			workspace: {
				grantId: "grant-1",
				key: "OMP-1",
				path: "/repo",
				primaryRoot: "/repo",
				branch: "feat",
				baseline: "b".repeat(40),
			},
		};

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: mockWorkClient as any }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => fakeWitness,
		});

		await emit(
			"tool_result",
			{
				toolCallId: "call-r5-1",
				toolName: "bash",
				input: { command: "echo test" },
				content: [{ type: "text", text: "ok" }],
				details: null,
				isError: false,
			},
			{ taskDepth: 0, sessionManager },
		);

		expect(repositoriesCalled).toBe(false);
		expect(requests.length).toBe(0);
	});

	it("default resolver carries exact repository_id from workItem to /v1/ingest (R5)", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200, body: { status: "recorded" } }));
		const sessionManager = makeMockSessionManager("session-r5-exact");
		const { pi, emit } = makeMockPi(sessionManager);

		const mockWorkClient = {
			workItem: async (_key: string) => ({
				work_id: "00000000-0000-0000-0000-000000000020",
				revision: { revision_id: "00000000-0000-0000-0000-000000000030" },
				repository_id: "00000000-0000-0000-0000-000000000010",
			}),
			repositories: async () => {
				throw new Error("repositories() must not be called");
			},
		};

		const fakeWitness: OwnExecutionWitness = {
			sessionId: "session-r5-exact",
			cwd: "/repo",
			workspace: {
				grantId: "grant-1",
				key: "OMP-1",
				path: "/repo",
				primaryRoot: "/repo",
				branch: "feat",
				baseline: "b".repeat(40),
			},
		};

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: mockWorkClient as any }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => fakeWitness,
		});

		await emit(
			"tool_result",
			{
				toolCallId: "call-r5-2",
				toolName: "bash",
				input: { command: "echo test" },
				content: [{ type: "text", text: "ok" }],
				details: null,
				isError: false,
			},
			{ taskDepth: 0, sessionManager },
		);

		expect(requests.length).toBe(1);
		expect(requests[0].body.repository_id).toBe("00000000-0000-0000-0000-000000000010");
		expect(requests[0].body.source_ref.repository_id).toBe("00000000-0000-0000-0000-000000000010");
	});

	it("terminal agent_end captures execution_trace even when ctx.isIdle is false", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/ingest")) {
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-agent-end-term");
		const { pi, emit, entries } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const endEvent: AgentEndEvent = {
			type: "agent_end",
			messages: [],
			willContinue: false,
		};
		const ctx = {
			taskDepth: 0,
			sessionManager,
			isIdle: () => false,
		};

		await emit("agent_end", endEvent, ctx);

		expect(requests.length).toBe(1);
		const req = requests[0];
		expect(req.url).toBe("http://127.0.0.1:18090/v1/ingest");
		expect(req.method).toBe("POST");

		const body = req.body;
		expect(body.kind).toBe("native_record");
		expect(body.workspace_id).toBe(TEST_WORK_CONFIG.workspaceId);
		expect(body.repository_id).toBe(TEST_IDENTITY.repositoryId);
		expect(body.observation.kind).toBe("execution_trace");
		expect(body.source_ref.producer).toBe("session-system/work-now:agent_end");
		expect(body.source_ref.run_id).toBe("session-agent-end-term");
		expect(body.observation.payload).toEqual({
			turnId: "agent_end_session-agent-end-term",
			messageCount: 0,
			willContinue: false,
		});

		const captureEntry = entries.find(e => e.type === "work-now-capture");
		expect(captureEntry).toBeDefined();
		expect(captureEntry?.data.toolCallId).toBe("agent_end_session-agent-end-term");
	});

	it("agent_end with willContinue=true is skipped without capture or ingest POST", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/ingest")) {
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-agent-end-cont");
		const { pi, emit, entries } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const endEvent: AgentEndEvent = {
			type: "agent_end",
			messages: [],
			willContinue: true,
		};
		const ctx = {
			taskDepth: 0,
			sessionManager,
			isIdle: () => true,
		};

		await emit("agent_end", endEvent, ctx);

		expect(requests.length).toBe(0);
		expect(entries.find(e => e.type === "work-now-capture")).toBeUndefined();
	});

	it("terminal agent_end restart replay with existing work-now-capture -> zero POSTs", async () => {
		const toolCallId = "agent_end_session-agent-end-replay";
		const payload = {
			turnId: toolCallId,
			messageCount: 0,
			willContinue: false,
		};
		const contentSha256 = sha256Hex(canonicalJson(payload));

		const sessionManager = makeMockSessionManager("session-agent-end-replay", [
			{
				id: "e-agent-end",
				type: "custom",
				customType: "work-now-capture",
				data: {
					toolCallId,
					content_sha256: contentSha256,
				},
			},
		]);

		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const endEvent: AgentEndEvent = {
			type: "agent_end",
			messages: [],
			willContinue: false,
		};
		const ctx = {
			taskDepth: 0,
			sessionManager,
		};

		await emit("agent_end", endEvent, ctx);

		expect(requests.length).toBe(0);
	});

	it("agent_end at taskDepth>0 without witness -> no POST", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-agent-end-nested");
		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => undefined,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const endEvent: AgentEndEvent = {
			type: "agent_end",
			messages: [],
			willContinue: false,
		};
		const ctx = {
			taskDepth: 1,
			sessionManager,
		};

		await emit("agent_end", endEvent, ctx);

		expect(requests.length).toBe(0);
	});

	it("session restart after response loss / incomplete capture replays byte-identical outbound request payload including timestamp", async () => {
		let fetchCallCount = 0;
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/ingest")) {
				fetchCallCount++;
				if (fetchCallCount === 1) {
					// Simulate response drop or process crash before work-now-capture marker append
					throw new Error("connection reset by peer");
				}
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const initialSessionManager = makeMockSessionManager("session-replay-exact");
		const { pi: pi1, emit: emit1, entries: entries1 } = makeMockPi(initialSessionManager);

		registerKnowledgeBridge(pi1, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const toolEvent: ToolResultEvent = {
			type: "tool_result",
			toolCallId: "call-restart-1",
			toolName: "bash",
			input: { command: "npm test" },
			content: [{ type: "text", text: "tests passed" }],
			details: null,
			isError: false,
		};

		// First execution attempt: fails due to network drop after pending marker was appended
		await emit1("tool_result", toolEvent, { taskDepth: 0, sessionManager: initialSessionManager });

		// Verify pending marker was written before POST
		const pendingEntry = entries1.find(e => e.type === "work-now-pending-capture");
		expect(pendingEntry).toBeDefined();
		expect(pendingEntry?.data.toolCallId).toBe("call-restart-1");
		expect(pendingEntry?.data.request_payload).toBeDefined();

		// work-now-capture was NOT appended because response was lost
		expect(entries1.find(e => e.type === "work-now-capture")).toBeUndefined();
		expect(requests.length).toBe(1);
		const firstPost = requests[0];

		// Simulate restart with new session / clock advance:
		// Transcript preserved across restart contains the pending marker
		const restartedBranch = initialSessionManager.getBranch();
		const restartedSessionManager = makeMockSessionManager("session-replay-exact", restartedBranch);
		const { pi: pi2, emit: emit2, entries: entries2 } = makeMockPi(restartedSessionManager);

		registerKnowledgeBridge(pi2, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => {
				throw new Error("resolveExecutionIdentity must not be called when pending marker exists");
			},
		});

		// Trigger restart via session_start
		await emit2("session_start", {}, { taskDepth: 0, sessionManager: restartedSessionManager });

		expect(requests.length).toBe(2);
		const secondPost = requests[1];

		// Proves outbound request body bytes are byte-identical!
		expect(secondPost.body).toEqual(firstPost.body);
		expect(JSON.stringify(secondPost.body)).toBe(JSON.stringify(firstPost.body));
		expect(secondPost.body.operation_id).toBe(firstPost.body.operation_id);
		expect(secondPost.body.source_ref.observed_at).toBe(firstPost.body.source_ref.observed_at);
		expect(secondPost.body.source_ref.work_id).toBe(firstPost.body.source_ref.work_id);
		expect(secondPost.body.source_ref.revision_id).toBe(firstPost.body.source_ref.revision_id);
		expect(secondPost.body.observation.observation_id).toBe(firstPost.body.observation.observation_id);

		// Completed capture marker appended on second attempt
		const captureEntry = entries2.find(e => e.type === "work-now-capture");
		expect(captureEntry).toBeDefined();
		expect(captureEntry?.data.toolCallId).toBe("call-restart-1");

		// Replay stops: subsequent tool_result event causes zero additional POSTs
		await emit2("tool_result", toolEvent, { taskDepth: 0, sessionManager: restartedSessionManager });
		expect(requests.length).toBe(2);
	});

	it("terminal agent_end restart replays byte-identical outbound request payload including timestamp", async () => {
		let fetchCallCount = 0;
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/ingest")) {
				fetchCallCount++;
				if (fetchCallCount === 1) {
					throw new Error("timeout");
				}
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const initialSessionManager = makeMockSessionManager("session-agent-end-replay-exact");
		const { pi: pi1, emit: emit1, entries: entries1 } = makeMockPi(initialSessionManager);

		registerKnowledgeBridge(pi1, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const endEvent: AgentEndEvent = {
			type: "agent_end",
			messages: [],
			willContinue: false,
		};

		// First execution attempt
		await emit1("agent_end", endEvent, { taskDepth: 0, sessionManager: initialSessionManager });

		expect(entries1.find(e => e.type === "work-now-pending-capture")).toBeDefined();
		expect(entries1.find(e => e.type === "work-now-capture")).toBeUndefined();
		expect(requests.length).toBe(1);
		const firstPost = requests[0];

		// Simulate restart
		const restartedBranch = initialSessionManager.getBranch();
		const restartedSessionManager = makeMockSessionManager("session-agent-end-replay-exact", restartedBranch);
		const { pi: pi2, emit: emit2, entries: entries2 } = makeMockPi(restartedSessionManager);

		registerKnowledgeBridge(pi2, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => {
				throw new Error("resolveExecutionIdentity must not be called when pending marker exists");
			},
		});

		await emit2("agent_end", endEvent, { taskDepth: 0, sessionManager: restartedSessionManager });

		expect(requests.length).toBe(2);
		const secondPost = requests[1];

		expect(secondPost.body).toEqual(firstPost.body);
		expect(JSON.stringify(secondPost.body)).toBe(JSON.stringify(firstPost.body));
		expect(secondPost.body.operation_id).toBe(firstPost.body.operation_id);
		expect(secondPost.body.source_ref.observed_at).toBe(firstPost.body.source_ref.observed_at);
		expect(secondPost.body.source_ref.producer).toBe("session-system/work-now:agent_end");
		expect(entries2.find(e => e.type === "work-now-capture")).toBeDefined();
	});

	it("pending marker append failure emits observable notice, does not POST, and preserves fail-closed behavior", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200, body: { status: "recorded" } }));
		const sessionManager = makeMockSessionManager("session-append-fail");

		// Simulate appendEntry failure on pending marker append
		sessionManager.appendEntry = (type: string, data: unknown) => {
			if (type === "work-now-pending-capture") {
				throw new Error("disk quota exceeded");
			}
			const branch = sessionManager.getBranch() as Array<Record<string, unknown>>;
			branch.push({ id: `entry-${branch.length + 1}`, type: "custom", customType: type, data });
		};

		const { pi, emit } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const toolEvent: ToolResultEvent = {
			type: "tool_result",
			toolCallId: "call-fail-append",
			toolName: "bash",
			input: { command: "ls" },
			content: [{ type: "text", text: "file.txt" }],
			details: null,
			isError: false,
		};

		// Should not throw, fail-closed
		await emit("tool_result", toolEvent, { taskDepth: 0, sessionManager });

		// Zero POSTs sent because durable capture could not be established
		expect(requests.length).toBe(0);

		// Exactly one observable notice queued
		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("capture degraded");
		expect(notices[0]).toContain("marker");
	});

	it("server conflict 409 on replay is handled truthfully as recorded without outage notice and stops replay", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/ingest")) {
				// 409 Conflict: server already has this operation_id from earlier attempt
				return { status: 409, body: { error: { code: "already_recorded" } } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-409");
		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const toolEvent: ToolResultEvent = {
			type: "tool_result",
			toolCallId: "call-409",
			toolName: "bash",
			input: { command: "echo test" },
			content: [{ type: "text", text: "test" }],
			details: null,
			isError: false,
		};

		await emit("tool_result", toolEvent, { taskDepth: 0, sessionManager });

		expect(requests.length).toBe(1);
		// 409 is server conflict/replay: NOT an outage, so no notice queued
		expect(notices.length).toBe(0);

		// Completed capture marker appended
		const captureEntry = entries.find(e => e.type === "work-now-capture");
		expect(captureEntry).toBeDefined();
		expect(captureEntry?.data.toolCallId).toBe("call-409");

		// Replay stops on subsequent invocation
		await emit("tool_result", toolEvent, { taskDepth: 0, sessionManager });
		expect(requests.length).toBe(1);
	});

	it("503 outage on pending capture replay leaves pending marker intact and remains bounded to one notice", async () => {
		let callCount = 0;
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/ingest")) {
				callCount++;
				return { status: 503, body: { error: { code: "service_unavailable" } } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-503-bounded");
		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const toolEvent: ToolResultEvent = {
			type: "tool_result",
			toolCallId: "call-503",
			toolName: "bash",
			input: { command: "cat log" },
			content: [{ type: "text", text: "error log" }],
			details: null,
			isError: false,
		};

		// First attempt: appends pending marker and fails with 503
		await emit("tool_result", toolEvent, { taskDepth: 0, sessionManager });

		expect(requests.length).toBe(1);
		expect(entries.find(e => e.type === "work-now-pending-capture")).toBeDefined();
		expect(entries.find(e => e.type === "work-now-capture")).toBeUndefined();
		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("capture degraded (503)");

		// Second invocation: retries with exact pending marker, fails with 503 again
		await emit("tool_result", toolEvent, { taskDepth: 0, sessionManager });

		expect(requests.length).toBe(2);
		// Notice count remains bounded to 1
		expect(notices.length).toBe(1);
		// work-now-capture still not appended
		expect(entries.find(e => e.type === "work-now-capture")).toBeUndefined();
	});
});

describe("Knowledge Bridge — Fresh-Task Delivery (Requirement 2)", () => {
	it("compile 200 -> injects custom message with canonical content and details; POSTs /v1/uses per proposal", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: MOCK_BUNDLE };
			}
			if (req.url.endsWith("/v1/uses")) {
				return {
					status: 200,
					body: {
						use_id: `use-${req.body.proposal_id}`,
						proposal_id: req.body.proposal_id,
					},
				};
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-200");
		const { pi, emit, entries } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const result = (await emit("before_agent_start", {}, { taskDepth: 0, sessionManager })) as
			| { message?: { customType: string; content: string; details: any } }
			| undefined;

		expect(result).toBeDefined();
		expect(result?.message?.customType).toBe("work-knowledge-bundle");

		const expectedContent = MOCK_BUNDLE.content_canonical_json;
		expect(result?.message?.content).toBe(expectedContent);
		expect(sha256Hex(result?.message?.content!)).toBe(sha256Hex(expectedContent));
		expect(result?.message?.details.bundle_id).toBe(MOCK_BUNDLE.bundle_id);
		expect(result?.message?.details.bundle_sha256).toBe(MOCK_BUNDLE.bundle_sha256);
		expect(result?.message?.details.enrichment_status).toBe("applied");

		const compileRequests = requests.filter(r => r.url.endsWith("/v1/context/compile"));
		expect(compileRequests.length).toBe(1);
		expect(compileRequests[0].body.budget).toEqual({
			method: "utf8_bytes",
			limit: 32768,
			tokenizer_id: null,
		});

		// Verifies /v1/uses calls: one per proposal in proposal_lineage
		const useRequests = requests.filter(r => r.url.endsWith("/v1/uses"));
		expect(useRequests.length).toBe(2);
		expect(useRequests[0].body.proposal_id).toBe("00000000-0000-0000-0000-000000000051");
		expect(useRequests[1].body.proposal_id).toBe("00000000-0000-0000-0000-000000000052");

		// Persists 'work-now-bundle' entry
		const bundleEntry = entries.find(e => e.type === "work-now-bundle");
		expect(bundleEntry).toBeDefined();
		expect(bundleEntry?.data.bundle_id).toBe(MOCK_BUNDLE.bundle_id);
		expect(bundleEntry?.data.uses).toEqual([
			{
				proposal_id: "00000000-0000-0000-0000-000000000051",
				use_id: "use-00000000-0000-0000-0000-000000000051",
			},
			{
				proposal_id: "00000000-0000-0000-0000-000000000052",
				use_id: "use-00000000-0000-0000-0000-000000000052",
			},
		]);
		expect(bundleEntry?.data.use_ids).toEqual([
			"use-00000000-0000-0000-0000-000000000051",
			"use-00000000-0000-0000-0000-000000000052",
		]);
	});

	it("compile request carries bound snapshot identity instead of selecting latest implicitly", async () => {
		const snapshotId = "sha256:" + "9".repeat(64);
		const bundle = buildMockBundle({ snapshotId });
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) return { status: 200, body: bundle };
			if (req.url.endsWith("/v1/uses")) return { status: 200, body: { use_id: `use-${req.body.proposal_id}` } };
			return { status: 404 };
		});
		const sessionManager = makeMockSessionManager("session-snapshot-bound");
		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => ({ ...TEST_IDENTITY, snapshotId }),
		});

		await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });
		const compile = requests.find(req => req.url.endsWith("/v1/context/compile"));
		expect(compile?.body.snapshot_id).toBe(snapshotId);
	});

	it("compile 503 / 422 fail-closed -> injects nothing, no /v1/uses, exactly one notice", async () => {
		const { fetchImpl } = makeFakeFetch(() => ({ status: 503, body: { error: { code: "native_unavailable" } } }));

		const sessionManager = makeMockSessionManager("session-201");
		const { pi, emit } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const result = await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });

		expect(result).toBeUndefined();
		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("context compile unavailable");
	});

	it("second prompt in same transcript with 'work-now-bundle' entry for same bundle_id -> zero /v1/uses POSTs (replay safe)", async () => {
		const sessionManager = makeMockSessionManager("session-202", [
			{
				id: "bundle-entry-1",
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: MOCK_BUNDLE.bundle_id,
					bundle_sha256: MOCK_BUNDLE.bundle_sha256,
					uses: [
						{ proposal_id: "00000000-0000-0000-0000-000000000051", use_id: "use-1" },
						{ proposal_id: "00000000-0000-0000-0000-000000000052", use_id: "use-2" },
					],
					use_ids: ["use-1", "use-2"],
				},
			},
		]);

		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: MOCK_BUNDLE };
			}
			return { status: 200, body: {} };
		});

		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });

		const usePosts = requests.filter(r => r.url.endsWith("/v1/uses"));
		expect(usePosts.length).toBe(0);
	});

	it("tool_call toolName='task' with witness -> returns input.context prepended with bundle bytes", async () => {
		const { fetchImpl } = makeFakeFetch(() => ({ status: 200, body: buildMockBundle({ stage: "subagent" }) }));
		const sessionManager = makeMockSessionManager("session-203");
		const { pi } = makeMockPi(sessionManager);

		const fakeWitness: OwnExecutionWitness = {
			sessionId: "session-203",
			cwd: "/repo",
			workspace: {
				grantId: "grant-1",
				key: "OMP-1",
				path: "/repo",
				primaryRoot: "/repo",
				branch: "feat",
				baseline: "c".repeat(40),
			},
		};

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => fakeWitness,
			resolveExecutionIdentity: async () => ({ ...TEST_IDENTITY, stage: "subagent" }),
		});

		const result = await bridge.handleToolCall(
			{
				type: "tool_call",
				toolName: "task",
				toolCallId: "call-task-spawn",
				input: { prompt: "do subtask", context: "existing instructions" },
			} as any,
			{ taskDepth: 0, sessionManager } as any,
		);

		expect(result).toBeDefined();
		const expectedBundleBytes = MOCK_BUNDLE.content_canonical_json;
		expect(result?.input?.context).toBeDefined();
		expect((result?.input?.context as string).startsWith(expectedBundleBytes)).toBe(true);
		expect((result?.input?.context as string).endsWith("existing instructions")).toBe(true);
	});

	describe("injection uses retained Python content bytes, never a TS re-serialization", () => {
		// Python canonical_json renders 1.0 as "1.0"; TS canonicalJson would render 1.
		// Build the identity bytes the way Python would, without any TS reconstruction of content.
		function buildPythonStyleBundle(stage: string) {
			const mandatory = { ratio: 1, glyph: "\u{1D518}" };
			const optional = { scale: 1 };
			const pythonContentBytes = '{"mandatory":{"glyph":"\u{1D518}","ratio":1.0},"optional":{"scale":10.0}}';
			const tsContentBytes = canonicalJson({ mandatory, optional: { scale: 10 } });
			expect(pythonContentBytes).not.toBe(tsContentBytes);
			const identityPrefix = mockIdentityCanonicalJson({
				workspace_id: TEST_WORK_CONFIG.workspaceId,
				repository_id: TEST_IDENTITY.repositoryId,
				work_id: TEST_IDENTITY.workId,
				revision_id: TEST_IDENTITY.revisionId,
				candidate_id: TEST_IDENTITY.candidateId ?? null,
				stage,
				snapshot_id: null,
				budget_spec: MOCK_BUDGET_SPEC,
				content: { mandatory: {}, optional: {} },
			});
			const identity_canonical_json = identityPrefix.replace(
				',"content":{"mandatory":{},"optional":{}},"identity_encoding":',
				`,"content":${pythonContentBytes},"identity_encoding":`,
			);
			expect(identity_canonical_json).not.toBe(identityPrefix);
			return {
				...buildMockBundle({ stage }),
				bundle_sha256: sha256Hex(identity_canonical_json),
				identity_canonical_json,
				content_canonical_json: pythonContentBytes,
				mandatory,
				optional: { scale: 10 },
				budget: mockBudgetActual(MOCK_BUDGET_SPEC, pythonContentBytes, mandatory),
				pythonContentBytes,
				tsContentBytes,
			};
		}

		it("owner before_agent_start injects the exact Python bytes", async () => {
			const bundle = buildPythonStyleBundle("execute");
			const { fetchImpl } = makeFakeFetch(req => {
				if (req.url.endsWith("/v1/context/compile")) return { status: 200, body: bundle };
				if (req.url.endsWith("/v1/uses")) return { status: 200, body: { use_id: "u", proposal_id: req.body.proposal_id } };
				return { status: 404 };
			});
			const sessionManager = makeMockSessionManager("session-210");
			const { pi, emit } = makeMockPi(sessionManager);
			registerKnowledgeBridge(pi, {
				backend: makeMockBackend(),
				workConfig: TEST_WORK_CONFIG,
				knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
				fetchImpl,
				resolveExecutionIdentity: async () => TEST_IDENTITY,
			});
			const result = (await emit("before_agent_start", {}, { taskDepth: 0, sessionManager })) as
				| { message?: { content: string } }
				| undefined;
			expect(result?.message?.content).toBe(bundle.pythonContentBytes);
			expect(result?.message?.content).not.toBe(bundle.tsContentBytes);
		});

		it("worker task tool_call prepends the exact Python bytes", async () => {
			const bundle = buildPythonStyleBundle("subagent");
			const { fetchImpl } = makeFakeFetch(() => ({ status: 200, body: bundle }));
			const sessionManager = makeMockSessionManager("session-211");
			const { pi } = makeMockPi(sessionManager);
			const bridge = registerKnowledgeBridge(pi, {
				backend: makeMockBackend(),
				workConfig: TEST_WORK_CONFIG,
				knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
				fetchImpl,
				getExecutionWitness: () => ({
					sessionId: "session-211",
					cwd: "/repo",
					workspace: { grantId: "g", key: "OMP-1", path: "/repo", primaryRoot: "/repo", branch: "feat", baseline: "c".repeat(40) },
				}),
				resolveExecutionIdentity: async () => ({ ...TEST_IDENTITY, stage: "subagent" }),
			});
			const result = await bridge.handleToolCall(
				{ type: "tool_call", toolName: "task", toolCallId: "call-task-py", input: { prompt: "x", context: "tail" } } as any,
				{ taskDepth: 0, sessionManager } as any,
			);
			expect((result?.input?.context as string).startsWith(bundle.pythonContentBytes)).toBe(true);
			expect((result?.input?.context as string).startsWith(bundle.tsContentBytes)).toBe(false);
		});

		it("owner path fails closed when the compiler omits content_canonical_json (legacy row)", async () => {
			const { content_canonical_json: _dropped, ...legacy } = buildMockBundle();
			const { fetchImpl } = makeFakeFetch(() => ({ status: 200, body: legacy }));
			const sessionManager = makeMockSessionManager("session-212");
			const { pi, emit } = makeMockPi(sessionManager);
			registerKnowledgeBridge(pi, {
				backend: makeMockBackend(),
				workConfig: TEST_WORK_CONFIG,
				knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
				fetchImpl,
				resolveExecutionIdentity: async () => TEST_IDENTITY,
			});
			const result = await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });
			expect(result).toBeUndefined();
		});
	});

	it("tool_call toolName='task' without witness -> returns undefined", async () => {
		const { fetchImpl } = makeFakeFetch(() => ({ status: 200, body: MOCK_BUNDLE }));
		const sessionManager = makeMockSessionManager("session-204");
		const { pi } = makeMockPi(sessionManager);

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => undefined, // NO witness
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const result = await bridge.handleToolCall(
			{
				type: "tool_call",
				toolName: "task",
				toolCallId: "call-task-no-witness",
				input: { prompt: "spawn without witness" },
			} as any,
			{ taskDepth: 1, sessionManager } as any,
		);

		expect(result).toBeUndefined();
	});

	it("compile 200 with lineage [51,52]; /v1/uses 200 for 51 and 503 for 52 -> exactly one marker with uses=[51], one notice, bundle message returned (R2 partial)", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: MOCK_BUNDLE };
			}
			if (req.url.endsWith("/v1/uses")) {
				if (req.body.proposal_id === "00000000-0000-0000-0000-000000000051") {
					return {
						status: 200,
						body: { use_id: "use-51", proposal_id: req.body.proposal_id },
					};
				}
				return {
					status: 503,
					body: { error: { code: "native_unavailable" } },
				};
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-partial-1");
		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const result = (await emit("before_agent_start", {}, { taskDepth: 0, sessionManager })) as
			| { message?: { customType: string; content: string; details: unknown } }
			| undefined;

		expect(result).toBeDefined();
		expect(result?.message?.customType).toBe("work-knowledge-bundle");

		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("use recording degraded (503)");

		const bundleEntries = entries.filter(e => e.type === "work-now-bundle");
		expect(bundleEntries.length).toBe(1);
		expect(bundleEntries[0].data.uses).toEqual([
			{ proposal_id: "00000000-0000-0000-0000-000000000051", use_id: "use-51" },
		]);
		expect(bundleEntries[0].data.use_ids).toEqual(["use-51"]);
	});

	it("retry path: next bound start with persisted bundle identity retries only proposal 52, zero compiles, second marker, settles both (R2 retry)", async () => {
		let compileCalls = 0;
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				compileCalls++;
				return { status: 200, body: MOCK_BUNDLE };
			}
			if (req.url.endsWith("/v1/uses")) {
				return {
					status: 200,
					body: { use_id: `use-${(req.body.proposal_id as string).slice(-2)}`, proposal_id: req.body.proposal_id },
				};
			}
			if (req.url.includes("/outcome")) {
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		// Transcript pre-populated with persisted bundle message and initial partial marker (proposal 51 only)
		const sessionManager = makeMockSessionManager("session-retry-r2", [
			{
				id: "msg-persisted-bundle",
				type: "custom_message",
				customType: "work-knowledge-bundle",
				content: "{}",
				details: {
					bundle_id: MOCK_BUNDLE.bundle_id,
					bundle_sha256: MOCK_BUNDLE.bundle_sha256,
					proposal_lineage: MOCK_BUNDLE.proposal_lineage,
					task_work_id: TEST_IDENTITY.workId,
					task_revision_id: TEST_IDENTITY.revisionId,
					task_candidate_id: TEST_IDENTITY.candidateId,
					stage: TEST_IDENTITY.stage,
				},
			},
			{
				id: "bundle-marker-1",
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: MOCK_BUNDLE.bundle_id,
					bundle_sha256: MOCK_BUNDLE.bundle_sha256,
					uses: [{ proposal_id: "00000000-0000-0000-0000-000000000051", use_id: "use-51" }],
					use_ids: ["use-51"],
					task_work_id: TEST_IDENTITY.workId,
					task_revision_id: TEST_IDENTITY.revisionId,
					task_candidate_id: TEST_IDENTITY.candidateId,
				},
			},
		]);

		const mockWorkClient = makeMockWorkClient(
			makeMockWorkflowView({
				launchId: "launch-settle-r2",
				attemptId: "attempt-1",
			}),
		);

		const { pi, emit, entries } = makeMockPi(sessionManager);
		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: mockWorkClient }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		// 1. Next bound start -> undefined result, zero compiles, exactly one /v1/uses POST (proposal 52)
		const startResult = await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });
		expect(startResult).toBeUndefined();

		const compilePosts = requests.filter(r => r.url.endsWith("/v1/context/compile"));
		expect(compilePosts.length).toBe(0);
		expect(compileCalls).toBe(0);

		const usePosts = requests.filter(r => r.url.endsWith("/v1/uses"));
		expect(usePosts.length).toBe(1);
		expect(usePosts[0].body.proposal_id).toBe("00000000-0000-0000-0000-000000000052");

		// Second work-now-bundle marker containing only proposal 52
		const bundleMarkers = entries.filter(e => e.type === "work-now-bundle");
		expect(bundleMarkers.length).toBe(1);
		expect(bundleMarkers[0].data.uses).toEqual([
			{ proposal_id: "00000000-0000-0000-0000-000000000052", use_id: "use-52" },
		]);
		expect(bundleMarkers[0].data.use_ids).toEqual(["use-52"]);

		// 2. Settle -> /outcome POSTs for both use-51 and use-52
		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			attemptId: "attempt-1",
			event: defaultEvent,
		};
		await bridge.onAuditorSettle("OMP-1", "launch-settle-r2", settleOutcome, { sessionManager } as unknown as ExtensionContext);

		const outcomePosts = requests.filter(r => r.url.includes("/outcome"));
		expect(outcomePosts.length).toBe(2);
		expect(outcomePosts[0].url).toContain("use-51");
		expect(outcomePosts[1].url).toContain("use-52");
	});

	it("all /v1/uses fail -> zero work-now-bundle markers, one notice, bundle message returned (R2 all-fail)", async () => {
		const { fetchImpl } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: MOCK_BUNDLE };
			}
			if (req.url.endsWith("/v1/uses")) {
				return { status: 503, body: { error: { code: "native_unavailable" } } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-all-fail");
		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const result = (await emit("before_agent_start", {}, { taskDepth: 0, sessionManager })) as
			| { message?: { customType: string; content: string; details: unknown } }
			| undefined;

		expect(result).toBeDefined();
		expect(result?.message?.customType).toBe("work-knowledge-bundle");

		const bundleMarkers = entries.filter(e => e.type === "work-now-bundle");
		expect(bundleMarkers.length).toBe(0);

		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("use recording degraded (503)");
	});

	it("/v1/uses throws network exception -> zero markers, one native_unavailable notice (R2 throw)", async () => {
		const { fetchImpl } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: MOCK_BUNDLE };
			}
			if (req.url.endsWith("/v1/uses")) {
				throw new Error("network connection refused");
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-throw-test");
		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });

		const bundleMarkers = entries.filter(e => e.type === "work-now-bundle");
		expect(bundleMarkers.length).toBe(0);

		expect(notices.length).toBe(1);
		expect(notices[0]).toBe("[knowledge] use recording degraded (native_unavailable)");
	});

	it("use outage notices are capped at one per session and reset on session_start / session_switch (R2 notices)", async () => {
		const { fetchImpl } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: MOCK_BUNDLE };
			}
			if (req.url.endsWith("/v1/uses")) {
				return { status: 500, body: { error: "internal error" } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-outage-cap");
		const { pi, emit } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		// Both proposals in lineage fail, but only 1 notice queued
		await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });
		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("use recording degraded (500)");

		// Trigger session_start -> resets flag
		await emit("session_start", {}, { sessionManager });

		// Next failure can queue another notice
		await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });
		expect(notices.length).toBe(2);

		// Trigger session_switch -> resets flag again
		await emit("session_switch", {}, { sessionManager });
		await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });
		expect(notices.length).toBe(3);
	});

	it("compile response identity mismatch -> returns undefined, no bundle message persisted or returned, no /v1/uses, queues malformed_response notice", async () => {
		const mismatchedBundle = buildMockBundle({
			workspaceId: "00000000-0000-0000-0000-000000000099",
		});
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: mismatchedBundle };
			}
			if (req.url.endsWith("/v1/uses")) {
				return { status: 200, body: { use_id: `use-${String(req.body?.proposal_id)}` } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-identity-mismatch");
		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const result = await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });

		expect(result).toBeUndefined();

		const compileRequests = requests.filter(r => r.url.endsWith("/v1/context/compile"));
		expect(compileRequests.length).toBe(1);

		const useRequests = requests.filter(r => r.url.endsWith("/v1/uses"));
		expect(useRequests.length).toBe(0);

		const persistedBundleMessages = (
			sessionManager.getBranch() as ReadonlyArray<{ customType?: string }>
		).filter(e => e.customType === "work-knowledge-bundle");
		expect(persistedBundleMessages.length).toBe(0);

		const persistedEntries = entries.filter(e => e.type === "work-knowledge-bundle");
		expect(persistedEntries.length).toBe(0);

		expect(notices.length).toBe(1);
		expect(notices[0]).toBe("[knowledge] context compile unavailable (malformed_response)");
	});

	it("compile response budget mismatch -> returns undefined, no bundle message persisted or returned, queues malformed_response notice", async () => {
		const budgetMismatchedBundle = buildMockBundle({
			budget: { method: "utf8_bytes", limit: 16384 },
		});
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: budgetMismatchedBundle };
			}
			if (req.url.endsWith("/v1/uses")) {
				return { status: 200, body: { use_id: `use-${String(req.body?.proposal_id)}` } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-budget-mismatch");
		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
			notices,
		});

		const result = await emit("before_agent_start", {}, { taskDepth: 0, sessionManager });

		expect(result).toBeUndefined();
		expect(requests.filter(r => r.url.endsWith("/v1/uses")).length).toBe(0);
		expect(entries.filter(e => e.type === "work-now-bundle").length).toBe(0);
		expect(notices.length).toBe(1);
		expect(notices[0]).toBe("[knowledge] context compile unavailable (malformed_response)");
	});
});

describe("Knowledge Bridge — Inject Once Per Bound Identity (R3)", () => {
	it("first before_agent_start at taskDepth 0 -> one compile POST, message returned with details.task_work_id/revision/candidate/stage; after persisting it, second before_agent_start -> zero compile POSTs, undefined result", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: MOCK_BUNDLE };
			}
			if (req.url.endsWith("/v1/uses")) {
				return { status: 200, body: { use_id: `use-${req.body.proposal_id}` } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-r3-dedupe");
		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		// First start: compiles and returns message with identity details
		const firstResult = (await emit("before_agent_start", {}, { taskDepth: 0, sessionManager } as unknown as ExtensionContext)) as
			| { message?: { customType: string; content: string; details: Record<string, unknown> } }
			| undefined;

		expect(firstResult).toBeDefined();
		expect(firstResult?.message?.customType).toBe("work-knowledge-bundle");
		expect(firstResult?.message?.details.task_work_id).toBe(TEST_IDENTITY.workId);
		expect(firstResult?.message?.details.task_revision_id).toBe(TEST_IDENTITY.revisionId);
		expect(firstResult?.message?.details.task_candidate_id).toBe(TEST_IDENTITY.candidateId);
		expect(firstResult?.message?.details.stage).toBe(TEST_IDENTITY.stage);

		const compileCount1 = requests.filter(r => r.url.endsWith("/v1/context/compile")).length;
		expect(compileCount1).toBe(1);

		// Persist the returned message into the session branch (simulating agent session persistence)
		persistCustomMessage(sessionManager, firstResult!.message!);

		// Second start: detects persisted injection entry -> returns undefined, zero new compile POSTs
		const secondResult = await emit("before_agent_start", {}, { taskDepth: 0, sessionManager } as unknown as ExtensionContext);
		expect(secondResult).toBeUndefined();

		const compileCount2 = requests.filter(r => r.url.endsWith("/v1/context/compile")).length;
		expect(compileCount2).toBe(1);
	});

	it("identity changes (new revisionId) -> new compile POST and new message", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: buildMockBundle({ revisionId: req.body.revision_id }) };
			}
			if (req.url.endsWith("/v1/uses")) {
				return { status: 200, body: { use_id: `use-${req.body.proposal_id}` } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-r3-change");
		const { pi, emit } = makeMockPi(sessionManager);

		let currentIdentity: KnowledgeExecutionIdentity = TEST_IDENTITY;

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => currentIdentity,
		});

		// 1. First start with revisionId = TEST_IDENTITY.revisionId
		const res1 = (await emit("before_agent_start", {}, { taskDepth: 0, sessionManager } as unknown as ExtensionContext)) as
			| { message?: { customType: string; content: string; details: Record<string, unknown> } }
			| undefined;
		expect(res1).toBeDefined();
		persistCustomMessage(sessionManager, res1!.message!);
		expect(requests.filter(r => r.url.endsWith("/v1/context/compile")).length).toBe(1);

		// 2. Identity changes to new revisionId
		const newRevId = "00000000-0000-0000-0000-000000000099";
		currentIdentity = {
			...TEST_IDENTITY,
			revisionId: newRevId,
		};

		const res2 = (await emit("before_agent_start", {}, { taskDepth: 0, sessionManager } as unknown as ExtensionContext)) as
			| { message?: { customType: string; content: string; details: Record<string, unknown> } }
			| undefined;
		expect(res2).toBeDefined();
		expect(res2?.message?.details.task_revision_id).toBe(newRevId);
		expect(requests.filter(r => r.url.endsWith("/v1/context/compile")).length).toBe(2);
	});

	it("before_agent_start at taskDepth 1 with witness present -> undefined, zero POSTs", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200, body: MOCK_BUNDLE }));
		const sessionManager = makeMockSessionManager("session-r3-depth1");
		const { pi, emit } = makeMockPi(sessionManager);

		const fakeWitness: OwnExecutionWitness = {
			sessionId: "session-r3-depth1",
			cwd: "/repo",
			workspace: {
				grantId: "grant-1",
				key: "OMP-1",
				path: "/repo",
				primaryRoot: "/repo",
				branch: "feat",
				baseline: "b".repeat(40),
			},
		};

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => fakeWitness,
			resolveExecutionIdentity: async () => ({ ...TEST_IDENTITY, stage: "subagent" }),
		});

		// before_agent_start at taskDepth 1 must return undefined with zero compile/uses POSTs
		const result = await emit("before_agent_start", {}, { taskDepth: 1, sessionManager } as unknown as ExtensionContext);
		expect(result).toBeUndefined();
		expect(requests.length).toBe(0);
	});

	it("restart replay: branch preloaded with persisted work-knowledge-bundle custom_message for identity and complete markers -> zero compile and zero /v1/uses POSTs", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200, body: {} }));

		const sessionManager = makeMockSessionManager("session-r3-replay", [
			{
				id: "msg-persisted",
				type: "custom_message",
				customType: "work-knowledge-bundle",
				content: "{}",
				details: {
					bundle_id: MOCK_BUNDLE.bundle_id,
					bundle_sha256: MOCK_BUNDLE.bundle_sha256,
					proposal_lineage: MOCK_BUNDLE.proposal_lineage,
					task_work_id: TEST_IDENTITY.workId,
					task_revision_id: TEST_IDENTITY.revisionId,
					task_candidate_id: TEST_IDENTITY.candidateId,
					stage: TEST_IDENTITY.stage,
				},
			},
			{
				id: "marker-persisted",
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: MOCK_BUNDLE.bundle_id,
					bundle_sha256: MOCK_BUNDLE.bundle_sha256,
					uses: [
						{ proposal_id: "00000000-0000-0000-0000-000000000051", use_id: "use-51" },
						{ proposal_id: "00000000-0000-0000-0000-000000000052", use_id: "use-52" },
					],
					use_ids: ["use-51", "use-52"],
					task_work_id: TEST_IDENTITY.workId,
					task_revision_id: TEST_IDENTITY.revisionId,
					task_candidate_id: TEST_IDENTITY.candidateId,
				},
			},
		]);

		const { pi, emit } = makeMockPi(sessionManager);

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			resolveExecutionIdentity: async () => TEST_IDENTITY,
		});

		const result = await emit("before_agent_start", {}, { taskDepth: 0, sessionManager } as unknown as ExtensionContext);
		expect(result).toBeUndefined();

		expect(requests.filter(r => r.url.endsWith("/v1/context/compile")).length).toBe(0);
		expect(requests.filter(r => r.url.endsWith("/v1/uses")).length).toBe(0);
	});

	it("findInjectedBundle correctly matches identity across custom, custom_message, and message entry shapes and rejects mismatches", () => {
		const matchingDetails = {
			bundle_id: "bundle-r3-test",
			task_work_id: TEST_IDENTITY.workId,
			task_revision_id: TEST_IDENTITY.revisionId,
			task_candidate_id: TEST_IDENTITY.candidateId,
			stage: TEST_IDENTITY.stage,
		};

		// 1. Custom entry shape
		const branch1 = [
			{
				type: "custom",
				customType: "work-knowledge-bundle",
				data: matchingDetails,
			},
		];
		expect(findInjectedBundle(branch1, TEST_IDENTITY)?.bundle_id).toBe("bundle-r3-test");

		// 2. Custom_message entry shape
		const branch2 = [
			{
				type: "custom_message",
				customType: "work-knowledge-bundle",
				details: matchingDetails,
			},
		];
		expect(findInjectedBundle(branch2, TEST_IDENTITY)?.bundle_id).toBe("bundle-r3-test");

		// 3. Message entry shape with customType (deprecated message shape rejection)
		const branch3 = [
			{
				type: "message",
				message: {
					customType: "work-knowledge-bundle",
					details: matchingDetails,
				},
			},
		];
		// Deprecated message shape rejection
		expect(findInjectedBundle(branch3, TEST_IDENTITY)).toBeUndefined();

		// 4. Mismatch on workId
		expect(findInjectedBundle(branch2, { ...TEST_IDENTITY, workId: "other-work" })).toBeUndefined();

		// 5. Mismatch on revisionId
		expect(findInjectedBundle(branch2, { ...TEST_IDENTITY, revisionId: "other-rev" })).toBeUndefined();

		// 6. Mismatch on candidateId
		expect(findInjectedBundle(branch2, { ...TEST_IDENTITY, candidateId: "other-cand" })).toBeUndefined();

		// 7. Mismatch on stage
		expect(findInjectedBundle(branch2, { ...TEST_IDENTITY, stage: "plan" })).toBeUndefined();
	});
});

describe("Knowledge Bridge — recordedUsesForBundle & isBundleReplayed helpers (R2)", () => {
	it("recordedUsesForBundle aggregates uses across multiple markers for same bundle_id", () => {
		const branch = [
			{
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: "bundle-alpha",
					uses: [{ proposal_id: "p1", use_id: "u1" }],
					use_ids: ["u1"],
				},
			},
			{
				type: "custom_message",
				customType: "work-now-bundle",
				details: {
					bundle_id: "bundle-alpha",
					uses: [{ proposal_id: "p2", use_id: "u2" }],
					use_ids: ["u2"],
				},
			},
			{
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: "bundle-beta",
					uses: [{ proposal_id: "p3", use_id: "u3" }],
					use_ids: ["u3"],
				},
			},
		];

		const mapAlpha = recordedUsesForBundle(branch, "bundle-alpha");
		expect(mapAlpha.size).toBe(2);
		expect(mapAlpha.get("p1")).toBe("u1");
		expect(mapAlpha.get("p2")).toBe("u2");

		const mapBeta = recordedUsesForBundle(branch, "bundle-beta");
		expect(mapBeta.size).toBe(1);
		expect(mapBeta.get("p3")).toBe("u3");

		const mapNone = recordedUsesForBundle(branch, "bundle-none");
		expect(mapNone.size).toBe(0);
	});

	it("isBundleReplayed returns true iff every proposalId is present (or map.size > 0 when omitted)", () => {
		const branch = [
			{
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: "bundle-alpha",
					uses: [
						{ proposal_id: "p1", use_id: "u1" },
						{ proposal_id: "p2", use_id: "u2" },
					],
					use_ids: ["u1", "u2"],
				},
			},
		];

		// All proposals present
		expect(isBundleReplayed(branch, "bundle-alpha", ["p1", "p2"])).toBe(true);
		// Partial proposals present
		expect(isBundleReplayed(branch, "bundle-alpha", ["p1", "p2", "p3"])).toBe(false);
		// Empty proposals list
		expect(isBundleReplayed(branch, "bundle-alpha", [])).toBe(true);
		// Omitted proposals -> true because map.size > 0
		expect(isBundleReplayed(branch, "bundle-alpha")).toBe(true);
		// Omitted proposals on empty bundle -> false
		expect(isBundleReplayed(branch, "bundle-absent")).toBe(false);
	});
});

describe("Knowledge Bridge — Outcome Recording (Requirement 3)", () => {
	const BUNDLE_ID = "00000000-0000-0000-0000-000000000099";
	const USE_ID_1 = "00000000-0000-0000-0000-000000000071";
	const USE_ID_2 = "00000000-0000-0000-0000-000000000072";

	function makeBranchWithBundleAndMessage() {
		return [
			{
				id: "bundle-entry",
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: BUNDLE_ID,
					uses: [
						{ proposal_id: "00000000-0000-0000-0000-000000000051", use_id: USE_ID_1 },
						{ proposal_id: "00000000-0000-0000-0000-000000000052", use_id: USE_ID_2 },
					],
					use_ids: [USE_ID_1, USE_ID_2],
					task_work_id: TEST_IDENTITY.workId,
					task_revision_id: TEST_IDENTITY.revisionId,
					task_candidate_id: TEST_IDENTITY.candidateId,
				},
			},
			{
				id: "bundle-msg",
				type: "custom_message",
				customType: "work-knowledge-bundle",
				details: {
					bundle_id: BUNDLE_ID,
				},
			},
		];
	}

	it("stale audit receipt first in view -> outcome POSTs carry exactly launch-bound receipt_id; second replay 409 tolerated", async () => {
		let outcomeCalls = 0;
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.includes("/outcome")) {
				outcomeCalls++;
				if (outcomeCalls <= 2) {
					return { status: 200, body: { status: "recorded" } };
				}
				// Second settle replay returns 409
				return { status: 409, body: { error: { code: "outcome_already_recorded" } } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-300", makeBranchWithBundleAndMessage());
		const { pi } = makeMockPi(sessionManager);

		const launchId = "launch-1";
		const attemptId = "attempt-1";
		const staleAuditReceipt: EvidenceReceipt = {
			receipt_id: "00000000-0000-0000-0000-000000000077",
			work_id: TEST_IDENTITY.workId,
			revision_id: TEST_IDENTITY.revisionId,
			candidate_id: TEST_IDENTITY.candidateId!,
			kind: "audit",
			issuer: "work-service/auditor-settle",
			issued_at: new Date().toISOString(),
			payload: { launch_id: "stale-launch" },
			payload_sha256: "0".repeat(64),
			verdict: "PASS",
		};
		const boundAuditReceipt: EvidenceReceipt = {
			receipt_id: AUDIT_RECEIPT_ID,
			work_id: TEST_IDENTITY.workId,
			revision_id: TEST_IDENTITY.revisionId,
			candidate_id: TEST_IDENTITY.candidateId!,
			kind: "audit",
			issuer: "work-service/auditor-settle",
			issued_at: new Date().toISOString(),
			payload: { launch_id: launchId },
			payload_sha256: "0".repeat(64),
			verdict: "PASS",
		};

		const mockView = makeMockWorkflowView({
			launchId,
			attemptId,
			receipts: [staleAuditReceipt, boundAuditReceipt],
		});

		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			attemptId,
			event: defaultEvent,
		};

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: makeMockWorkClient(mockView) }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});

		// First settle: records outcome for both use IDs with the launch-bound receipt
		await bridge.onAuditorSettle("OMP-1", launchId, settleOutcome, { sessionManager } as any);

		const outcomePosts = requests.filter(r => r.url.includes("/outcome"));
		expect(outcomePosts.length).toBe(2);
		expect(outcomePosts[0].url).toContain(USE_ID_1);
		expect(outcomePosts[0].body.outcome_receipt_id).toBe(AUDIT_RECEIPT_ID);
		expect(outcomePosts[0].body.worker_used).toBe(true);

		expect(outcomePosts[1].url).toContain(USE_ID_2);
		expect(outcomePosts[1].body.outcome_receipt_id).toBe(AUDIT_RECEIPT_ID);
		expect(outcomePosts[1].body.worker_used).toBe(true);

		// Second settle replay: returns 409, tolerated without throwing
		await expect(
			bridge.onAuditorSettle("OMP-1", launchId, settleOutcome, { sessionManager } as any),
		).resolves.toBeUndefined();
	});

	it("view has the launch but no audit receipt whose payload.launch_id === launchId -> zero /outcome POSTs, zero pending entries", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-missing-receipt", makeBranchWithBundleAndMessage());
		const { pi, entries } = makeMockPi(sessionManager);

		const mockView = makeMockWorkflowView({
			launchId: "launch-1",
			receipts: [],
		});

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: makeMockWorkClient(mockView) }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});

		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			event: defaultEvent,
		};

		await bridge.onAuditorSettle("OMP-1", "launch-1", settleOutcome, { sessionManager } as any);

		expect(requests.length).toBe(0);
		const pending = entries.filter(e => e.type === "work-now-pending-outcome");
		expect(pending.length).toBe(0);
	});

	it("two audit receipts claim the same launch_id -> zero /outcome POSTs (fail closed on ambiguity)", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-ambiguous-receipt", makeBranchWithBundleAndMessage());
		const { pi, entries } = makeMockPi(sessionManager);

		const launchId = "launch-1";
		const r1: EvidenceReceipt = {
			receipt_id: "00000000-0000-0000-0000-000000000081",
			work_id: TEST_IDENTITY.workId,
			revision_id: TEST_IDENTITY.revisionId,
			candidate_id: TEST_IDENTITY.candidateId!,
			kind: "audit",
			issuer: "work-service/auditor-settle",
			issued_at: new Date().toISOString(),
			payload: { launch_id: launchId },
			payload_sha256: "0".repeat(64),
			verdict: "PASS",
		};
		const r2: EvidenceReceipt = {
			receipt_id: "00000000-0000-0000-0000-000000000082",
			work_id: TEST_IDENTITY.workId,
			revision_id: TEST_IDENTITY.revisionId,
			candidate_id: TEST_IDENTITY.candidateId!,
			kind: "audit",
			issuer: "work-service/auditor-settle",
			issued_at: new Date().toISOString(),
			payload: { launch_id: launchId },
			payload_sha256: "0".repeat(64),
			verdict: "PASS",
		};

		const mockView = makeMockWorkflowView({
			launchId,
			receipts: [r1, r2],
		});

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: makeMockWorkClient(mockView) }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});

		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			event: defaultEvent,
		};

		await bridge.onAuditorSettle("OMP-1", launchId, settleOutcome, { sessionManager } as any);

		expect(requests.length).toBe(0);
		const pending = entries.filter(e => e.type === "work-now-pending-outcome");
		expect(pending.length).toBe(0);
	});

	it("launch.attempt_id !== settleOutcome.attemptId, or receipt.verdict !== settleOutcome.verdict, or settleOutcome.launchId !== launchId param -> zero /outcome POSTs", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-mismatch", makeBranchWithBundleAndMessage());
		const { pi } = makeMockPi(sessionManager);

		const launchId = "launch-1";
		const mockView = makeMockWorkflowView({
			launchId,
			attemptId: "attempt-correct",
		});

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: makeMockWorkClient(mockView) }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});

		// 1. Attempt ID mismatch
		await bridge.onAuditorSettle(
			"OMP-1",
			launchId,
			{ status: "applied", verdict: "PASS", attemptId: "attempt-different", event: defaultEvent },
			{ sessionManager } as any,
		);
		expect(requests.length).toBe(0);

		// 2. Verdict mismatch (receipt has PASS, settleOutcome has NEEDS_FIX)
		await bridge.onAuditorSettle(
			"OMP-1",
			launchId,
			{ status: "applied", verdict: "NEEDS_FIX", attemptId: "attempt-correct", event: defaultEvent },
			{ sessionManager } as any,
		);
		expect(requests.length).toBe(0);

		// 3. settleOutcome.launchId !== launchId param
		await bridge.onAuditorSettle(
			"OMP-1",
			launchId,
			{ status: "applied", verdict: "PASS", attemptId: "attempt-correct", launchId: "other-launch-id", event: defaultEvent },
			{ sessionManager } as any,
		);
		expect(requests.length).toBe(0);
	});

	it("workClient.workflow rejects -> zero /outcome POSTs, no throw", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-query-fail", makeBranchWithBundleAndMessage());
		const { pi } = makeMockPi(sessionManager);

		const rejectingWorkClient = {
			workflow: async () => {
				throw new Error("ledger workflow lookup failed");
			},
		} as unknown as WorkClient;

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: rejectingWorkClient }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});

		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			event: defaultEvent,
		};

		await expect(
			bridge.onAuditorSettle("OMP-1", "launch-1", settleOutcome, { sessionManager } as any),
		).resolves.toBeUndefined();
		expect(requests.length).toBe(0);
	});

	it("task tool context injection marks worker delivery and subsequent outcome uses worker_used=true; preserves owner before_agent_start behavior", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: buildMockBundle({ stage: "subagent" }) };
			}
			if (req.url.endsWith("/v1/uses")) {
				return {
					status: 200,
					body: {
						use_id: `use-${req.body.proposal_id}`,
						proposal_id: req.body.proposal_id,
					},
				};
			}
			if (req.url.includes("/outcome")) {
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-task-worker-outcome");
		const { pi, emit, entries } = makeMockPi(sessionManager);

		const fakeWitness: OwnExecutionWitness = {
			sessionId: "session-task-worker-outcome",
			cwd: "/repo",
			workspace: {
				grantId: "grant-1",
				key: "OMP-1",
				path: "/repo",
				primaryRoot: "/repo",
				branch: "feat",
				baseline: "c".repeat(40),
			},
		};

		const taskWorkClient = {
			workflow: async (_key: string): Promise<WorkflowView> => {
				return makeMockWorkflowView({
					launches: [
						{
							launch_id: "launch-task-1",
							attempt_id: "attempt-task-1",
							manifest_id: "00000000-0000-0000-0000-000000000001",
							launch_number: 1,
							task_sha256: "0".repeat(64),
							tool_call_id: "call-1",
							reserved_at: new Date().toISOString(),
						},
						{
							launch_id: "launch-owner",
							attempt_id: "attempt-owner",
							manifest_id: "00000000-0000-0000-0000-000000000001",
							launch_number: 2,
							task_sha256: "0".repeat(64),
							tool_call_id: "call-2",
							reserved_at: new Date().toISOString(),
						},
					],
					receipts: [
						{
							receipt_id: AUDIT_RECEIPT_ID,
							work_id: TEST_IDENTITY.workId,
							revision_id: TEST_IDENTITY.revisionId,
							candidate_id: TEST_IDENTITY.candidateId!,
							kind: "audit",
							issuer: "work-service/auditor-settle",
							issued_at: new Date().toISOString(),
							payload: { launch_id: "launch-task-1" },
							payload_sha256: "0".repeat(64),
							verdict: "PASS",
						},
						{
							receipt_id: AUDIT_RECEIPT_ID,
							work_id: TEST_IDENTITY.workId,
							revision_id: TEST_IDENTITY.revisionId,
							candidate_id: TEST_IDENTITY.candidateId!,
							kind: "audit",
							issuer: "work-service/auditor-settle",
							issued_at: new Date().toISOString(),
							payload: { launch_id: "launch-owner" },
							payload_sha256: "0".repeat(64),
							verdict: "PASS",
						},
					],
				});
			},
		} as unknown as WorkClient;

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: taskWorkClient }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => fakeWitness,
			resolveExecutionIdentity: async () => ({ ...TEST_IDENTITY, stage: "subagent" }),
		});

		// 1. Task tool call injects bundle and appends work-knowledge-bundle transcript marker
		const result = await bridge.handleToolCall(
			{
				type: "tool_call",
				toolName: "task",
				toolCallId: "call-task-worker-exec",
				input: { prompt: "run task subagent", context: "subagent context" },
			} as any,
			{ taskDepth: 0, sessionManager } as any,
		);

		expect(result).toBeDefined();
		const expectedBundleBytes = canonicalJson({
			mandatory: MOCK_BUNDLE.mandatory,
			optional: MOCK_BUNDLE.optional,
		});
		expect(result?.input?.context).toBeDefined();
		expect((result?.input?.context as string).startsWith(expectedBundleBytes)).toBe(true);

		// Prior to task execution completing, marker is NOT appended and worker is not marked used
		expect(entries.find(e => e.type === "work-knowledge-bundle")).toBeUndefined();
		expect(wasWorkerUsed(sessionManager.getBranch(), MOCK_BUNDLE.bundle_id)).toBe(false);

		// Post-execution task tool_result succeeds (isError false) -> appends marker
		await emit(
			"tool_result",
			{
				toolCallId: "call-task-worker-exec",
				toolName: "task",
				input: result?.input,
				content: [{ type: "text", text: "subagent finished successfully" }],
				details: { results: [{ outputPath: "worker.diff" }] },
				isError: false,
			},
			{ taskDepth: 0, sessionManager } as any,
		);

		// Transcript marker appended without model-visible duplicate message
		const marker = entries.find(e => e.type === "work-knowledge-bundle");
		expect(marker).toBeDefined();
		expect(marker?.data.bundle_id).toBe(MOCK_BUNDLE.bundle_id);
		expect(marker?.data.toolCallId).toBe("call-task-worker-exec");

		// Transcript state reflects worker delivery and replay dedupe remains exact
		const branch = sessionManager.getBranch();
		expect(wasWorkerUsed(branch, MOCK_BUNDLE.bundle_id)).toBe(true);
		expect(isBundleReplayed(branch, MOCK_BUNDLE.bundle_id)).toBe(true);

		// 2. Auditor settle produces outcome with worker_used=true
		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			event: defaultEvent,
		};

		await bridge.onAuditorSettle("OMP-1", "launch-task-1", settleOutcome, { sessionManager } as any);

		const outcomePosts = requests.filter(r => r.url.includes("/outcome"));
		expect(outcomePosts.length).toBe(2);
		expect(outcomePosts[0].body.outcome_receipt_id).toBe(AUDIT_RECEIPT_ID);
		expect(outcomePosts[0].body.worker_used).toBe(true);
		expect(outcomePosts[1].body.outcome_receipt_id).toBe(AUDIT_RECEIPT_ID);
		expect(outcomePosts[1].body.worker_used).toBe(true);

		// 3. Verify owner before_agent_start path also continues to report worker_used=true
		const ownerSessionManager = makeMockSessionManager("session-owner-outcome", makeBranchWithBundleAndMessage());
		const { pi: ownerPi } = makeMockPi(ownerSessionManager);
		const ownerBridge = registerKnowledgeBridge(ownerPi, {
			backend: makeMockBackend({ workClient: taskWorkClient }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});
		requests.length = 0;
		await ownerBridge.onAuditorSettle("OMP-1", "launch-owner", settleOutcome, { sessionManager: ownerSessionManager } as any);
		const ownerOutcomePosts = requests.filter(r => r.url.includes("/outcome"));
		expect(ownerOutcomePosts.length).toBe(2);
		expect(ownerOutcomePosts[0].body.worker_used).toBe(true);
		expect(ownerOutcomePosts[1].body.worker_used).toBe(true);
	});

	it("blocked or failed task tool_call does not append marker and produces worker_used=false on settle", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: buildMockBundle({ stage: "subagent" }) };
			}
			if (req.url.endsWith("/v1/uses")) {
				return {
					status: 200,
					body: {
						use_id: `use-${req.body.proposal_id}`,
						proposal_id: req.body.proposal_id,
					},
				};
			}
			if (req.url.includes("/outcome")) {
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-task-worker-blocked");
		const { pi, emit, entries } = makeMockPi(sessionManager);

		const fakeWitness: OwnExecutionWitness = {
			sessionId: "session-task-worker-blocked",
			cwd: "/repo",
			workspace: {
				grantId: "grant-1",
				key: "OMP-1",
				path: "/repo",
				primaryRoot: "/repo",
				branch: "feat",
				baseline: "c".repeat(40),
			},
		};

		const taskWorkClient = {
			workflow: async (_key: string): Promise<WorkflowView> => {
				return makeMockWorkflowView({
					launches: [
						{
							launch_id: "launch-task-blocked",
							attempt_id: "attempt-task-blocked",
							manifest_id: "00000000-0000-0000-0000-000000000001",
							launch_number: 1,
							task_sha256: "0".repeat(64),
							tool_call_id: "call-1",
							reserved_at: new Date().toISOString(),
						},
					],
					receipts: [
						{
							receipt_id: AUDIT_RECEIPT_ID,
							work_id: TEST_IDENTITY.workId,
							revision_id: TEST_IDENTITY.revisionId,
							candidate_id: TEST_IDENTITY.candidateId!,
							kind: "audit",
							issuer: "work-service/auditor-settle",
							issued_at: new Date().toISOString(),
							payload: { launch_id: "launch-task-blocked" },
							payload_sha256: "0".repeat(64),
							verdict: "PASS",
						},
					],
				});
			},
		} as unknown as WorkClient;

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: taskWorkClient }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => fakeWitness,
			resolveExecutionIdentity: async () => ({ ...TEST_IDENTITY, stage: "subagent" }),
		});

		// 1. Task tool call injects bundle
		const result = await bridge.handleToolCall(
			{
				type: "tool_call",
				toolName: "task",
				toolCallId: "call-task-worker-blocked",
				input: { prompt: "run task subagent", context: "subagent context" },
			} as any,
			{ taskDepth: 0, sessionManager } as any,
		);

		expect(result).toBeDefined();
		expect(entries.find(e => e.type === "work-knowledge-bundle")).toBeUndefined();

		// 2. Task execution fails / is blocked (isError: true)
		await emit(
			"tool_result",
			{
				toolCallId: "call-task-worker-blocked",
				toolName: "task",
				input: result?.input,
				content: [{ type: "text", text: "task blocked by security guard" }],
				details: null,
				isError: true,
			},
			{ taskDepth: 0, sessionManager } as any,
		);

		// No marker appended after blocked/errored task
		expect(entries.find(e => e.type === "work-knowledge-bundle")).toBeUndefined();
		const branch = sessionManager.getBranch();
		expect(wasWorkerUsed(branch, MOCK_BUNDLE.bundle_id)).toBe(false);

		// 3. Auditor settle produces outcome with worker_used=false
		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			event: defaultEvent,
		};

		await bridge.onAuditorSettle("OMP-1", "launch-task-blocked", settleOutcome, { sessionManager } as any);

		const outcomePosts = requests.filter(r => r.url.includes("/outcome"));
		expect(outcomePosts.length).toBe(2);
		expect(outcomePosts[0].body.outcome_receipt_id).toBe(AUDIT_RECEIPT_ID);
		expect(outcomePosts[0].body.worker_used).toBe(false);
		expect(outcomePosts[1].body.outcome_receipt_id).toBe(AUDIT_RECEIPT_ID);
		expect(outcomePosts[1].body.worker_used).toBe(false);
	});

	it("unrelated task tool_result does not mark worker used for a bundle bound to another task", async () => {
		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.endsWith("/v1/context/compile")) {
				return { status: 200, body: buildMockBundle({ stage: "subagent" }) };
			}
			if (req.url.endsWith("/v1/uses")) {
				return {
					status: 200,
					body: {
						use_id: `use-${req.body.proposal_id}`,
						proposal_id: req.body.proposal_id,
					},
				};
			}
			if (req.url.includes("/outcome")) {
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const sessionManager = makeMockSessionManager("session-task-unrelated");
		const { pi, emit, entries } = makeMockPi(sessionManager);

		const fakeWitness: OwnExecutionWitness = {
			sessionId: "session-task-unrelated",
			cwd: "/repo",
			workspace: {
				grantId: "grant-1",
				key: "OMP-1",
				path: "/repo",
				primaryRoot: "/repo",
				branch: "feat",
				baseline: "c".repeat(40),
			},
		};

		const taskWorkClient = {
			workflow: async (_key: string): Promise<WorkflowView> => {
				return makeMockWorkflowView({
					launches: [
						{
							launch_id: "launch-task-unrelated",
							attempt_id: "attempt-task-unrelated",
							manifest_id: "00000000-0000-0000-0000-000000000001",
							launch_number: 1,
							task_sha256: "0".repeat(64),
							tool_call_id: "call-1",
							reserved_at: new Date().toISOString(),
						},
					],
					receipts: [
						{
							receipt_id: AUDIT_RECEIPT_ID,
							work_id: TEST_IDENTITY.workId,
							revision_id: TEST_IDENTITY.revisionId,
							candidate_id: TEST_IDENTITY.candidateId!,
							kind: "audit",
							issuer: "work-service/auditor-settle",
							issued_at: new Date().toISOString(),
							payload: { launch_id: "launch-task-unrelated" },
							payload_sha256: "0".repeat(64),
							verdict: "PASS",
						},
					],
				});
			},
		} as unknown as WorkClient;

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: taskWorkClient }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			getExecutionWitness: () => fakeWitness,
			resolveExecutionIdentity: async () => ({ ...TEST_IDENTITY, stage: "subagent" }),
		});

		// 1. Task call A injects bundle
		await bridge.handleToolCall(
			{
				type: "tool_call",
				toolName: "task",
				toolCallId: "call-task-A",
				input: { prompt: "run task subagent A", context: "subagent context" },
			} as any,
			{ taskDepth: 0, sessionManager } as any,
		);

		// 2. An unrelated task result for call B completes (isError false)
		await emit(
			"tool_result",
			{
				toolCallId: "call-task-B-unrelated",
				toolName: "task",
				input: { prompt: "unrelated subagent" },
				content: [{ type: "text", text: "done" }],
				details: null,
				isError: false,
			},
			{ taskDepth: 0, sessionManager } as any,
		);

		// The marker for bundle A must NOT be appended
		expect(entries.find(e => e.type === "work-knowledge-bundle")).toBeUndefined();
		const branch = sessionManager.getBranch();
		expect(wasWorkerUsed(branch, MOCK_BUNDLE.bundle_id)).toBe(false);

		// 3. Settle reports worker_used=false for bundle A
		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			event: defaultEvent,
		};
		await bridge.onAuditorSettle("OMP-1", "launch-task-unrelated", settleOutcome, { sessionManager } as any);

		const outcomePosts = requests.filter(r => r.url.includes("/outcome"));
		expect(outcomePosts.length).toBe(2);
		expect(outcomePosts[0].body.worker_used).toBe(false);
		expect(outcomePosts[1].body.worker_used).toBe(false);
	});

	it("settle outcome sets worker_used=false when bundle was compiled but never delivered to worker", async () => {
		const sessionManager = makeMockSessionManager("session-undelivered", [
			{
				id: "bundle-entry",
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: BUNDLE_ID,
					use_ids: [USE_ID_1],
					task_work_id: TEST_IDENTITY.workId,
					task_revision_id: TEST_IDENTITY.revisionId,
					task_candidate_id: TEST_IDENTITY.candidateId,
				},
			},
		]);

		const { fetchImpl, requests } = makeFakeFetch(req => {
			if (req.url.includes("/outcome")) {
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const { pi } = makeMockPi(sessionManager);
		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({
				workClient: makeMockWorkClient(
					makeMockWorkflowView({ launchId: "launch-undelivered" }),
				),
			}),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});

		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			event: defaultEvent,
		};

		await bridge.onAuditorSettle("OMP-1", "launch-undelivered", settleOutcome, { sessionManager } as any);

		const outcomePosts = requests.filter(r => r.url.includes("/outcome"));
		expect(outcomePosts.length).toBe(1);
		expect(outcomePosts[0].body.outcome_receipt_id).toBe(AUDIT_RECEIPT_ID);
		expect(outcomePosts[0].body.worker_used).toBe(false);
	});

	it("view.receipts contains only kind:'push' -> zero /outcome POSTs", async () => {
		const { fetchImpl, requests } = makeFakeFetch(() => ({ status: 200 }));
		const sessionManager = makeMockSessionManager("session-301", makeBranchWithBundleAndMessage());
		const { pi } = makeMockPi(sessionManager);

		const pushReceipt: EvidenceReceipt = {
			receipt_id: "00000000-0000-0000-0000-000000000099",
			work_id: TEST_IDENTITY.workId,
			revision_id: TEST_IDENTITY.revisionId,
			candidate_id: TEST_IDENTITY.candidateId!,
			kind: "push",
			issuer: "work-service/pusher",
			issued_at: new Date().toISOString(),
			payload: { launch_id: "launch-1" },
			payload_sha256: "0".repeat(64),
		};

		const mockView = makeMockWorkflowView({
			launchId: "launch-1",
			receipts: [pushReceipt],
		});

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: makeMockWorkClient(mockView) }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});

		await bridge.onAuditorSettle(
			"OMP-1",
			"launch-1",
			{ status: "applied", verdict: "PASS", event: defaultEvent },
			{ sessionManager } as any,
		);
		expect(requests.length).toBe(0);

		// Also verify: backend without workClient fails closed with zero POSTs
		const bridgeNoClient = registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});
		await bridgeNoClient.onAuditorSettle(
			"OMP-1",
			"launch-1",
			{ status: "applied", verdict: "PASS", event: defaultEvent },
			{ sessionManager } as any,
		);
		expect(requests.length).toBe(0);
	});

	it("outcome POST 503 -> identity persisted; simulated restart session_start -> exactly one retry POST", async () => {
		let settleFetchCalls = 0;
		let retryFetchCalls = 0;

		const { fetchImpl } = makeFakeFetch(req => {
			if (req.url.includes("/outcome")) {
				settleFetchCalls++;
				return { status: 503, body: { error: { code: "native_unavailable" } } };
			}
			return { status: 404 };
		});

		const initialBranch = makeBranchWithBundleAndMessage();
		const sessionManager1 = makeMockSessionManager("session-302", initialBranch);
		const { pi: pi1, entries: entries1 } = makeMockPi(sessionManager1);

		const mockView = makeMockWorkflowView({ launchId: "launch-1" });

		const bridge1 = registerKnowledgeBridge(pi1, {
			backend: makeMockBackend({ workClient: makeMockWorkClient(mockView) }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
		});

		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			event: defaultEvent,
		};

		// 1. Settle fails with 503 -> pending outcome entries persisted
		await bridge1.onAuditorSettle("OMP-1", "launch-1", settleOutcome, { sessionManager: sessionManager1 } as any);

		const pendingEntries = entries1.filter(e => e.type === "work-now-pending-outcome");
		expect(pendingEntries.length).toBe(2);
		expect(pendingEntries[0].data.use_id).toBe(USE_ID_1);
		expect(pendingEntries[0].data.outcome_receipt_id).toBe(AUDIT_RECEIPT_ID);

		// 2. Simulate restart: a new session manager loads the persisted branch
		const reloadedBranch = [...sessionManager1.getBranch()];
		const sessionManager2 = makeMockSessionManager("session-303", reloadedBranch);

		const { fetchImpl: retryFetch, requests: retryRequests } = makeFakeFetch(req => {
			if (req.url.includes("/outcome")) {
				retryFetchCalls++;
				return { status: 200, body: { status: "recorded" } };
			}
			return { status: 404 };
		});

		const { pi: pi2, emit: emit2, entries: entries2 } = makeMockPi(sessionManager2);

		registerKnowledgeBridge(pi2, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl: retryFetch,
		});

		// Trigger session_start to run durable retry
		await emit2("session_start", {}, { sessionManager: sessionManager2 });

		// Exactly one retry POST per pending use_id
		expect(retryRequests.length).toBe(2);
		expect(retryRequests[0].url).toContain(USE_ID_1);
		expect(retryRequests[1].url).toContain(USE_ID_2);

		// Outcome acks recorded
		const ackEntries = entries2.filter(e => e.type === "work-now-outcome-ack");
		expect(ackEntries.length).toBe(2);

		// Subsequent session start should not retry acknowledged items
		retryRequests.length = 0;
		await emit2("session_start", {}, { sessionManager: sessionManager2 });
		expect(retryRequests.length).toBe(0);
	});
});

describe("Knowledge Bridge — 4xx Outcome Signal & Retry (R4)", () => {
	it("/outcome returns 422 -> zero work-now-pending-outcome entries, exactly one notice containing 'outcome rejected', no throw; a second 4xx in the same session adds no second notice", async () => {
		let outcomeCalls = 0;
		const { fetchImpl } = makeFakeFetch(req => {
			if (req.url.includes("/outcome")) {
				outcomeCalls++;
				return { status: 422, body: { error: { code: "receipt_task_mismatch" } } };
			}
			return { status: 404 };
		});

		const branch = [
			{
				id: "bundle-entry",
				type: "custom",
				customType: "work-now-bundle",
				data: {
					bundle_id: "00000000-0000-0000-0000-000000000099",
					uses: [
						{ proposal_id: "00000000-0000-0000-0000-000000000051", use_id: "use-422-1" },
						{ proposal_id: "00000000-0000-0000-0000-000000000052", use_id: "use-422-2" },
					],
					use_ids: ["use-422-1", "use-422-2"],
					task_work_id: TEST_IDENTITY.workId,
					task_revision_id: TEST_IDENTITY.revisionId,
					task_candidate_id: TEST_IDENTITY.candidateId,
				},
			},
			{
				id: "bundle-msg",
				type: "custom_message",
				customType: "work-knowledge-bundle",
				details: {
					bundle_id: "00000000-0000-0000-0000-000000000099",
				},
			},
		];

		const sessionManager = makeMockSessionManager("session-422", branch);
		const { pi, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		const launchId = "launch-422";
		const mockView = makeMockWorkflowView({ launchId });

		const bridge = registerKnowledgeBridge(pi, {
			backend: makeMockBackend({ workClient: makeMockWorkClient(mockView) }),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			notices,
		});

		const settleOutcome: CloseAttemptOutcome = {
			status: "applied",
			verdict: "PASS",
			attemptId: "attempt-1",
			event: defaultEvent,
		};

		// Must not throw on definitive 4xx
		await expect(
			bridge.onAuditorSettle("OMP-1", launchId, settleOutcome, { sessionManager } as unknown as ExtensionContext),
		).resolves.toBeUndefined();

		// Both uses were attempted
		expect(outcomeCalls).toBe(2);

		// Zero work-now-pending-outcome entries written
		const pendingEntries = entries.filter(e => e.type === "work-now-pending-outcome");
		expect(pendingEntries.length).toBe(0);

		// Exactly one notice queued containing 'outcome rejected'
		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("outcome rejected (422)");

		// A second onAuditorSettle call in the same session adds no second notice
		await bridge.onAuditorSettle("OMP-1", launchId, settleOutcome, { sessionManager } as unknown as ExtensionContext);
		expect(notices.length).toBe(1);
	});

	it("session_start retry of a pending entry receiving 403 -> one work-now-outcome-ack with rejected:true, one notice, not retried on following session_start", async () => {
		let retryCalls = 0;
		let returnStatus = 403;

		const { fetchImpl } = makeFakeFetch(req => {
			if (req.url.includes("/outcome")) {
				retryCalls++;
				return { status: returnStatus, body: { error: { code: "forbidden" } } };
			}
			return { status: 404 };
		});

		// Transcript preloaded with a pending outcome from a prior transient failure
		const sessionManager = makeMockSessionManager("session-403-retry", [
			{
				id: "pending-1",
				type: "custom",
				customType: "work-now-pending-outcome",
				data: {
					use_id: "use-pending-403",
					outcome_receipt_id: AUDIT_RECEIPT_ID,
					worker_used: true,
					task_work_id: TEST_IDENTITY.workId,
					task_revision_id: TEST_IDENTITY.revisionId,
					task_candidate_id: TEST_IDENTITY.candidateId,
				},
			},
		]);

		const { pi, emit, entries } = makeMockPi(sessionManager);
		const notices: string[] = [];

		registerKnowledgeBridge(pi, {
			backend: makeMockBackend(),
			workConfig: TEST_WORK_CONFIG,
			knowledgeConfig: TEST_KNOWLEDGE_CONFIG,
			fetchImpl,
			notices,
		});

		// 1. First session_start: retry receives 403 -> terminal rejection
		await emit("session_start", {}, { sessionManager });

		expect(retryCalls).toBe(1);

		// Terminal rejection writes one work-now-outcome-ack with rejected: true
		const acks = entries.filter(e => e.type === "work-now-outcome-ack");
		expect(acks.length).toBe(1);
		expect(acks[0].data).toEqual({
			use_id: "use-pending-403",
			outcome_receipt_id: AUDIT_RECEIPT_ID,
			worker_used: true,
			task_work_id: TEST_IDENTITY.workId,
			task_revision_id: TEST_IDENTITY.revisionId,
			task_candidate_id: TEST_IDENTITY.candidateId,
			status: 403,
			rejected: true,
		});

		// Exactly one notice containing 'outcome rejected (403)'
		expect(notices.length).toBe(1);
		expect(notices[0]).toContain("outcome rejected (403)");
		expect(notices[0]).toContain("use=use-pending-403");

		// 2. Following session_start: rejected outcome is not retried or notified (no duplicate retry)
		returnStatus = 200;
		await emit("session_start", {}, { sessionManager });

		// Call count remains 1: retain assertion that no duplicate retry occurs
		expect(retryCalls).toBe(1);

		// Not re-notified on following session start
		expect(notices.length).toBe(1);

		// No duplicate ack entry appended
		const acksAfterFollow = entries.filter(e => e.type === "work-now-outcome-ack");
		expect(acksAfterFollow.length).toBe(1);
	});
});

describe("Knowledge Bridge — selectAuditReceiptForLaunch selector", () => {
	const LAUNCH_ID = "00000000-0000-0000-0000-000000000091";
	const ATTEMPT_ID = "00000000-0000-0000-0000-000000000092";

	function makeSelectorFixture() {
		const launch: AuditorLaunch = {
			launch_id: LAUNCH_ID,
			attempt_id: ATTEMPT_ID,
			manifest_id: "00000000-0000-0000-0000-000000000001",
			launch_number: 1,
			task_sha256: "0".repeat(64),
			tool_call_id: "call-1",
			reserved_at: new Date().toISOString(),
		};
		const receipt: EvidenceReceipt = {
			receipt_id: AUDIT_RECEIPT_ID,
			work_id: TEST_IDENTITY.workId,
			revision_id: TEST_IDENTITY.revisionId,
			candidate_id: TEST_IDENTITY.candidateId!,
			kind: "audit",
			issuer: "work-service/auditor-settle",
			issued_at: new Date().toISOString(),
			payload: { launch_id: LAUNCH_ID },
			payload_sha256: "0".repeat(64),
			verdict: "PASS",
		};
		return { launch, receipt };
	}

	it("returns receipt when exactly one matching launch and receipt exist", () => {
		const { launch, receipt } = makeSelectorFixture();
		const result = selectAuditReceiptForLaunch(
			{ auditor_launches: [launch], receipts: [receipt] },
			LAUNCH_ID,
			{ attemptId: ATTEMPT_ID, verdict: "PASS" },
		);
		expect(result).toBe(receipt);
	});

	it("returns null when launch is missing or attemptId mismatches", () => {
		const { launch, receipt } = makeSelectorFixture();
		// Missing launch
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [], receipts: [receipt] },
				LAUNCH_ID,
				{ attemptId: ATTEMPT_ID, verdict: "PASS" },
			),
		).toBeNull();

		// Attempt mismatch
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [launch], receipts: [receipt] },
				LAUNCH_ID,
				{ attemptId: "different-attempt", verdict: "PASS" },
			),
		).toBeNull();
	});

	it("returns null when receipt is missing or ambiguous", () => {
		const { launch, receipt } = makeSelectorFixture();
		// Missing receipt
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [launch], receipts: [] },
				LAUNCH_ID,
				{ attemptId: ATTEMPT_ID, verdict: "PASS" },
			),
		).toBeNull();

		// Ambiguous receipts (two receipts with same launch_id)
		const receipt2: EvidenceReceipt = {
			...receipt,
			receipt_id: "00000000-0000-0000-0000-000000000089",
		};
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [launch], receipts: [receipt, receipt2] },
				LAUNCH_ID,
				{ attemptId: ATTEMPT_ID, verdict: "PASS" },
			),
		).toBeNull();
	});

	it("returns null when kind, issuer, work_id, revision_id, or verdict mismatch", () => {
		const { launch, receipt } = makeSelectorFixture();

		// Wrong kind
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [launch], receipts: [{ ...receipt, kind: "push" }] },
				LAUNCH_ID,
				{ attemptId: ATTEMPT_ID, verdict: "PASS" },
			),
		).toBeNull();

		// Wrong issuer
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [launch], receipts: [{ ...receipt, issuer: "other-issuer" }] },
				LAUNCH_ID,
				{ attemptId: ATTEMPT_ID, verdict: "PASS" },
			),
		).toBeNull();

		// Empty work_id
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [launch], receipts: [{ ...receipt, work_id: "" }] },
				LAUNCH_ID,
				{ attemptId: ATTEMPT_ID, verdict: "PASS" },
			),
		).toBeNull();

		// Empty revision_id
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [launch], receipts: [{ ...receipt, revision_id: "" }] },
				LAUNCH_ID,
				{ attemptId: ATTEMPT_ID, verdict: "PASS" },
			),
		).toBeNull();

		// Verdict mismatch
		expect(
			selectAuditReceiptForLaunch(
				{ auditor_launches: [launch], receipts: [receipt] },
				LAUNCH_ID,
				{ attemptId: ATTEMPT_ID, verdict: "NEEDS_FIX" },
			),
		).toBeNull();
	});
});

describe("Knowledge Config & Capability Loading (config.ts)", () => {
	it("loadKnowledgeConfig returns null when unconfigured", () => {
		const originalEnv = { ...process.env };
		delete process.env.OMP_KNOWLEDGE_BASE_URL;
		delete process.env.OMP_KNOWLEDGE_URL;
		delete process.env.OMP_KNOWLEDGE_CONFIG_DIR;
		delete process.env.OMP_KNOWLEDGE_PORT;
		delete process.env.OMP_KNOWLEDGE_HOST;

		// With a non-existent config path
		process.env.OMP_KNOWLEDGE_CONFIG_DIR = "/tmp/non-existent-knowledge-dir-12345";
		const cfg = loadKnowledgeConfig();
		expect(cfg).toBeNull();

		process.env = originalEnv;
	});

	it("loadKnowledgeConfig refuses non-loopback base_url", () => {
		const originalEnv = { ...process.env };
		process.env.OMP_KNOWLEDGE_BASE_URL = "http://10.0.0.1:18090";

		expect(() => loadKnowledgeConfig()).toThrow("is not loopback");

		process.env = originalEnv;
	});

	it("loadKnowledgeConfig accepts loopback URL and applies budget limit", () => {
		const originalEnv = { ...process.env };
		process.env.OMP_KNOWLEDGE_BASE_URL = "http://127.0.0.1:18090";
		process.env.OMP_KNOWLEDGE_BUDGET_LIMIT = "65536";

		const cfg = loadKnowledgeConfig();
		expect(cfg).not.toBeNull();
		expect(cfg?.baseUrl).toBe("http://127.0.0.1:18090");
		expect(cfg?.budgetLimit).toBe(65536);

		process.env = originalEnv;
	});

	it("loadKnowledgeBearer loads bearer matching requiredScope from mode 0600 file", () => {
		const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "kb-cap-test-"));
		const capDir = path.join(tmpDir, "capabilities");
		fs.mkdirSync(capDir, { mode: 0o700 });

		const readerFile = path.join(capDir, "reader.json");
		fs.writeFileSync(
			readerFile,
			JSON.stringify({
				token: "tok_reader_val",
				scopes: ["knowledge.read"],
			}),
			{ mode: 0o600 },
		);

		const ingesterFile = path.join(capDir, "ingester.json");
		fs.writeFileSync(
			ingesterFile,
			JSON.stringify({
				token: "tok_ingest_val",
				scopes: ["knowledge.read", "knowledge.ingest"],
			}),
			{ mode: 0o600 },
		);

		const cfg: KnowledgeClientConfig = {
			baseUrl: "http://127.0.0.1:18090",
			capabilitiesDir: capDir,
		};

		const readToken = loadKnowledgeBearer(cfg, "knowledge.read");
		expect(readToken).toBeDefined();

		const ingestToken = loadKnowledgeBearer(cfg, "knowledge.ingest");
		expect(ingestToken).toBe("tok_ingest_val");

		const adminToken = loadKnowledgeBearer(cfg, "knowledge.admin");
		expect(adminToken).toBeNull();

		// Clean up
		fs.rmSync(tmpDir, { recursive: true, force: true });
	});
});
