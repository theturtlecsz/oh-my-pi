import { expect, test, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { z } from "zod";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { resolveLocalUrlToPath } from "@oh-my-pi/pi-coding-agent/internal-urls";
import { sha256Hex } from "@oh-my-pi/pi-work-client";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { resetConfirmations } from "../extensions/workflow/confirm";
import { createWorkflowHost, readNsiRouteMode } from "../extensions/workflow/host";

interface WorkTool {
	name: string;
	execute: (
		id: string,
		params: Record<string, unknown>,
		signal: unknown,
		update: unknown,
		ctx: ExtensionContext,
	) => Promise<{ content: { text: string }[] }>;
}

test("fresh hosts restore current prose intake after typed refusal; previews and refusals write nothing", async () => {
	const root = path.resolve(import.meta.dir, "../..");
	const temp = await fs.mkdtemp(path.join(root, ".nsi6-host-"));
	const previous = process.env.XDG_CONFIG_HOME;
	const createIssue = vi.fn(async () => ({ id: "new", key: "HOME-10", title: "Ship gate" }));
	const setNowRemote = vi.fn(async () => undefined);
	const assess = vi.fn(async () => ({
		ready_for_ratification: true,
		issue_count: 0,
		blocking_question_count: 0,
		warning_count: 0,
		questions: [],
		warnings: [],
		decision_posture: "ready",
		required_gates: [],
	}));
	const backend = {
		cacheFile: path.join(temp, "cache.json"),
		queueNoun: "decision queue",
		markerFile: ".work-project",
		teamNoun: "ledger",
		reviewKind: "review",
		createIssue,
		setNowRemote,
		assessBoundedIntake: assess,
	} as unknown as WorkflowBackend;
	const description = "# Ship gate\n\n## Acceptance criteria\n- check passes\n";
	const draft = {
		archetype: "small_code_change",
		source: { text: description, sha256: sha256Hex(description), spans: [] },
		goal: { id: "goal", statement: "Ship gate" },
		acceptance_criteria: [{ id: "ac-1", statement: "check passes", observable_outcome: "check passes" }],
	};
	const observations: Record<string, unknown>[] = [];
	try {
		process.env.XDG_CONFIG_HOME = temp;
		const service = await Bun.file(path.join(root, "docs/nsi6/service.json")).json();
		for (const mode of ["current", "typed", "current"] as const) {
			const state = mode === "typed" ? service.transitions[0] : service.transitions[1];
			await Bun.write(path.join(temp, "omp/work-ledger/nsi-route.json"), JSON.stringify(state));
			expect(readNsiRouteMode()).toBe(mode);
			resetConfirmations({ resetShared: true });
			let work: WorkTool | undefined;
			const listeners: Record<string, ((event: unknown, ctx: ExtensionContext) => unknown)[]> = {};
			const pi = {
				logger: { warn() {}, error() {}, debug() {}, info() {} },
				zod: z,
				registerTool(spec: WorkTool) {
					if (spec.name === "work") work = spec;
				},
				registerMessageRenderer() {},
				registerCommand() {},
				registerFlag() {},
				sendMessage() {},
				appendEntry() {},
				getSessionId: () => "rehearsal",
				on(event: string, handler: (event: unknown, ctx: ExtensionContext) => unknown) {
					(listeners[event] ??= []).push(handler);
				},
			} as unknown as ExtensionAPI;
			createWorkflowHost({ backend, teamNoun: "ledger", entryType: "work-now", acceptEntry: () => true })(pi);
			const localProtocolOptions = {
				getArtifactsDir: () => path.join(temp, "artifacts"),
				getSessionId: () => "rehearsal",
			};
			await Bun.write(resolveLocalUrlToPath("local://intake-rehearsal.md", localProtocolOptions), description);
			const ctx = {
				taskDepth: 0,
				localProtocolOptions,
				sessionManager: { getSessionId: () => "rehearsal" },
				ui: { notify() {}, setStatus() {} },
			} as unknown as ExtensionContext;
			for (const handler of listeners.message_start ?? [])
				await handler(
					{
						message: {
							role: "custom",
							customType: "skill-prompt",
							attribution: "user",
							details: { name: "intake" },
						},
					},
					ctx,
				);
			for (const handler of listeners.message_end ?? [])
				await handler(
					{
						message: {
							role: "assistant",
							content: [
								{
									type: "text",
									text: "## Figured out myself\n- fact\n## Asking you\n- question\n## Leaving for later\n- later",
								},
							],
						},
					},
					ctx,
				);
			if (!work) throw new Error("work tool missing");
			const tool = work;
			const execute = async (params: Record<string, unknown>) => {
				const result = await tool.execute("rehearsal", params, undefined, undefined, ctx);
				return result.content.map(item => item.text).join("\n");
			};
			const prose = { action: "create_work", title: "Ship gate", description, project: "TestProject" };
			const before = { issues: createIssue.mock.calls.length, now: setNowRemote.mock.calls.length };
			const proseResult = await execute(prose);
			if (mode === "typed") expect(proseResult).toContain("intake_typed_required");
			else expect(proseResult).toContain("CONFIRM REQUIRED");
			const params = mode === "typed" ? { ...prose, intake: draft } : prose;
			const preview = mode === "typed" ? await execute(params) : proseResult;
			expect(preview).toContain("CONFIRM REQUIRED");
			expect({ issues: createIssue.mock.calls.length, now: setNowRemote.mock.calls.length }).toEqual(before);
			const confirmation = /confirmation_id: (\S+)/.exec(preview)?.[1];
			expect(confirmation).toBeDefined();
			const confirmed = await execute({ ...params, confirm: true, confirmation_id: confirmation });
			expect(confirmed).toContain("created HOME-10 + NOW");
			const after = { issues: createIssue.mock.calls.length, now: setNowRemote.mock.calls.length };
			expect(after).toEqual({ issues: before.issues + 1, now: before.now + 1 });
			observations.push({
				mode,
				proseResult,
				preview,
				confirmed,
				before,
				after,
				preview_write_delta: 0,
				confirmed_write_delta: { issues: after.issues - before.issues, now: after.now - before.now },
			});
		}
		const out = process.env.NSI6_HOST_EVIDENCE_OUT;
		if (out)
			await Bun.write(
				out,
				`${JSON.stringify(
					{
						verified: true,
						owner_approval: false,
						scope: "fresh real workflow hosts; mocked backend assessment and writes; copied temporary service states",
						observations,
					},
					null,
					2,
				)}\n`,
			);
	} finally {
		if (previous === undefined) delete process.env.XDG_CONFIG_HOME;
		else process.env.XDG_CONFIG_HOME = previous;
		resetConfirmations({ resetShared: true });
		await fs.rm(temp, { recursive: true, force: true });
	}
});
