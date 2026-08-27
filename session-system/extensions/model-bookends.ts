/**
 * model-bookends — Fable bookends for omp (HOME-131, OMP-168).
 *
 * /intake: resolve the `intake` model role, switch the session to it at :high
 * effort, then forward to the native `/skill:intake` dispatcher — no manual
 * /model switch.
 *
 * Note (OMP-168): /summary close-attempt audits are now executed directly via
 * the host's native `work action:"run_audit"` runner; task-tool interception
 * is completely removed.
 */
import { ThinkingLevel } from "@oh-my-pi/pi-agent-core";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

/** Effort pinned by HOME-131: /intake always runs Fable at :high. */
export const INTAKE_THINKING_LEVEL = ThinkingLevel.High;

export default function modelBookends(pi: ExtensionAPI) {
	// Owner session only; fail closed on hosts predating ctx.taskDepth (same rule as work-now).
	const ownerSession = (ctx: { taskDepth?: number } | undefined): boolean => ctx?.taskDepth === 0;

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

	pi.on("input", async (event, ctx) => {
		if (!ownerSession(ctx) || event.source === "extension") return undefined;
		const intake = /^\s*\/(?:skill:)?intake\b(.*)$/s.exec(event.text);
		if (intake) {
			// HOME-131: /intake runs on Fable-high or not at all — never forward on the wrong model.
			if (!(await switchToIntake(ctx))) return { handled: true };
			return { text: `/skill:intake${intake[1]}` };
		}
		return undefined;
	});
}
