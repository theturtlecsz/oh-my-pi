import { Database } from "bun:sqlite";
import { afterEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { createAutoresearchExtension } from "@oh-my-pi/pi-coding-agent/autoresearch";
import { importManagedResults, parseManagedResults } from "@oh-my-pi/pi-coding-agent/autoresearch/managed-import";
import { buildExperimentState, findBestKeptMetric } from "@oh-my-pi/pi-coding-agent/autoresearch/state";
import {
	AutoresearchStorage,
	closeAllAutoresearchStorages,
	openAutoresearchStorage,
	type SessionRow,
} from "@oh-my-pi/pi-coding-agent/autoresearch/storage";
import type {
	ExtensionAPI,
	ExtensionCommandContext,
	RegisteredCommand,
} from "@oh-my-pi/pi-coding-agent/extensibility/extensions";
import {
	createDashboardController,
	type AutoresearchDashboardHost,
	type AutoresearchDashboardRuntime,
} from "@oh-my-pi/pi-tui/apps/autoresearch-dashboard";
import { findBaselineMetric, isBetter, provenanceLabel } from "@oh-my-pi/pi-tui/apps/autoresearch-data";
import type { ExperimentState } from "@oh-my-pi/pi-tui/tools/autoresearch";
import * as vcs from "@oh-my-pi/pi-natives/vcs";
import { TempDir } from "@oh-my-pi/pi-utils";

const temps: TempDir[] = [];

afterEach(async () => {
	vi.restoreAllMocks();
	delete process.env.OMP_AUTORESEARCH_DB_DIR;
	closeAllAutoresearchStorages();
	await Bun.sleep(0);
	for (const dir of temps.splice(0)) {
		await dir.remove().catch(() => {});
	}
});

function tempDir(): TempDir {
	const dir = TempDir.createSync("@pi-autoresearch-managed-");
	temps.push(dir);
	return dir;
}

function receipt(char: string): string {
	return char.repeat(64);
}

function managedLine(overrides: Record<string, unknown> = {}): string {
	return JSON.stringify({
		format: "omp-managed-result.v1",
		trial_id: "t1",
		campaign_id: "camp",
		receipt_sha256: receipt("a"),
		candidate_digest: "digest",
		verdict: "qualified",
		primary_metric: "score",
		direction: "lower",
		metrics: { score: 10 },
		description: "imported",
		...overrides,
	});
}

function openSession(storage: AutoresearchStorage, direction: "lower" | "higher" = "lower"): SessionRow {
	return storage.openSession({
		name: "speed",
		goal: null,
		primaryMetric: "score",
		metricUnit: "",
		direction,
		preferredCommand: null,
		branch: null,
		baselineCommit: null,
		maxIterations: null,
		scopePaths: [],
		offLimits: [],
		constraints: [],
		secondaryMetrics: [],
	});
}

function expandedText(state: ExperimentState): string {
	const previous = Object.getOwnPropertyDescriptor(process.stdout, "columns");
	Object.defineProperty(process.stdout, "columns", { configurable: true, value: 180 });
	try {
		const dashboard = createDashboardController();
		let text = "";
		const runtime: AutoresearchDashboardRuntime = {
			autoresearchMode: true,
			dashboardExpanded: true,
			state,
			lastRunSummary: null,
			runningExperiment: null,
		};
		dashboard.updateWidget(
			{
				hasUI: true,
				ui: {
					setWidget(_key, content) {
						if (!content) return;
						const widget = content({ requestRender() {} }, {
							fg: (_color: string, value: string) => value,
						} as never);
						text = (widget as unknown as { getText(): string }).getText();
					},
					custom: async () => undefined,
				},
			} as AutoresearchDashboardHost,
			runtime,
		);
		return text;
	} finally {
		if (previous) Object.defineProperty(process.stdout, "columns", previous);
		else Reflect.deleteProperty(process.stdout, "columns");
	}
}

const V1_SCHEMA = `
CREATE TABLE sessions (
	id INTEGER PRIMARY KEY,
	name TEXT NOT NULL,
	goal TEXT,
	primary_metric TEXT NOT NULL,
	metric_unit TEXT NOT NULL DEFAULT '',
	direction TEXT NOT NULL DEFAULT 'lower',
	preferred_command TEXT,
	branch TEXT,
	baseline_commit TEXT,
	current_segment INTEGER NOT NULL DEFAULT 0,
	max_iterations INTEGER,
	scope_paths_json TEXT NOT NULL DEFAULT '[]',
	off_limits_json TEXT NOT NULL DEFAULT '[]',
	constraints_json TEXT NOT NULL DEFAULT '[]',
	secondary_metrics_json TEXT NOT NULL DEFAULT '[]',
	notes TEXT NOT NULL DEFAULT '',
	created_at INTEGER NOT NULL,
	closed_at INTEGER
);
CREATE TABLE runs (
	id INTEGER PRIMARY KEY,
	session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
	segment INTEGER NOT NULL,
	command TEXT NOT NULL,
	started_at INTEGER NOT NULL,
	completed_at INTEGER,
	duration_ms INTEGER,
	exit_code INTEGER,
	timed_out INTEGER NOT NULL DEFAULT 0,
	parsed_primary REAL,
	parsed_metrics_json TEXT,
	parsed_asi_json TEXT,
	pre_run_dirty_paths_json TEXT NOT NULL DEFAULT '[]',
	log_path TEXT NOT NULL,
	status TEXT,
	description TEXT,
	metric REAL,
	metrics_json TEXT,
	asi_json TEXT,
	commit_hash TEXT,
	confidence REAL,
	modified_paths_json TEXT,
	scope_deviations_json TEXT,
	justification TEXT,
	flagged INTEGER NOT NULL DEFAULT 0,
	flagged_reason TEXT,
	logged_at INTEGER,
	abandoned_at INTEGER
);
PRAGMA user_version = 1;
`;

describe("managed result import", () => {
	it("opens a version-1 database, keeps the run, and labels it unmanaged", () => {
		const dir = tempDir();
		const dbPath = dir.join("legacy.db");
		const legacy = new Database(dbPath);
		legacy.exec(V1_SCHEMA);
		legacy.exec(`
			INSERT INTO sessions (name, primary_metric, metric_unit, direction, notes, created_at)
			VALUES ('speed', 'score', '', 'lower', '', 1);
			INSERT INTO runs (
				session_id, segment, command, started_at, log_path, status, description, metric, flagged, logged_at
			) VALUES (1, 0, 'bun run bench', 1, '/tmp/run.log', 'keep', 'baseline', 100, 0, 2);
		`);
		legacy.close();

		const storage = new AutoresearchStorage(dbPath, dir.path());
		try {
			const session = storage.getActiveSession();
			expect(session?.name).toBe("speed");
			const runs = storage.listLoggedRuns(session!.id);
			expect(runs).toHaveLength(1);
			expect(runs[0]?.description).toBe("baseline");
			expect(runs[0]?.metric).toBe(100);
			expect(runs[0]?.status).toBe("keep");
			expect(runs[0]?.managedTrialId).toBeNull();
			expect(runs[0]?.managedReceiptSha256).toBeNull();
			const state = buildExperimentState(session!, runs);
			expect(state.results).toHaveLength(1);
			expect(state.results[0]?.provenance).toBeUndefined();
			expect(provenanceLabel(state.results[0]!)).toBe("unmanaged");
			const rendered = expandedText(state);
			expect(rendered).toContain("unmanaged (legacy)");
			expect(rendered).toContain("unmanaged");
			expect(rendered).not.toContain("imported-unverified");
		} finally {
			storage.close();
		}

		const migrated = new Database(dbPath, { readonly: true });
		const version = migrated.query<{ user_version: number }, []>("PRAGMA user_version").get();
		expect(version?.user_version).toBe(2);
		const columns = migrated.query<{ name: string }, []>("PRAGMA table_info(runs)").all();
		expect(columns.some(column => column.name === "managed_trial_id")).toBe(true);
		expect(columns.some(column => column.name === "managed_receipt_sha256")).toBe(true);
		const kept = migrated
			.query<{ description: string; metric: number }, []>("SELECT description, metric FROM runs")
			.get();
		expect(kept).toEqual({ description: "baseline", metric: 100 });
		migrated.close();
	});

	it("imports three mapped rows, rejects two, marks one incompatible, and skips them on re-import", () => {
		const dir = tempDir();
		const storage = new AutoresearchStorage(dir.join("test.db"), dir.path());
		try {
			const opened = openSession(storage);
			const session = storage.bumpSegment(opened.id);
			expect(session.currentSegment).toBe(1);
			const text = [
				managedLine({
					trial_id: "t-keep",
					receipt_sha256: receipt("a"),
					verdict: "qualified",
					metrics: { score: 11 },
					description: "qualified row",
				}),
				managedLine({
					trial_id: "t-crash",
					receipt_sha256: receipt("B"),
					verdict: "candidate_failed",
					metrics: { score: 22 },
					description: "candidate row",
				}),
				managedLine({
					trial_id: "t-checks",
					receipt_sha256: receipt("c"),
					verdict: "evaluator_failed",
					metrics: { score: 33 },
					description: "evaluator row",
				}),
				"{not json",
				JSON.stringify({ format: "omp-managed-result.v1", trial_id: "missing" }),
				"",
				managedLine({
					trial_id: "t-other",
					receipt_sha256: receipt("d"),
					primary_metric: "latency",
					metrics: { latency: 4 },
					description: "other metric",
				}),
			].join("\n");

			const first = importManagedResults(storage, session.id, text);
			expect(first).toEqual({ imported: 3, rejected: 2, incompatible: 1, skipped: 0 });

			const runs = storage.listLoggedRuns(session.id);
			expect(runs).toHaveLength(3);
			expect(runs.every(run => run.segment === 1)).toBe(true);
			expect(runs.every(run => run.flagged && run.flaggedReason === "imported-unverified")).toBe(true);
			const byTrial = new Map(runs.map(run => [run.managedTrialId, run]));
			expect(byTrial.get("t-keep")?.status).toBe("keep");
			expect(byTrial.get("t-keep")?.metric).toBe(11);
			expect(byTrial.get("t-crash")?.status).toBe("crash");
			expect(byTrial.get("t-crash")?.metric).toBe(22);
			expect(byTrial.get("t-crash")?.managedReceiptSha256).toBe(receipt("B"));
			expect(byTrial.get("t-checks")?.status).toBe("checks_failed");
			expect(byTrial.get("t-checks")?.metric).toBe(33);

			const state = buildExperimentState(storage.getSessionById(session.id)!, runs);
			expect(state.results.map(result => provenanceLabel(result))).toEqual([
				"imported-unverified",
				"imported-unverified",
				"imported-unverified",
			]);
			expect(state.results[0]?.provenance).toEqual({
				mode: "imported",
				trialId: "t-keep",
				receiptSha256: receipt("a"),
			});
			const rendered = expandedText(state);
			expect(rendered).toContain("imported-unverified");
			expect(rendered).not.toContain("unmanaged (legacy)");
			expect(rendered).toMatch(/\bsrc\b/);

			const second = importManagedResults(storage, session.id, text);
			expect(second).toEqual({ imported: 0, rejected: 2, incompatible: 1, skipped: 3 });
			expect(storage.listLoggedRuns(session.id)).toHaveLength(3);
		} finally {
			storage.close();
		}
	});

	it("keeps best and baseline when an imported keep is better", () => {
		const dir = tempDir();
		const storage = new AutoresearchStorage(dir.join("test.db"), dir.path());
		try {
			const session = openSession(storage, "lower");
			const pending = storage.insertRun({
				sessionId: session.id,
				segment: 0,
				command: "bun run bench",
				logPath: "/tmp/run.log",
				preRunDirtyPaths: [],
				startedAt: 1,
			});
			const logged = storage.markRunLogged({
				runId: pending.id,
				status: "keep",
				description: "baseline",
				metric: 100,
				metrics: {},
				asi: null,
				commitHash: "abc123",
				confidence: null,
				modifiedPaths: [],
				scopeDeviations: [],
				justification: null,
				loggedAt: 2,
			});
			expect(logged.managedTrialId).toBeNull();
			expect(logged.managedReceiptSha256).toBeNull();

			const before = buildExperimentState(session, storage.listLoggedRuns(session.id));
			expect(findBaselineMetric(before.results, before.currentSegment)).toBe(100);
			expect(findBestKeptMetric(before.results, before.currentSegment, "lower")).toBe(100);
			expect(before.bestMetric).toBe(100);
			expect(provenanceLabel(before.results[0]!)).toBe("unmanaged");

			const text = managedLine({
				trial_id: "t-better",
				receipt_sha256: receipt("e"),
				verdict: "qualified",
				metrics: { score: 40 },
				description: "better but unverified",
			});
			expect(importManagedResults(storage, session.id, text)).toEqual({
				imported: 1,
				rejected: 0,
				incompatible: 0,
				skipped: 0,
			});

			const after = buildExperimentState(session, storage.listLoggedRuns(session.id));
			expect(findBaselineMetric(after.results, after.currentSegment)).toBe(100);
			expect(findBestKeptMetric(after.results, after.currentSegment, "lower")).toBe(100);
			expect(after.bestMetric).toBe(100);
			const imported = after.results.find(result => result.provenance?.trialId === "t-better");
			expect(imported?.status).toBe("keep");
			expect(imported?.metric).toBe(40);
			expect(imported?.flagged).toBe(true);
			expect(imported && isBetter(imported.metric, 100, "lower")).toBe(true);
			expect(imported && provenanceLabel(imported)).toBe("imported-unverified");
			const rendered = expandedText(after);
			expect(rendered).not.toContain("unmanaged (legacy)");
			expect(rendered).toContain("imported-unverified");
			expect(rendered).toContain("unmanaged");
			expect(rendered).toContain("Best: 100");
			expect(rendered).not.toContain("Best: 40");
		} finally {
			storage.close();
		}
	});

	it("rejects malformed lines and incompatible direction before import", () => {
		const dir = tempDir();
		const storage = new AutoresearchStorage(dir.join("test.db"), dir.path());
		try {
			const session = openSession(storage, "lower");
			const text = [
				managedLine({ format: "other-format", trial_id: "bad-format" }),
				managedLine({ trial_id: "bad-receipt", receipt_sha256: "abcd" }),
				managedLine({ trial_id: "bad-verdict", verdict: "discard" }),
				managedLine({ trial_id: "bad-metric" }).replace('"score":10', '"score":1e309'),
				managedLine({ trial_id: "extra", note: "nope" }),
				managedLine({ trial_id: "wrong-direction", direction: "higher", metrics: { score: 5 } }),
				managedLine({ trial_id: "dup", receipt_sha256: receipt("1") }),
				managedLine({ trial_id: "dup", receipt_sha256: receipt("2"), metrics: { score: 9 } }),
			].join("\n");
			const parsed = parseManagedResults(text, session);
			expect(parsed.rejected).toBe(5);
			expect(parsed.incompatible).toBe(1);
			expect(parsed.accepted.map(line => line.trialId)).toEqual(["dup", "dup"]);
			const imported = importManagedResults(storage, session.id, text);
			expect(imported).toEqual({ imported: 1, rejected: 5, incompatible: 1, skipped: 1 });
			expect(storage.listLoggedRuns(session.id)).toHaveLength(1);
		} finally {
			storage.close();
		}
	});
});

describe("/autoresearch import-managed", () => {
	function harness(cwd: string): {
		command: RegisteredCommand;
		ctx: ExtensionCommandContext;
		notifications: Array<{ message: string; type: string | undefined }>;
	} {
		const notifications: Array<{ message: string; type: string | undefined }> = [];
		let command: RegisteredCommand | undefined;
		const api = {
			appendEntry(): void {},
			on(): void {},
			registerCommand(name: string, options: Omit<RegisteredCommand, "name">): void {
				command = { name, ...options };
			},
			registerShortcut(): void {},
			registerTool(): void {},
			getActiveTools(): string[] {
				return [];
			},
			setActiveTools: async (): Promise<void> => {},
			sendUserMessage(): void {},
			sendMessage(): void {},
		} as unknown as ExtensionAPI;
		createAutoresearchExtension(api);
		if (!command) throw new Error("autoresearch command was not registered");
		const ctx = {
			cwd,
			hasUI: false,
			sessionManager: {
				getBranch: () => [],
				getSessionId: () => "session-1",
			},
			ui: {
				notify(message: string, type?: string): void {
					notifications.push({ message, type });
				},
				setWidget(): void {},
			},
		} as unknown as ExtensionCommandContext;
		return { command, ctx, notifications };
	}

	it("notifies imported, rejected, incompatible, and skipped counts", async () => {
		const dir = tempDir();
		const dbDir = tempDir();
		process.env.OMP_AUTORESEARCH_DB_DIR = dbDir.path();
		vi.spyOn(vcs, "repo").mockReturnValue(null);
		const storage = await openAutoresearchStorage(dir.path());
		const session = openSession(storage);
		const file = path.join(dir.path(), "results.jsonl");
		fs.writeFileSync(file, `${managedLine({ trial_id: "from-file", metrics: { score: 7 } })}\n`);
		const { command, ctx, notifications } = harness(dir.path());

		await command.handler(`import-managed ${file}`, ctx);
		expect(notifications.at(-1)).toEqual({
			message: "imported 1, rejected 0, incompatible 0, skipped 0",
			type: "info",
		});
		const imported = storage.listLoggedRuns(session.id);
		expect(imported).toHaveLength(1);
		expect(imported[0]?.managedTrialId).toBe("from-file");
		expect(imported[0]?.flaggedReason).toBe("imported-unverified");

		await command.handler("import-managed results.jsonl", ctx);
		expect(notifications.at(-1)).toEqual({
			message: "imported 0, rejected 0, incompatible 0, skipped 1",
			type: "info",
		});

		await command.handler("import-managed", ctx);
		expect(notifications.at(-1)?.message).toBe("import-managed requires a results file.");
		await command.handler("import-managed missing.jsonl", ctx);
		expect(notifications.at(-1)?.message).toContain("Failed to read managed results");
		expect(command.getArgumentCompletions?.("imp")?.some(item => item.value === "import-managed")).toBe(true);
	});

	it("reports a missing session without importing", async () => {
		const dir = tempDir();
		const dbDir = tempDir();
		process.env.OMP_AUTORESEARCH_DB_DIR = dbDir.path();
		vi.spyOn(vcs, "repo").mockReturnValue(null);
		fs.writeFileSync(path.join(dir.path(), "results.jsonl"), `${managedLine()}\n`);
		const { command, ctx, notifications } = harness(dir.path());
		await command.handler("import-managed results.jsonl", ctx);
		expect(notifications.at(-1)?.message).toBe("No active autoresearch session.");
		expect(fs.existsSync(dbDir.path()) ? fs.readdirSync(dbDir.path()) : []).toEqual([]);
	});
});
