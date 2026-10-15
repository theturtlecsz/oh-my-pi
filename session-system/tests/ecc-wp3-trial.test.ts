import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { evaluate, initTrial, main, taskTemplate, trialSkeleton } from "../../scripts/ecc-workflow-trial.ts";

type Rec = Record<string, unknown>;

const dirs: string[] = [];
afterEach(async () => {
	for (const d of dirs.splice(0)) await fs.rm(d, { recursive: true, force: true });
});

async function tempRoot(): Promise<string> {
	const d = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-wp3-trial-"));
	dirs.push(d);
	return d;
}

const MODELS = { default: "anthropic/claude-sonnet-5-5", smol: "anthropic/claude-haiku-5-5" };
const CONFIG = {
	models: MODELS,
	effort: { default: "medium" },
	fallback: { models: [] },
	advisor: "off",
	assistance: { audit: "required" },
};

function usage(totalTokens: number, cost: number, premium = 0): Rec {
	return {
		input: totalTokens,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens,
		premiumRequests: premium,
		cost: { input: cost, output: 0, cacheRead: 0, cacheWrite: 0, total: cost },
	};
}

interface Turn {
	usage?: Rec;
	model?: string;
	response?: string;
}

/** Write a native-shaped session JSONL export; returns its path. */
async function writeSession(root: string, id: string, turns: Turn[], opts: { parent?: string; childUsage?: Rec } = {}): Promise<string> {
	const lines: Rec[] = [{ type: "session", version: 3, id, timestamp: "2026-10-01T00:00:00Z", cwd: "/w", parentSession: opts.parent }];
	let prev: string | null = null;
	const push = (entryId: string, message: Rec) => {
		lines.push({ type: "message", id: entryId, parentId: prev, timestamp: "2026-10-01T00:00:01Z", message });
		prev = entryId;
	};
	for (const [i, t] of turns.entries()) {
		const [provider, model] = (t.model ?? MODELS.default).split("/");
		push(`${id}-a${i}`, {
			role: "assistant",
			content: [],
			api: "anthropic-messages",
			provider,
			model,
			responseId: t.response,
			usage: t.usage,
			stopReason: "stop",
			timestamp: 1,
		});
	}
	if (opts.childUsage) {
		push(`${id}-tr`, { role: "toolResult", toolCallId: "c", toolName: "task", content: [], isError: false, details: { usage: opts.childUsage }, timestamp: 1 });
	}
	const file = path.join(root, "evidence", "sessions", `${id}.jsonl`);
	await Bun.write(file, `${lines.map(l => JSON.stringify(l)).join("\n")}\n`);
	return file;
}

async function evidence(root: string, name: string): Promise<string> {
	const file = path.join(root, "evidence", "tasks", name);
	await Bun.write(file, "evidence\n");
	return file;
}

interface TaskSpec {
	key: string;
	repo: string;
	domains: string[];
	status?: string;
	exports?: Rec[];
	patch?: Rec;
}

async function makeTask(root: string, spec: TaskSpec): Promise<Rec> {
	const status = spec.status ?? "accepted";
	const ev = (n: string) => evidence(root, `${spec.key}-${n}.md`);
	const task = taskTemplate();
	Object.assign(task, {
		work_key: spec.key,
		title: `task ${spec.key}`,
		repo: spec.repo,
		domains: spec.domains,
		status,
		acceptance: {
			criteria_ref: await ev("criteria"),
			checks: [{ id: "unit", result: status === "accepted" ? "pass" : "fail", evidence_ref: await ev("check"), known_bad: null }],
		},
		reuse: { intake_ref: await ev("intake"), plan_ref: await ev("plan") },
		retrieved_rules: [{ uri: "rule://workflow/intake", evidence_ref: await ev("rule") }],
		records: { outcome_ref: await ev("outcome"), audit_ref: await ev("audit"), closeout_ref: await ev("closeout") },
		usage: { exports: spec.exports ?? [] },
		interventions: [{ kind: "clarification", minutes: 2, unplanned_workflow_repair: false }],
		time: { elapsed_seconds: 600, stalls: [{ seconds: 30, recovered: true }] },
		workflow_growth: { unplanned_steps: 0, mandatory_additions: [], notes_ref: null },
		false_acceptance: { occurred: false, evidence_ref: null },
		false_blocking: { occurred: false, evidence_ref: null },
	});
	Object.assign(task, spec.patch ?? {});
	return task;
}

