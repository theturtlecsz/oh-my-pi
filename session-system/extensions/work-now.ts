/**
 * work-now.ts — Work Ledger-backed owner workflow, HOME-147 entrypoint.
 *
 * Candidate backend: installed by install.sh --backend work. Never reads or
 * writes Linear; loopback-only per workflow/config.ts. Dormant (warning only)
 * when ~/.config/omp-work/client.json is absent or malformed. The model-facing
 * tool surface is identical to the Linear backend's (one name, one enum, one
 * action set — plan §2); only storage differs.
 */
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { loadBearer, loadWorkConfig, type WorkClientConfig } from "./workflow/config";
import { createWorkflowHost } from "./workflow/host";
import { createWorkBackend } from "./workflow/work";

export { CLOSEOUT_BOUNDARY, STOP_REMINDER_BOUNDARY } from "./workflow/backend";
export { WORKFLOW_SEQUENCE } from "./workflow/host";

export default function workNow(pi: ExtensionAPI) {
	let config: WorkClientConfig | null;
	try {
		config = loadWorkConfig();
	} catch (error) {
		pi.on("session_start", async (_event, ctx) => {
			ctx.ui.notify(`work-now: ${String(error)} — Work Ledger backend dormant`, "warning");
		});
		return;
	}
	if (!config) {
		pi.on("session_start", async (_event, ctx) => {
			ctx.ui.notify("work-now: ~/.config/omp-work/client.json missing — Work Ledger backend dormant", "warning");
		});
		return;
	}
	const cfg = config;
	createWorkflowHost({
		backend: createWorkBackend(cfg, () => loadBearer(cfg)),
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: data => data.backend === "work",
		reviewCheckpointHint:
			"Forward the fresh auditor's report verbatim as the body; the host binds the registered audit receipt.",
	})(pi);
}
