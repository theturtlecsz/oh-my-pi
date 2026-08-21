/**
 * model-bookends — Fable bookends for omp (HOME-131, OMP-47).
 *
 * /intake: resolve the `intake` model role, switch the session to it at :high
 * effort, then forward to the native `/skill:intake` dispatcher — no manual
 * /model switch.
 *
 * /summary: the audit gate is now LEDGER-OWNED (OMP-47). This extension keeps
 * only task-tool interception and WorkService transport:
 *   1. Before a batch containing exactly one `agent:"auditor"` task runs, it
 *      reserves a bounded launch with the task's EXACT byte hash — a mismatch
 *      against the sealed manifest, a missing slot, or any other gate failure
 *      blocks the spawn with the service's typed refusal (no slot burned on a
 *      task mismatch).
 *   2. On the task's tool_result it settles the launch with the UNTOUCHED
 *      transport payload. Normalization, verdict parsing, budget accounting,
 *      drift detection, and audit-receipt minting all live in WorkService.
 * There is no process-global bridge, no report validation here, and no
 * prompt-enforced budget prose — the service refuses what must be refused.
 */
import { ThinkingLevel } from "@oh-my-pi/pi-agent-core";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { sha256Hex } from "@oh-my-pi/pi-work-client";
import auditContract from "./model-bookends-audit.md" with { type: "text" };
import type { WorkflowBackend } from "./workflow/backend";
import { deliverCheckpoint } from "./workflow/checkpoint-delivery";
import { loadBearer, loadWorkConfig } from "./workflow/config";
import { createWorkBackend } from "./workflow/work";

/** Effort pinned by HOME-131: /intake always runs Fable at :high. */
export const INTAKE_THINKING_LEVEL = ThinkingLevel.High;

export const AUDIT_CONTRACT = auditContract.trim();

/**
 * The UNTOUCHED transport payload from a task tool result: prefer the host's
 * `details.results[0].output` verbatim (whatever shape it is), else the raw
 * joined text content. `undefined` = no payload reached us at all.
 */
export function transportPayload(details: unknown, contentText: string): unknown {
	const output = (details as { results?: Array<{ output?: unknown }> } | undefined)?.results?.[0]?.output;
	if (output !== undefined && output !== null) return output;
	return contentText.trim() ? contentText : undefined;
}

interface AuditGate {
	armed: boolean;
	contractInjected: boolean;
	/** The reserved in-flight launch, if any: settle on its tool_result. */
	inflight?: { toolCallId: string; launchId: string; key: string };
}

const freshGate = (): AuditGate => ({ armed: false, contractInjected: false });

