import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import * as ai from "@oh-my-pi/pi-ai";
import type { Model } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { resolveModelRoleValue } from "@oh-my-pi/pi-coding-agent/config/model-resolver";
import type { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import * as sdkModule from "@oh-my-pi/pi-coding-agent/sdk";
import type { CreateAgentSessionOptions, CreateAgentSessionResult } from "@oh-my-pi/pi-coding-agent/sdk";
import type { AgentSession, AgentSessionEvent, PromptOptions } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import type { ServingModel } from "@oh-my-pi/pi-coding-agent/session/retry-fallback-chains";
import { EventBus } from "@oh-my-pi/pi-coding-agent/utils/event-bus";
import { createSessionDefaults } from "../../packages/coding-agent/test/helpers/session-defaults";
import { prepareNativeAuditRunner } from "../extensions/workflow/auditor-runner";

/**
 * Valid contract-conforming AuditResult payload for native auditor output validation.
 * Conforms to schema.json #/$defs/AuditResult and auditor.md frontmatter.
 */
const validAuditResultPayload = {
	audit_result: {
		schema_version: 1,
		verdict: "PASS",
		evidence: {
			references: [
				{
					kind: "command",
					ref: "bun test session-system/tests/model-policy-native-review.test.ts",
					result: "pass",
				},
			],
		},
		criteria: [
			{
				criterion_id: "AC-1",
				status: "met",
				evidence: "Governed reviewer identity matches execution boundary",
			},
		],
		findings: [],
		out_of_scope: [],
		remaining_questions: [],
	},
};

/**
 * Creates a mock session that follows the executor-pass-through fixture pattern,
 * emitting an assistant completion and a successful yield with structured output.
 */
function createMockSession(
	onPrompt: (params: { emit: (event: AgentSessionEvent) => void }) => void,
	servingModel?: ServingModel,
): AgentSession {
	const listeners: Array<(event: AgentSessionEvent) => void> = [];
	const emit = (event: AgentSessionEvent) => {
		for (const listener of listeners) listener(event);
	};
	const session = {
		...createSessionDefaults(),
		state: { messages: [] },
		agent: { state: { systemPrompt: ["test"] } },
		model: undefined,
		servingModel,
		extensionRunner: undefined,
		sessionManager: { appendSessionInit: () => {} },
		getActiveToolNames: () => ["read", "yield"],
		getEnabledToolNames: () => ["read", "yield"],
		subscribe: (listener: (event: AgentSessionEvent) => void) => {
			listeners.push(listener);
			return () => {
				const index = listeners.indexOf(listener);
				if (index >= 0) listeners.splice(index, 1);
			};
		},
		prompt: async (_text: string, _options?: PromptOptions) => {
			onPrompt({ emit });
			return true;
		},
	};
	return session as unknown as AgentSession;
}

function createSessionResult(session: AgentSession): CreateAgentSessionResult {
	return {
		session,
		extensionsResult: { extensions: [], errors: [], runtime: {} as unknown } as never,
		setToolUIContext: () => {},
		eventBus: new EventBus(),
	};
}

describe("native audit model policy review and boundary observation (OMP-615-s08-s06)", () => {
	const fixtureDirs: string[] = [];

	// Load authoritative packaged routing policy for governance assertions
	const policyPath = path.resolve(
		import.meta.dir,
		"../../python/omp-work/src/omp_work/routing/routing-policy.v1.json",
	);

	// Admitted reviewer model from catalog
	const codexSol = getBundledModel("openai-codex", "gpt-5.6-sol");
	// Competing provider sharing the exact model id "gpt-5.6-sol"
	const openaiSol = getBundledModel("openai", "gpt-5.6-sol");

	if (!codexSol || !openaiSol) {
		throw new Error("Expected bundled models openai-codex/gpt-5.6-sol and openai/gpt-5.6-sol to exist");
	}

	const availableModels: Model[] = [codexSol, openaiSol];

	afterEach(async () => {
		vi.restoreAllMocks();
		for (const dir of fixtureDirs.splice(0)) {
			await fs.rm(dir, { recursive: true, force: true });
		}
	});

	/**
	 * Sets up an isolated workspace directory with discovered auditor.md and settings.json.
	 */
	async function createIsolatedAuditWorkspace(auditRoleSelector: string): Promise<string> {
		const tmpCwd = await fs.mkdtemp(path.join(os.tmpdir(), "omp-native-review-"));
		fixtureDirs.push(tmpCwd);

		const agentsDir = path.join(tmpCwd, ".omp", "agents");
		await fs.mkdir(agentsDir, { recursive: true });

		const sourceAuditorPath = path.resolve(import.meta.dir, "../agents/auditor.md");
		await fs.copyFile(sourceAuditorPath, path.join(agentsDir, "auditor.md"));

		const settingsContent = JSON.stringify({
			modelRoles: {
				audit: auditRoleSelector,
			},
		});
		await fs.writeFile(path.join(tmpCwd, ".omp", "settings.json"), settingsContent);

		return tmpCwd;
	}

	test("governed reviewer identity and lane mapping observed at execution boundary", async () => {
		const policy = await Bun.file(policyPath).json();
		const admittedReviewer = policy.stages.audit.reviewers[0]; // "openai-codex/gpt-5.6-sol:medium"
		const auditStageProvider = policy.stages.audit.provider; // "kimi"
		const auditStageModel = policy.stages.audit.model; // "kimi-code/k3-256k"
		const codexConcurrencyCap = policy.concurrency.providers.openai_codex; // 1
		const codexLaneOwner = policy.governance.lanes.openai_codex.unresolved_owner; // "Chris"

		expect(admittedReviewer).toBe("openai-codex/gpt-5.6-sol:medium");
		expect(auditStageProvider).toBe("kimi");
		expect(auditStageModel).toBe("kimi-code/k3-256k");
		expect(codexConcurrencyCap).toBe(1);
		expect(codexLaneOwner).toBe("Chris");

		// Reviewer role selector without effort suffix for model assignment
		const reviewerSelector = "openai-codex/gpt-5.6-sol";
		const tmpCwd = await createIsolatedAuditWorkspace(reviewerSelector);

		const settings = Settings.isolated({
			modelRoles: { audit: reviewerSelector },
		});

		// 1. Spy at completeSimple transport preflight
		const preflightCalls: Array<{ provider: string; id: string }> = [];
		vi.spyOn(ai, "completeSimple").mockImplementation(async (model) => {
			preflightCalls.push({ provider: model.provider, id: model.id });
			return {
				stopReason: "stop",
				content: [{ type: "text", text: "OK" }],
			} as never;
		});

		// Credential resolver tracking
		const preflightApiKeyRequests: Array<{ provider: string; id: string }> = [];
		const subprocessResolverRequests: Array<{ provider: string; id: string; attemptId: string }> = [];
		const mockRegistry = {
			authStorage: {},
			getAvailable: () => availableModels,
			getApiKey: vi.fn(async (model: Model) => {
				preflightApiKeyRequests.push({ provider: model.provider, id: model.id });
				return "test-audit-api-key";
			}),
			resolver: vi.fn((model: Model, attemptId: string) => {
				subprocessResolverRequests.push({ provider: model.provider, id: model.id, attemptId });
				return async () => "test-audit-resolver-token";
			}),
		} as unknown as ModelRegistry;

		// 2. Spy at external SDK createAgentSession execution boundary
		const sessionCreationCalls: Array<{
			model?: { provider: string; id: string };
			modelRole?: string;
			modelOverride?: unknown;
		}> = [];

		vi.spyOn(sdkModule, "createAgentSession").mockImplementation(async (opts: CreateAgentSessionOptions) => {
			sessionCreationCalls.push({
				model: opts.model ? { provider: opts.model.provider, id: opts.model.id } : undefined,
				modelRole: opts.modelRole,
				modelOverride: opts.modelOverride,
			});

			// If the session requests credentials via getApiKey, invoke it to exercise the resolver
			if (opts.getApiKey && opts.model) {
				await opts.getApiKey(opts.model);
			}

			const session = createMockSession(
				({ emit }) => {
					// Assistant message produces requests > 0
					emit({
						type: "message_end",
						message: { role: "assistant", content: [{ type: "text", text: "Audit report generated." }] },
					} as AgentSessionEvent);

					// Tool yield with valid structured AuditResult
					emit({
						type: "tool_execution_end",
						toolCallId: "tool-yield-1",
						toolName: "yield",
						result: {
							content: [{ type: "text", text: "Yield accepted" }],
							details: {
								status: "success",
								data: validAuditResultPayload,
							},
						},
						isError: false,
					} as AgentSessionEvent);
				},
				{
					selector: `${opts.model?.provider}/${opts.model?.id}`,
					modelIdentity: `${opts.model?.provider}/${opts.model?.id}`,
					isFallback: false,
					contextWindow: opts.model?.contextWindow,
				},
			);

			return createSessionResult(session);
		});

		// Context with real resolveModelRoleValue
		const ctx = {
			cwd: tmpCwd,
			models: {
				resolve: (role: string) => resolveModelRoleValue(role, availableModels, { settings }).model,
			},
			modelRegistry: mockRegistry,
			taskDepth: 0,
		} as unknown as ExtensionContext;

		// Exercise prepareNativeAuditRunner
		const runner = await prepareNativeAuditRunner(ctx);
		expect(typeof runner).toBe("function");

		// Run the real runner (which executes real task/executor.runSubprocess)
		const attemptId = "attempt-native-audit-001";
		const runResult = await runner("Perform native audit review on OMP-615 candidate", attemptId);

		// Assertions on the preflight boundary
		expect(preflightCalls).toHaveLength(1);
		expect(preflightCalls[0]).toEqual({ provider: "openai-codex", id: "gpt-5.6-sol" });
		expect(preflightApiKeyRequests).toHaveLength(1);
		expect(preflightApiKeyRequests[0]).toEqual({ provider: "openai-codex", id: "gpt-5.6-sol" });

		// Assertions on the subprocess session creation boundary
		expect(sessionCreationCalls).toHaveLength(1);
		expect(sessionCreationCalls[0]?.model).toEqual({ provider: "openai-codex", id: "gpt-5.6-sol" });

		// Assertions on the subprocess credential resolver boundary
		expect(subprocessResolverRequests).toHaveLength(1);
		expect(subprocessResolverRequests[0]).toEqual({
			provider: "openai-codex",
			id: "gpt-5.6-sol",
			attemptId,
		});

		// Assert that captured model matches governed reviewer identity and documented lane
		const governedWireProvider = "openai-codex";
		const governedModelId = "gpt-5.6-sol";
		const documentedLane = "openai_codex";

		expect(preflightCalls[0]?.provider).toBe(governedWireProvider);
		expect(preflightCalls[0]?.id).toBe(governedModelId);
		expect(sessionCreationCalls[0]?.model?.provider).toBe(governedWireProvider);
		expect(sessionCreationCalls[0]?.model?.id).toBe(governedModelId);
		expect(subprocessResolverRequests[0]?.provider).toBe(governedWireProvider);
		expect(subprocessResolverRequests[0]?.id).toBe(governedModelId);

		// Verify that the wire provider matches the concurrency lane name
		expect(governedWireProvider.replace("-", "_")).toBe(documentedLane);

		// Verify distinctness from audit stage route (kimi/kimi-code/k3-256k in kimi lane)
		expect(governedWireProvider).not.toBe(auditStageProvider);
		expect(governedModelId).not.toBe(auditStageModel);
		expect(documentedLane).not.toBe(policy.stages.audit.provider);

		// Assert runner outcome
		expect(runResult.started).toBe(true);
		expect(runResult.error).toBeUndefined();
		expect(runResult.payload).toBeDefined();

		const parsedPayload = JSON.parse(runResult.payload!);
		expect(parsedPayload.audit_result.verdict).toBe("PASS");
		expect(runResult.resolvedModel).toBe("openai-codex/gpt-5.6-sol");
		expect(runResult.resolvedModelIsFallback).toBeFalsy();

		// Primary agreement assertion: must match governed reviewer and not be a fallback
		const isPrimaryAgreement =
			runResult.resolvedModel === "openai-codex/gpt-5.6-sol" && !runResult.resolvedModelIsFallback;
		expect(isPrimaryAgreement).toBe(true);
	});

	test("detects provider drift when @audit role points to another provider sharing the model id", async () => {
		// Change isolated @audit to openai/gpt-5.6-sol (shares model id with openai-codex/gpt-5.6-sol)
		const driftSelector = "openai/gpt-5.6-sol";
		const tmpCwd = await createIsolatedAuditWorkspace(driftSelector);

		const settings = Settings.isolated({
			modelRoles: { audit: driftSelector },
		});

		const preflightCalls: Array<{ provider: string; id: string }> = [];
		vi.spyOn(ai, "completeSimple").mockImplementation(async (model) => {
			preflightCalls.push({ provider: model.provider, id: model.id });
			return {
				stopReason: "stop",
				content: [{ type: "text", text: "OK" }],
			} as never;
		});

		const sessionCreationCalls: Array<{ model?: { provider: string; id: string } }> = [];
		vi.spyOn(sdkModule, "createAgentSession").mockImplementation(async (opts: CreateAgentSessionOptions) => {
			sessionCreationCalls.push({
				model: opts.model ? { provider: opts.model.provider, id: opts.model.id } : undefined,
			});

			const session = createMockSession(
				({ emit }) => {
					emit({
						type: "message_end",
						message: { role: "assistant", content: [{ type: "text", text: "Drift run complete." }] },
					} as AgentSessionEvent);
					emit({
						type: "tool_execution_end",
						toolCallId: "tool-yield-drift",
						toolName: "yield",
						result: {
							content: [{ type: "text", text: "Yield accepted" }],
							details: { status: "success", data: validAuditResultPayload },
						},
						isError: false,
					} as AgentSessionEvent);
				},
				{
					selector: `${opts.model?.provider}/${opts.model?.id}`,
					modelIdentity: `${opts.model?.provider}/${opts.model?.id}`,
					isFallback: false,
					contextWindow: opts.model?.contextWindow,
				},
			);

			return createSessionResult(session);
		});

		const mockRegistry = {
			authStorage: {},
			getAvailable: () => availableModels,
			getApiKey: vi.fn(async () => "test-drift-api-key"),
			resolver: vi.fn(() => async () => "test-drift-resolver-token"),
		} as unknown as ModelRegistry;

		const ctx = {
			cwd: tmpCwd,
			models: {
				resolve: (role: string) => resolveModelRoleValue(role, availableModels, { settings }).model,
			},
			modelRegistry: mockRegistry,
			taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeAuditRunner(ctx);
		const result = await runner("Perform audit with drifted provider", "attempt-drift-001");

		expect(result.started).toBe(true);

		// Boundary capture asserts execution switched provider to "openai", exposing drift
		expect(preflightCalls[0]?.provider).toBe("openai");
		expect(preflightCalls[0]?.id).toBe("gpt-5.6-sol");
		expect(sessionCreationCalls[0]?.model?.provider).toBe("openai");
		expect(sessionCreationCalls[0]?.model?.id).toBe("gpt-5.6-sol");

		// Governed reviewer wire provider is "openai-codex", NOT "openai"
		const governedReviewerWire = "openai-codex";
		expect(sessionCreationCalls[0]?.model?.provider).not.toBe(governedReviewerWire);

		// Lane mapping: "openai" is NOT the governed "openai_codex" lane
		const actualLane = sessionCreationCalls[0]?.model?.provider.replace("-", "_");
		expect(actualLane).not.toBe("openai_codex");

		// Primary agreement does NOT hold because resolved model provider drifted
		expect(result.resolvedModel).toBe("openai/gpt-5.6-sol");
		const isGovernedAgreement =
			result.resolvedModel === "openai-codex/gpt-5.6-sol" && !result.resolvedModelIsFallback;
		expect(isGovernedAgreement).toBe(false);
	});

	test("asserts fallback use is exposed and never counted as primary agreement", async () => {
		const reviewerSelector = "openai-codex/gpt-5.6-sol";
		const tmpCwd = await createIsolatedAuditWorkspace(reviewerSelector);

		const settings = Settings.isolated({
			modelRoles: { audit: reviewerSelector },
		});

		vi.spyOn(ai, "completeSimple").mockResolvedValue({
			stopReason: "stop",
			content: [{ type: "text", text: "OK" }],
		} as never);

		// Simulate session where servingModel indicates a fallback was used
		vi.spyOn(sdkModule, "createAgentSession").mockImplementation(async (opts: CreateAgentSessionOptions) => {
			const session = createMockSession(
				({ emit }) => {
					emit({
						type: "message_end",
						message: { role: "assistant", content: [{ type: "text", text: "Fallback run completed." }] },
					} as AgentSessionEvent);
					emit({
						type: "tool_execution_end",
						toolCallId: "tool-yield-fallback",
						toolName: "yield",
						result: {
							content: [{ type: "text", text: "Yield accepted" }],
							details: { status: "success", data: validAuditResultPayload },
						},
						isError: false,
					} as AgentSessionEvent);
				},
				{
					selector: "openai-codex/gpt-5.6-sol",
					modelIdentity: "openai-codex/gpt-5.6-sol",
					isFallback: true, // Fallback model activated
					contextWindow: opts.model?.contextWindow,
				},
			);

			return createSessionResult(session);
		});

		const mockRegistry = {
			authStorage: {},
			getAvailable: () => availableModels,
			getApiKey: vi.fn(async () => "test-api-key"),
			resolver: vi.fn(() => async () => "test-resolver-token"),
		} as unknown as ModelRegistry;

		const ctx = {
			cwd: tmpCwd,
			models: {
				resolve: (role: string) => resolveModelRoleValue(role, availableModels, { settings }).model,
			},
			modelRegistry: mockRegistry,
			taskDepth: 0,
		} as unknown as ExtensionContext;

		const runner = await prepareNativeAuditRunner(ctx);
		const result = await runner("Perform audit with fallback serving model", "attempt-fallback-001");

		expect(result.started).toBe(true);

		// Assert fallback use is explicitly exposed on the runner result
		expect(result.resolvedModelIsFallback).toBe(true);

		// Contract: fallback must NEVER be counted as primary agreement
		const isPrimaryAgreement =
			result.resolvedModel === "openai-codex/gpt-5.6-sol" && !result.resolvedModelIsFallback;
		expect(isPrimaryAgreement).toBe(false);
	});
});