async function sessionExport(root: string, id: string, role: string, turns: Turn[], extra: Rec = {}): Promise<Rec> {
	return { kind: "session", role, path: await writeSession(root, id, turns), ...extra };
}

const REPOS = [
	{ id: "omp", name: "oh-my-pi", is_omp: true, baseline_commit: "a".repeat(40) },
	{ id: "ext", name: "external-service", is_omp: false, baseline_commit: "b".repeat(40) },
];

async function stops(root: string, observed: boolean | null = false): Promise<Rec> {
	const base = trialSkeleton("t").stop_conditions as Record<string, Rec>;
	const out: Rec = {};
	for (const k of Object.keys(base)) out[k] = { observed, evidence_ref: null };
	return out;
}

/** Five tasks covering Python/PostgreSQL + TypeScript (one task), Rust, and a non-OMP repo. */
async function writeTrial(root: string, mutate?: (tasks: Rec[], trial: Rec) => void | Promise<void>): Promise<void> {
	await initTrial(root, "pilot");
	const specs: TaskSpec[] = [
		{
			key: "W-1",
			repo: "omp",
			domains: ["python-postgres", "typescript"],
			exports: [
				await sessionExport(root, "s1", "primary", [{ usage: usage(100, 0.1, 1), response: "r1" }, { usage: usage(50, 0) }]),
				await sessionExport(root, "s1-sup", "supporting", [{ usage: usage(30, 0.03) }]),
			],
		},
		{ key: "W-2", repo: "omp", domains: ["typescript"], exports: [await sessionExport(root, "s2", "primary", [{ usage: usage(200, 0.2) }])] },
		{ key: "W-3", repo: "omp", domains: ["rust"], exports: [await sessionExport(root, "s3", "primary", [{ usage: usage(300, 0.3) }])] },
		{ key: "W-4", repo: "ext", domains: ["other"], exports: [await sessionExport(root, "s4", "primary", [{ usage: usage(400, 0.4) }])] },
		{ key: "W-5", repo: "ext", domains: ["python-postgres"], exports: [await sessionExport(root, "s5", "primary", [{ usage: usage(500, 0.5) }])] },
	];
	const tasks = await Promise.all(specs.map(s => makeTask(root, s)));
	const trial: Rec = {
		...trialSkeleton("pilot"),
		repos: REPOS,
		pack: { install_hash: "c".repeat(64), pack_hash: "d".repeat(64) },
		config: { before: CONFIG, after: CONFIG },
		stop_conditions: await stops(root),
		tasks,
	};
	await mutate?.(tasks, trial);
	await Bun.write(path.join(root, "trial.json"), JSON.stringify(trial));
}

function at<T>(rec: Rec, ...keys: string[]): T {
	let cur: unknown = rec;
	for (const k of keys) cur = (cur as Rec)[k];
	return cur as T;
}

