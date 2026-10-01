import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { TextContent } from "@oh-my-pi/pi-ai";
import { createSessionRuntime } from "@oh-my-pi/pi-coding-agent/autoresearch/state";
import {
	type AutoresearchStorage,
	closeAllAutoresearchStorages,
	openAutoresearchStorage,
	type SessionRow,
} from "@oh-my-pi/pi-coding-agent/autoresearch/storage";
import { createInitExperimentTool } from "@oh-my-pi/pi-coding-agent/autoresearch/tools/init-experiment";
import { createLogExperimentTool } from "@oh-my-pi/pi-coding-agent/autoresearch/tools/log-experiment";
import type { LogDetails } from "@oh-my-pi/pi-tui/tools/autoresearch";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import { $ } from "bun";

const tempDirs: string[] = [];

afterEach(async () => {
	delete process.env.OMP_AUTORESEARCH_DB_DIR;
	closeAllAutoresearchStorages();
	await Bun.sleep(0);
	for (const dir of tempDirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

function makeTemp(prefix: string): string {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

function textOf(content: Array<{ type: string; text?: string }>): string {
	const block = content.find((part): part is TextContent => part.type === "text");
	if (!block) throw new Error("expected a text tool content block");
	return block.text;
}

function seedCompletedRun(storage: AutoresearchStorage, session: SessionRow, parsedPrimary: number | null): void {
	const now = Date.now();
	const run = storage.insertRun({
		sessionId: session.id,
		segment: session.currentSegment,
		command: "bash autoresearch.sh",
		startedAt: now,
		logPath: "",
		preRunDirtyPaths: [],
	});
	storage.markRunCompleted({
		runId: run.id,
		completedAt: now + 1,
		durationMs: 1,
		exitCode: 0,
		timedOut: false,
		parsedPrimary,
		parsedMetrics: parsedPrimary === null ? null : { runtime_ms: parsedPrimary },
		parsedAsi: null,
	});
}

async function setup(parsedPrimary: number | null) {
	const dir = makeTemp("autoresearch-harness-metric-");
	const dbDir = makeTemp("autoresearch-harness-metric-db-");
	process.env.OMP_AUTORESEARCH_DB_DIR = dbDir;
	await Bun.write(path.join(dir, "README.md"), "# baseline\n");
	await Bun.write(path.join(dir, "autoresearch.sh"), "#!/usr/bin/env bash\necho METRIC runtime_ms=1\n");
	await $`git init --initial-branch=main && git config core.autocrlf false && git config core.fsmonitor false && git config user.email tester@example.com && git config user.name Tester && git add -A && git commit -m baseline`
		.cwd(dir)
		.quiet();
	const runtime = createSessionRuntime();
	const pi = {
		appendEntry() {},
		exec: async () => ({ code: 0, stdout: "", stderr: "" }),
		getActiveTools: () => [] as string[],
		setActiveTools: async () => {},
	} as unknown as ExtensionAPI;
	const init = createInitExperimentTool({
		dashboard: { clear() {}, requestRender() {}, showOverlay: async () => {}, updateWidget() {} },
		getRuntime: () => runtime,
		pi,
	});
	await init.execute("i", { name: "speed", primary_metric: "runtime_ms", metric_unit: "ms" }, undefined, undefined, {
		cwd: dir,
		hasUI: false,
	} as ExtensionContext);
	const storage = await openAutoresearchStorage(dir);
	const session = storage.getActiveSession();
	if (!session) throw new Error("expected an active session");
	seedCompletedRun(storage, session, parsedPrimary);
	const log = createLogExperimentTool({
		dashboard: { clear() {}, requestRender() {}, showOverlay: async () => {}, updateWidget() {} },
		getRuntime: () => runtime,
		pi,
	});
	return { dir, storage, session, log };
}

describe("log_experiment harness metric", () => {
	it("stores the harness-parsed primary and warns when the agent metric differs", async () => {
		const { dir, storage, session, log } = await setup(42);
		const result = await log.execute(
			"l",
			{ metric: 40, status: "discard", description: "agent disagrees" },
			undefined,
			undefined,
			{ cwd: dir, hasUI: false } as ExtensionContext,
		);
		const details = result.details as LogDetails;
		expect(details.experiment.metric).toBe(42);
		expect(textOf(result.content)).toContain("Logged metric 40 ignored; harness-parsed 42 recorded.");
		const logged = storage.listLoggedRuns(session.id);
		expect(logged).toHaveLength(1);
		expect(logged[0]?.metric).toBe(42);
	});

	it("stores the agent metric when the harness parsed no primary", async () => {
		const { dir, storage, session, log } = await setup(null);
		const result = await log.execute(
			"l",
			{ metric: 40, status: "discard", description: "agent only" },
			undefined,
			undefined,
			{ cwd: dir, hasUI: false } as ExtensionContext,
		);
		const details = result.details as LogDetails;
		expect(details.experiment.metric).toBe(40);
		expect(textOf(result.content)).not.toContain("harness-parsed");
		const logged = storage.listLoggedRuns(session.id);
		expect(logged).toHaveLength(1);
		expect(logged[0]?.metric).toBe(40);
	});
});
