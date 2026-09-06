/**
 * workflow/auditor-runner.ts — Native, blocking auditor subprocess runner (OMP-168).
 *
 * Runs the installed `auditor` agent directly via the host's task executor,
 * with no model-transport copy/paste, no agent loop recreation, and no
 * prompt-enforced budget prose.
 */
import { completeSimple } from "@oh-my-pi/pi-ai";
import { getAgentDir, Settings, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { discoverAgents, getAgent } from "@oh-my-pi/pi-coding-agent/task";
import { runSubprocess } from "@oh-my-pi/pi-coding-agent/task/executor";

export interface NativeAuditRunResult {
	started: boolean;
	payload?: string;
	error?: string;
}

export type NativeAuditRunner = (
	taskBody: string,
	attemptId: string,
	signal?: AbortSignal,
) => Promise<NativeAuditRunResult>;

/**
 * Prepares a native auditor runner for the current extension context.
 *
 * Fails before a ledger launch reservation unless all preconditions exist:
 * 1. Discovers the installed `auditor` agent definition.
 * 2. Resolves the `@audit` role through `ctx.models`.
 * 3. Verifies provider credentials and live transport for the audit model
 *    (OMP-251) — a minimal probe completion must not error, so transport
 *    failures surface here instead of burning a reserved launch.
 * 4. Loads effective settings for `ctx.cwd` / `getAgentDir()`.
 */
export async function prepareNativeAuditRunner(ctx: ExtensionContext, signal?: AbortSignal): Promise<NativeAuditRunner> {
	const discovery = await discoverAgents(ctx.cwd);
	const agent = getAgent(discovery.agents, "auditor");
	if (!agent) {
		throw new Error('Installed "auditor" agent definition not found');
	}
	if (!agent.output) {
		throw new Error('Installed "auditor" agent definition is missing required output schema');
	}

	const auditModel = ctx.models.resolve("@audit");
	if (!auditModel) {
		throw new Error("Could not resolve @audit role — fix modelRoles.audit and retry");
	}

	// OMP-251: auditor transport preflight. Launch reservations are budgeted
	// (3 per attempt); prove credentials + endpoint connectivity BEFORE the
	// caller reserves one, so auth/network outages deny instead of burning
	// the audit budget with transport_failed settlements.
	const auditApiKey = await ctx.modelRegistry.getApiKey(auditModel, undefined, { signal });
	if (auditApiKey === undefined) {
		throw new Error(
			`No provider credentials configured for @audit model ${auditModel.provider}/${auditModel.id} — authenticate the provider and retry`,
		);
	}
	// pi-ai surfaces provider failures IN-BAND (stopReason "error"/"aborted" +
	// errorMessage, content empty) — completeSimple normally does not throw,
	// but synchronous dispatch/configuration failures still reject: qualify
	// those with the audit model too, keeping cancellation errors untouched.
	const probe = await completeSimple(
		auditModel,
		{ messages: [{ role: "user", content: "Transport preflight. Reply with the single word OK.", timestamp: Date.now() }] },
		{
			apiKey: auditApiKey,
			maxTokens: 256,
			temperature: 0,
			disableReasoning: true,
			signal,
		},
	).catch((error: unknown) => {
		if (signal?.aborted) throw error;
		throw new Error(
			`@audit transport preflight failed for ${auditModel.provider}/${auditModel.id}: ${error instanceof Error ? error.message : String(error)}`,
		);
	});
	if (probe.stopReason === "error" || probe.stopReason === "aborted") {
		throw new Error(
			`@audit transport preflight ${probe.stopReason} for ${auditModel.provider}/${auditModel.id}: ${probe.errorMessage || "provider returned no detail"}`,
		);
	}

	const settings = await Settings.loadReadOnly({
		cwd: ctx.cwd,
		agentDir: getAgentDir(),
	});

	return async (
		taskBody: string,
		attemptId: string,
		signal?: AbortSignal,
	): Promise<NativeAuditRunResult> => {
		let started = false;
		try {
			const result = await runSubprocess({
				index: 0,
				cwd: ctx.cwd,
				agent,
				task: taskBody,
				modelOverride: agent.model,
				modelRegistry: ctx.modelRegistry,
				authStorage: ctx.modelRegistry?.authStorage,
				getApiKey: ctx.modelRegistry?.resolver
					? requestModel => ctx.modelRegistry.resolver(requestModel, attemptId)
					: undefined,
				modelRole: "audit",
				outputSchema: agent.output,
				outputSchemaSource: "agent",
				outputSchemaMode: "strict",
				taskDepth: ctx.taskDepth,
				restrictToolNames: true,
				enableMCP: false,
				enableIrc: false,
				enableLsp: true,
				keepAlive: false,
				id: attemptId,
				signal,
				settings,
			});

			started = Boolean(result.requests && result.requests > 0);
			const payload =
				typeof result.output === "string" && result.output.trim().length > 0
					? result.output
					: undefined;

			return {
				started,
				payload,
				error: result.error || result.stderr || undefined,
			};
		} catch (error) {
			return {
				started,
				error: error instanceof Error ? error.message : String(error),
			};
		}
	};
}