describe("ecc workflow trial recorder", () => {
	test("mixed-domain evidence aggregates and keeps measured, estimated, unpriced and premium usage apart", async () => {
		const root = await tempRoot();
		await writeTrial(root);
		const r = await evaluate(root);
		expect(r.errors).toEqual([]);
		expect(r.incomplete).toEqual([]);
		const s = r.summary;
		expect(s.complete).toBe(true);
		expect(s.fixed_config_success).toBe(true);
		expect(at<string[]>(s, "coverage", "missing")).toEqual([]);
		expect(at<string[]>(s, "coverage", "domains", "rust")).toEqual(["W-3"]);
		expect(at<string[]>(s, "coverage", "non_omp")).toEqual(["W-4", "W-5"]);
		// 100+50+200+300+400+500 primary, 30 supporting.
		expect(at<number>(s, "usage", "primary", "measured", "totalTokens")).toBe(1550);
		expect(at<number>(s, "usage", "supporting", "measured", "totalTokens")).toBe(30);
		expect(at<number>(s, "usage", "total", "measured", "totalTokens")).toBe(1580);
		expect(at<number>(s, "usage", "total", "estimated_cost", "total")).toBeCloseTo(1.53, 6);
		expect(at<number>(s, "usage", "total", "premium_requests")).toBe(1);
		// The 50-token zero-cost request is unpriced, not free.
		expect(at<number>(s, "usage", "total", "unpriced", "tokens")).toBe(50);
		expect(at<number>(s, "interventions", "count")).toBe(5);
		expect(at<number>(s, "time", "stalls", "count")).toBe(5);
	});

	test("failed and blocked tasks stay visible and the run is not a fixed-config success", async () => {
		const root = await tempRoot();
		await writeTrial(root, tasks => {
			tasks[2].status = "failed";
			(tasks[2].acceptance as Rec).checks = [{ id: "unit", result: "fail", evidence_ref: at<string>(tasks[2], "acceptance", "criteria_ref"), known_bad: null }];
			tasks[3].status = "blocked";
			(tasks[3].acceptance as Rec).checks = [];
		});
		const r = await evaluate(root);
		expect(r.errors).toEqual([]);
		const s = r.summary;
		expect(s.fixed_config_success).toBe(false);
		expect(at<number>(s, "outcomes", "accepted")).toBe(3);
		expect(at<Rec[]>(s, "outcomes", "not_accepted")).toEqual([
			{ work_key: "W-3", status: "failed" },
			{ work_key: "W-4", status: "blocked" },
		]);
		// Their usage still counts toward totals.
		expect(at<number>(s, "usage", "total", "measured", "totalTokens")).toBe(1580);
	});

	test("incomplete tasks and absent usage stay unknown instead of becoming zero", async () => {
		const root = await tempRoot();
		await writeTrial(root, tasks => {
			tasks[2].usage = { exports: [] };
			tasks.pop();
		});
		const r = await evaluate(root);
		expect(r.errors).toEqual([]);
		const s = r.summary;
		expect(s.complete).toBe(false);
		expect(s.fixed_config_success).toBe(false);
		expect(at<string[]>(s, "usage", "unknown_tasks")).toEqual(["W-3"]);
		expect(at<string[]>(s, "coverage", "missing")).toEqual([]);
		expect(r.incomplete.join("\n")).toContain("need 5-10 tasks, have 4");
		expect(r.incomplete.join("\n")).toContain("no primary usage export");
		expect(at<number>(s, "usage", "total", "measured", "totalTokens")).toBe(780);
		expect(await main(["check", "--root", root, "--complete"])).toBe(3);
		expect(await main(["check", "--root", root])).toBe(0);
	});

	test("an assistant turn without usage is reported as unknown, not zero tokens", async () => {
		const root = await tempRoot();
		await writeTrial(root, async tasks => {
			tasks[1].usage = { exports: [await sessionExport(root, "s2b", "primary", [{ usage: usage(200, 0.2) }, {}])] };
		});
		const r = await evaluate(root);
		expect(at<string[]>(r.summary, "usage", "partial_tasks")).toEqual(["W-2"]);
		expect(at<number>(r.summary, "usage", "total", "unknown_requests")).toBe(1);
		expect(r.summary.complete).toBe(false);
	});

	test("duplicate accounting is a hard error: same session twice, shared request id, parent plus child", async () => {
		const root = await tempRoot();
		await writeTrial(root, async tasks => {
			const dup = at<Rec[]>(tasks[0], "usage", "exports")[0];
			tasks[1].usage = { exports: [dup] };
			tasks[2].usage = { exports: [await sessionExport(root, "s3b", "primary", [{ usage: usage(1, 0), response: "r1" }])] };
		});
		const r = await evaluate(root);
		const joined = r.errors.join("\n");
		expect(joined).toContain("session s1 is linked twice");
		expect(joined).toContain("request req:r1 already counted");
		expect(await main(["summarize", "--root", root])).toBe(1);
		expect(await Bun.file(path.join(root, "summary.json")).exists()).toBe(false);

		const root2 = await tempRoot();
		await writeTrial(root2, async tasks => {
			const parent = await writeSession(root2, "p1", [{ usage: usage(10, 0.01) }], { childUsage: usage(70, 0.07) });
			const child = await writeSession(root2, "c1", [{ usage: usage(70, 0.07) }], { parent: "p1" });
			tasks[0].usage = {
				exports: [
					{ kind: "session", role: "primary", path: parent },
					{ kind: "session", role: "supporting", path: child },
				],
			};
		});
		expect((await evaluate(root2)).errors.join("\n")).toContain("its child c1 is also linked");

		const root3 = await tempRoot();
		await writeTrial(root3, async tasks => {
			const parent = await writeSession(root3, "p1", [{ usage: usage(10, 0.01) }], { childUsage: usage(70, 0.07) });
			const child = await writeSession(root3, "c1", [{ usage: usage(70, 0.07) }], { parent: "p1" });
			tasks[0].usage = {
				exports: [
					{ kind: "session", role: "primary", path: parent, children: "linked" },
					{ kind: "session", role: "supporting", path: child },
				],
			};
		});
		const ok = await evaluate(root3);
		expect(ok.errors).toEqual([]);
		// Child spend counted once: 10 (parent) + 70 (child) + other four tasks (200+300+400+500).
		expect(at<number>(ok.summary, "usage", "total", "measured", "totalTokens")).toBe(1480);
	});

	test("changed or out-of-config models and settings cannot pass as fixed-config success", async () => {
		const root = await tempRoot();
		await writeTrial(root, (_t, trial) => {
			trial.config = { before: CONFIG, after: { ...CONFIG, effort: { default: "high" } } };
		});
		const changed = await evaluate(root);
		expect(changed.errors).toEqual([]);
		expect(at<boolean>(changed.summary, "fixed_config", "held")).toBe(false);
		expect(changed.summary.fixed_config_success).toBe(false);
		expect(changed.summary.complete).toBe(false);

		const root2 = await tempRoot();
		await writeTrial(root2, async tasks => {
			tasks[0].usage = { exports: [await sessionExport(root2, "s1x", "primary", [{ usage: usage(1, 0.1), model: "openai/gpt-other" }])] };
		});
		const drift = await evaluate(root2);
		expect(at<string[]>(drift.summary, "fixed_config", "violations").join("\n")).toContain("openai/gpt-other");
		expect(drift.summary.fixed_config_success).toBe(false);
	});

	test("false acceptance and false blocking are reported separately and need evidence", async () => {
		const root = await tempRoot();
		await writeTrial(root, async tasks => {
			const ev = await evidence(root, "fa.md");
			tasks[0].false_acceptance = { occurred: true, evidence_ref: ev };
			tasks[1].false_blocking = { occurred: true, evidence_ref: ev };
			(tasks[2].acceptance as Rec).checks = [
				{
					id: "unit",
					result: "pass",
					evidence_ref: ev,
					known_bad: { candidate_ref: ev, evidence_ref: ev, rejected: false },
				},
			];
		});
		const r = await evaluate(root);
		expect(r.errors).toEqual([]);
		expect(at<string[]>(r.summary, "false_acceptance", "tasks")).toEqual(["W-1"]);
		expect(at<string[]>(r.summary, "false_blocking", "tasks")).toEqual(["W-2"]);
		expect(at<number>(r.summary, "false_acceptance", "known_bad_accepted")).toBe(1);

		const root2 = await tempRoot();
		await writeTrial(root2, tasks => {
			tasks[0].false_acceptance = { occurred: true, evidence_ref: null };
			(tasks[1].acceptance as Rec).checks = [{ id: "u", result: "pass", evidence_ref: null, known_bad: { rejected: true } }];
		});
		const bad = (await evaluate(root2)).errors.join("\n");
		expect(bad).toContain("false_acceptance.evidence_ref: required when occurred is true");
		expect(bad).toContain("known_bad: candidate_ref and evidence_ref are required");
	});

	test("an unassessed or observed §8 stop condition is not hidden", async () => {
		const root = await tempRoot();
		await writeTrial(root, async (_t, trial) => {
			trial.stop_conditions = await stops(root, null);
		});
		const r = await evaluate(root);
		expect(r.summary.complete).toBe(false);
		expect(r.incomplete.join("\n")).toContain("stop_conditions.policy_boundary_changed.observed: not assessed");

		const root2 = await tempRoot();
		await writeTrial(root2, (_t, trial) => {
			(trial.stop_conditions as Rec).dependency_unavailable = { observed: true, evidence_ref: null };
		});
		expect((await evaluate(root2)).errors.join("\n")).toContain("stop_conditions.dependency_unavailable.evidence_ref: required");
	});

	test("init lays out an empty, valid, incomplete trial and refuses to overwrite", async () => {
		const root = await tempRoot();
		expect(await main(["init", "--root", root, "--id", "pilot"])).toBe(0);
		expect(await Bun.file(path.join(root, "evidence", "task.template.json")).exists()).toBe(true);
		expect(await main(["check", "--root", root])).toBe(0);
		expect(await main(["check", "--root", root, "--complete"])).toBe(3);
		expect(await main(["init", "--root", root])).toBe(1);
		expect(await main(["summarize", "--root", root])).toBe(0);
		const summary = (await Bun.file(path.join(root, "summary.json")).json()) as Rec;
		expect(summary.complete).toBe(false);
	});
});
