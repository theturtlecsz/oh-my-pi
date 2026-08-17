/**
 * linear-now.ts — Linear-backed owner workflow, HOME-147 entrypoint.
 *
 * All behavior lives in workflow/: linear.ts is the Linear storage adapter,
 * host.ts the backend-agnostic workflow host. This file only wires the two
 * together and re-exports the symbols tests pin. Installed by install.sh
 * (--backend linear, the default until HOME-148). The model-facing tool
 * surface is identical to the Work backend's (plan §2).
 */
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { commitSessionWork } from "./workflow/git";
import { createWorkflowHost } from "./workflow/host";
import { createLinearBackend } from "./workflow/linear";

export { CLOSEOUT_BOUNDARY, STOP_REMINDER_BOUNDARY } from "./workflow/backend";
export { commitSessionWork, findSecrets, parsePorcelain } from "./workflow/git";
export { WORKFLOW_SEQUENCE } from "./workflow/host";

export default function linearNow(pi: ExtensionAPI) {
	createWorkflowHost({
		backend: createLinearBackend({}),
		teamNoun: "team HOME",
		entryType: "linear-now",
		acceptEntry: data => data.backend === "linear" || (data.backend === undefined && (data.team ?? "HOME") === "HOME"),
		commitAfterClose: (ui, cwd, key) => commitSessionWork(ui, cwd, key),
		reviewCheckpointHint:
			"The body carries the session review: completed work, verification evidence, remaining risks, and exact resume state.",
	})(pi);
}
