/**
 * Contract: a vibe worker's spawn options carry the pre-expansion model role.
 *
 * `#resolveWorker` expands the bundled worker's role alias (`good` -> `task` ->
 * `@task`, `fast` -> `sonic` -> `@smol`) into concrete patterns, so the role
 * survives only as a separate field forwarded across `ResolvedVibeWorker` ->
 * `VibeRecord` -> `#buildSpawnOptions` -> `runSubprocess`. The executor keys the
 * child's inherited `retry.fallbackChains` entry off it; drop any link in that
 * chain and vibe children silently retry on the `default` role's chain.
 */
import { afterEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import { AsyncJobManager } from "@oh-my-pi/pi-coding-agent/async/job-manager";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { AgentRegistry } from "@oh-my-pi/pi-coding-agent/registry/agent-registry";
import type { ExecutorOptions } from "@oh-my-pi/pi-coding-agent/task/executor";
import * as executorModule from "@oh-my-pi/pi-coding-agent/task/executor";
import type { SingleResult } from "@oh-my-pi/pi-coding-agent/task/types";
import type { ToolSession } from "@oh-my-pi/pi-coding-agent/tools";
import { type VibeCli, VibeSessionRegistry } from "@oh-my-pi/pi-coding-agent/vibe/runtime";

function makeParentSession(settings: Settings): ToolSession {
	return {
		cwd: "/tmp",
		settings,
		asyncJobManager: new AsyncJobManager({ onJobComplete: () => {} }),
		getSessionId: () => "parent-session",
		// No session file: spawn skips lifecycle persistence and stays in-memory.
		getSessionFile: () => null,
		getArtifactsDir: () => null,
		taskDepth: 0,
		enableLsp: false,
	} as unknown as ToolSession;
}

/** Spawn one worker and capture the ExecutorOptions the vibe path hands the executor. */
async function spawnAndCaptureOptions(cli: VibeCli, settings: Settings): Promise<ExecutorOptions> {
	const captured = Promise.withResolvers<ExecutorOptions>();
	vi.spyOn(executorModule, "runSubprocess").mockImplementation(async options => {
		captured.resolve(options);
		return {
			index: 0,
			id: options.id,
			agent: options.agent.name,
			agentSource: "bundled",
			task: options.task,
			exitCode: 0,
			output: "done",
			stderr: "",
			truncated: false,
			durationMs: 1,
			tokens: 0,
			requests: 0,
		} as SingleResult;
	});

	const registry = VibeSessionRegistry.global();
	await registry.spawn(makeParentSession(settings), { cli, prompt: "work" });
	return captured.promise;
}

describe("vibe worker spawn model role", () => {
	afterEach(() => {
		vi.restoreAllMocks();
		VibeSessionRegistry.resetGlobalForTests();
		AgentRegistry.resetGlobalForTests();
	});

	it("forwards the `task` role behind the `good` worker's expanded patterns", async () => {
		const options = await spawnAndCaptureOptions(
			"good",
			Settings.isolated({
				modelRoles: { default: "anthropic/opus", task: "anthropic/sonnet" },
			}),
		);

		expect(options.modelOverride).toEqual(["anthropic/sonnet"]);
		expect(options.modelRole).toBe("task");
	});

	it("forwards the `smol` role behind the `fast` worker's expanded patterns", async () => {
		const options = await spawnAndCaptureOptions(
			"fast",
			Settings.isolated({
				modelRoles: { default: "anthropic/opus", smol: "fast/hy3" },
			}),
		);

		expect(options.modelOverride).toEqual(["fast/hy3"]);
		expect(options.modelRole).toBe("smol");
	});

	it("keeps the role identity when a per-agent model override replaces the alias", async () => {
		// `task.agentModelOverrides` wins over the agent definition, and an explicit
		// selector carries no role — the child must then inherit `default`, not
		// capture the routing of whichever role happens to name the same model.
		const options = await spawnAndCaptureOptions(
			"good",
			Settings.isolated({
				modelRoles: { default: "anthropic/opus", task: "anthropic/sonnet" },
				"task.agentModelOverrides": { task: "openai-codex/sol" },
			}),
		);

		expect(options.modelOverride).toEqual(["openai-codex/sol"]);
		expect(options.modelRole).toBeUndefined();
	});

	it("keeps the throwaway artifacts dir as the keep-alive home until the worker is killed", async () => {
		// OMP-389: a worker with no session file gets a tmpdir `omp-vibe-*` home.
		// That dir is the keep-alive session home the executor writes
		// `<id>.jsonl`/`<id>.md` into, so it must survive the turn settling (a
		// delete here broke park/revive and `agent://`); it is removed only when
		// the worker's lifetime ends.
		const settings = Settings.isolated({ modelRoles: { default: "anthropic/opus", smol: "fast/hy3" } });
		const manager = new AsyncJobManager({ onJobComplete: () => {} });
		const session = { ...makeParentSession(settings), asyncJobManager: manager } as unknown as ToolSession;
		const captured = Promise.withResolvers<ExecutorOptions>();
		vi.spyOn(executorModule, "runSubprocess").mockImplementation(async options => {
			captured.resolve(options);
			return {
				index: 0,
				id: options.id,
				agent: options.agent.name,
				agentSource: "bundled",
				task: options.task,
				exitCode: 0,
				output: "done",
				stderr: "",
				truncated: false,
				durationMs: 1,
				tokens: 0,
				requests: 0,
			} as SingleResult;
		});

		const { id, jobId } = await VibeSessionRegistry.global().spawn(session, { cli: "fast", prompt: "work" });
		const options = await captured.promise;
		expect(options.artifactsDir).toMatch(/omp-vibe-/);
		const artifactsDir = options.artifactsDir!;
		await manager.getJob(jobId)?.promise;
		// The turn settled but the keep-alive home must still be there.
		expect(fs.existsSync(artifactsDir)).toBe(true);

		// Killing the worker ends its lifetime, so the throwaway home goes with it.
		await VibeSessionRegistry.global().kill(session, id);
		expect(fs.existsSync(artifactsDir)).toBe(false);
	});
});
