/** Disposable fixture authority and identity setup, not a replacement CLI host. */
import * as path from "node:path";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { computeAuditTcb } from "../../extensions/workflow/audit-tcb";
import { loadBearer, loadWorkConfig } from "../../extensions/workflow/config";
import { headCommit } from "../../extensions/workflow/git";
import { createWorkBackend } from "../../extensions/workflow/work";
import { runtimeEnvironment } from "../../runtime/run";

const [mode, releaseRoot, stateRoot, repository, predecessorState] = process.argv.slice(2);
if (!releaseRoot || !stateRoot || !repository) throw new Error("mode, release, state and repository required");
if (mode === "environment") {
	console.log(JSON.stringify(runtimeEnvironment(releaseRoot, stateRoot)));
} else if (mode === "setup") {
	// computeAuditTcb seals its loaded source bytes. Prove those bytes equal the
	// selected installation before using this candidate-side setup helper.
	for (const name of ["host.ts", "work.ts", "git.ts", "auditor-runner.ts"]) {
		const candidate = Bun.file(path.join(import.meta.dir, "../../extensions/workflow", name));
		const installed = Bun.file(path.join(releaseRoot, "source/session-system/extensions/workflow", name));
		if (await candidate.text() !== await installed.text()) throw new Error(`setup/installation identity differs: ${name}`);
	}
	const config = loadWorkConfig();
	if (!config) throw new Error("disposable service config missing");
	const backend = createWorkBackend(config, () => loadBearer(config));
	if (!backend.workClient) throw new Error("service client unavailable");
	const baseline = headCommit(repository);
	if (!baseline) throw new Error("fixture baseline missing");
	// The identity routine consumes cwd only; no session/dispatch implementation
	// is supplied here. The test later starts the unmodified installed RPC CLI.
	const identityContext = { cwd: repository } as ExtensionContext;
	const tcb = await computeAuditTcb(identityContext, backend.workClient, specifier =>
		Bun.resolveSync(specifier, path.join(releaseRoot, "source/session-system")),
	);
	const description = "Disposable controller recovery fixture: change result.txt from before to after.";
	const issue = await backend.createIssue({ title: "Installed controller continuation recovery", description });
	let predecessor;
	if (predecessorState !== undefined) {
		if (!["CANCELED", "CANCELLED"].includes(predecessorState)) throw new Error("Unsupported predecessor fixture state");
		const source = await backend.createIssue({ title: "Retained terminal predecessor" });
		const stateResult = await backend.workClient.execute({
			api_version: "work.omp.dev/v1", workspace_id: config.workspaceId,
			operation_id: crypto.randomUUID(), request_id: crypto.randomUUID(), correlation_id: crypto.randomUUID(),
			command: { type: "set_work_state", payload: { work_id: source.id, state: predecessorState } },
		});
		const relationResult = await backend.workClient.execute({
			api_version: "work.omp.dev/v1", workspace_id: config.workspaceId,
			operation_id: crypto.randomUUID(), request_id: crypto.randomUUID(), correlation_id: crypto.randomUUID(),
			command: { type: "put_relation", payload: { relation: {
				workspace_id: config.workspaceId, source_work_id: source.id,
				target_work_id: issue.id, kind: "blocks", active: true,
			} } },
		});
		predecessor = { source, state: predecessorState, stateResult, relationResult };
	}
	// The real installed CLI admits /execute and writes its own session binding.
	console.log(JSON.stringify({ issue, baseline, tcb, predecessor }));
} else {
	throw new Error("unknown setup mode");
}
