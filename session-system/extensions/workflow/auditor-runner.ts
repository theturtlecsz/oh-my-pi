/**
 * workflow/auditor-runner.ts — Native, blocking auditor subprocess runner (OMP-168).
 *
 * Runs the installed `auditor` agent directly via the host's task executor,
 * with no model-transport copy/paste, no agent loop recreation, and no
 * prompt-enforced budget prose.
 */
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
 * 3. Loads effective settings for `ctx.cwd` / `getAgentDir()`.
 */
export async function prepareNativeAuditRunner(ctx: ExtensionContext): Promise<NativeAuditRunner> {
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