export default function modelBookends(pi: ExtensionAPI) {
	// Owner session only; fail closed on hosts predating ctx.taskDepth (same rule as work-now).
	const ownerSession = (ctx: { taskDepth?: number } | undefined): boolean => ctx?.taskDepth === 0;

	let gate = freshGate();

	// Own loopback transport (OMP-47): the loader cache-busts module graphs per
	// extension, so this instance is separate from work-now's — the service is
	// the shared authority, and the shared pending-ops dir dedupes intents.
	let backend: WorkflowBackend | null | undefined;
	function service(): WorkflowBackend | null {
		if (backend !== undefined) return backend;
		try {
			const config = loadWorkConfig();
			backend = config ? createWorkBackend(config, () => loadBearer(config)) : null;
		} catch {
			backend = null;
		}
		return backend;
	}

	/** Switch to the intake role at pinned effort; false = switch impossible (fail closed). */
	async function switchToIntake(ctx: ExtensionContext): Promise<boolean> {
		const model = ctx.models.resolve("@intake");
		if (!model) {
			ctx.ui.notify("model-bookends: /intake refused — could not resolve @intake; fix modelRoles.intake and retry", "error");
			return false;
		}
		if (!(await pi.setModel(model))) {
			ctx.ui.notify(`model-bookends: /intake refused — no credential for ${model.provider}/${model.id}; log in and retry`, "error");
			return false;
		}
		pi.setThinkingLevel(INTAKE_THINKING_LEVEL);
		return true;
	}

	pi.on("session_start", async () => {
		gate = freshGate();
	});
	pi.on("session_switch", async () => {
		gate = freshGate(); // a fresh /summary means a fresh attempt — never carry interception across transcripts
	});

	pi.on("input", async (event, ctx) => {
		if (!ownerSession(ctx) || event.source === "extension") return undefined;
		const intake = /^\s*\/(?:skill:)?intake\b(.*)$/s.exec(event.text);
		if (intake) {
			// HOME-131: /intake runs on Fable-high or not at all — never forward on the wrong model.
			if (!(await switchToIntake(ctx))) return { handled: true };
			return { text: `/skill:intake${intake[1]}` };
		}
		if (/^\s*\/(?:skill:)?summary\b/.test(event.text)) {
			gate = { ...freshGate(), armed: true };
		}
		return undefined;
	});

	// Structured skill invocation (host-composed /skill:summary) also arms the gate.
	pi.on("message_start", async (event, ctx) => {
		if (!ownerSession(ctx) || gate.armed) return;
		const m = event.message as { role?: string; customType?: string; attribution?: string; details?: { name?: string } };
		if (m.role === "custom" && m.customType === "skill-prompt" && m.attribution === "user" && m.details?.name === "summary") {
			gate = { ...freshGate(), armed: true };
		}
	});

	pi.on("before_agent_start", async () => {
		if (!gate.armed || gate.contractInjected) return undefined;
		gate.contractInjected = true;
		return { message: { customType: "audit-contract", content: AUDIT_CONTRACT } };
	});

	pi.on("tool_call", async (event, ctx) => {
		if (!ownerSession(ctx) || !gate.armed || event.toolName !== "task") return undefined;
		const input = event.input as { tasks?: Array<{ agent?: string; task?: unknown; outputSchema?: unknown }> };
		if (!Array.isArray(input.tasks)) return undefined;
		const auditors = input.tasks.filter(task => task?.agent === "auditor");
		if (auditors.length === 0) return undefined;
		if (input.tasks.length !== 1 || auditors.length !== 1) {
			return { block: true, reason: "Audit gate: the auditor must be the only task in its batch." };
		}
		const [auditor] = auditors;
		if (typeof auditor.task !== "string") {
			return { block: true, reason: "Audit gate: the auditor task must be the sealed task body from work get_work, as one string." };
		}
		// HOME-137: schema serialization mangles the plain-text report — the
		// auditor returns canonical headed text, never schema output.
		if (auditor.outputSchema !== undefined && auditor.outputSchema !== null) {
			return { block: true, reason: "Audit gate: never pass outputSchema to the auditor — it must return the canonical plain headed-text report." };
		}
		// Fail closed on an unresolvable audit role: the auditor's independence is
		// its fresh blocking context plus the service-bound receipt.
		if (!ctx.models.resolve("@audit")) {
			return { block: true, reason: "Audit gate: could not resolve @audit — fix modelRoles.audit and retry." };
		}
		const svc = service();
		if (!svc) {
			return { block: true, reason: "Audit gate: the Work Ledger backend is dormant (~/.config/omp-work/client.json) — no launch can be reserved." };
		}
		try {
			const now = await svc.currentNow();
			if (!now) {
				return { block: true, reason: "Audit gate: no NOW is selected — the auditor audits the NOW work item's sealed task." };
			}
			// EXACT bytes, no trim, no normalization: any transformed-equivalent
			// task must be refused before spawn — with zero slot burn.
			const outcome = await svc.reserveAuditorLaunch(now.key, sha256Hex(auditor.task), event.toolCallId);
			if (outcome.status === "refused" || !outcome.launchId) {
				return { block: true, reason: `Audit gate refused by the ledger:\n${outcome.event.renderedText}` };
			}
			gate.inflight = { toolCallId: event.toolCallId, launchId: outcome.launchId, key: now.key };
			return undefined;
		} catch (error) {
			return { block: true, reason: `Audit gate: launch reservation failed (${String(error)}) — nothing was reserved; fix the service and retry.` };
		}
	});

	pi.on("tool_result", async (event, ctx) => {
		if (!ownerSession(ctx) || !gate.inflight || event.toolCallId !== gate.inflight.toolCallId) return undefined;
		const { key, launchId } = gate.inflight;
		gate.inflight = undefined;
		const contentText = event.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
		// Fail closed: a host-reported error result NEVER reaches normalization —
		// error text must not be parseable as a report. It burns a typed
		// transport_failed launch instead (bounded budget, OMP-47).
		const payload = event.isError ? undefined : transportPayload(event.details, contentText);
		const svc = service();
		if (!svc) return undefined; // reservation existed, so this is unreachable in practice
		try {
			const outcome = await svc.settleAuditorLaunch(key, launchId, payload === undefined ? { failed: true } : { payload });
			if (outcome.event.requiresDelivery) {
				try {
					await deliverCheckpoint(pi, svc, outcome.event);
				} catch {
					/* the delivery debt is recorded server-side; recovery runs at the next owner session start */
				}
			}
			// The typed outcome reaches the MODEL in-band — never a side channel only.
			return { content: [...event.content, { type: "text", text: `Audit gate — launch settled by the ledger:\n${outcome.event.renderedText}` }] };
		} catch (error) {
			return {
				content: [
					...event.content,
					{ type: "text", text: `Audit gate: settle failed (${String(error)}). The launch is still reserved server-side; call work get_work and follow the attempt's state.` },
				],
			};
		}
	});
}
