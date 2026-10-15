#!/usr/bin/env bun
/**
 * Offline recorder for the ECC WP3 workflow trial (docs/programme/ECC-WP3-TRIAL.md).
 *
 *   bun scripts/ecc-workflow-trial.ts init      --root DIR [--id TRIAL_ID]
 *   bun scripts/ecc-workflow-trial.ts check     --root DIR [--complete]
 *   bun scripts/ecc-workflow-trial.ts summarize --root DIR
 *
 * Records paths and flags only: no model calls, no settings edits, no adoption decision.
 * `check` exits 1 on structural errors (malformed record, unresolvable path, duplicate
 * accounting). Missing evidence is reported as "incomplete" and, with --complete, exits 3.
 * `summarize` writes <root>/summary.json once there are no errors; incomplete evidence,
 * unknown usage, changed configuration and failed/blocked tasks stay visible in it.
 */

import * as fs from "node:fs/promises";
import * as path from "node:path";
import { isEnoent } from "@oh-my-pi/pi-utils";
import type { Usage } from "@oh-my-pi/pi-ai";
import type { SessionEntry } from "../packages/coding-agent/src/session/session-entries";
import { parseSessionContent } from "../packages/coding-agent/src/session/session-loader";
import {
	extractToolResultUsage,
	USAGE_REPORTING_TOOL_NAMES,
} from "../packages/coding-agent/src/session/session-manager";
import { addUsageTotals, createUsageTotals } from "../packages/coding-agent/src/utils/usage-totals";

export const TRIAL_VERSION = 1;
export const MIN_TASKS = 5;
export const MAX_TASKS = 10;
export const DOMAINS = ["python-postgres", "typescript", "rust", "other"] as const;
export const STATUSES = ["pending", "accepted", "failed", "blocked", "abandoned"] as const;
/** The §8 "stop or narrow a rollout when" conditions, in spec order. */
export const STOP_CONDITIONS = [
	"policy_boundary_changed",
	"dependency_unavailable",
	"transform_lost_meaning",
	"false_acceptance_or_recovery_broken",
	"supporting_activity_growth_without_outcomes",
	"update_conflict_requires_fork",
] as const;
const CONFIG_KEYS = ["models", "effort", "fallback", "advisor", "assistance"] as const;
const MANDATORY_KINDS = ["reviewer", "coverage", "commit", "push", "approval", "other"] as const;
const HASH_RE = /^[0-9a-f]{12,64}$/i;
const COMMIT_RE = /^[0-9a-f]{40}$/i;

type Domain = (typeof DOMAINS)[number];
type Status = (typeof STATUSES)[number];
type Json = unknown;
type Rec = Record<string, unknown>;

export interface UsageRollup {
	/** Requests whose usage was reported. */
	requests: number;
	/** Requests present in an export without usage. Never counted as zero. */
	unknown_requests: number;
	/** Measured tokens; `cost` is the estimated price of priced requests only. */
	usage: Usage;
	priced_requests: number;
	/** Requests with tokens but no price. A missing price is not zero cost. */
	unpriced_requests: number;
	unpriced_tokens: number;
	premium_requests: number;
}

export interface Evaluation {
	errors: string[];
	incomplete: string[];
	summary: Rec;
}

function isRec(v: Json): v is Rec {
	return typeof v === "object" && v !== null && !Array.isArray(v);
}

function emptyRollup(): UsageRollup {
	return {
		requests: 0,
		unknown_requests: 0,
		usage: createUsageTotals(),
		priced_requests: 0,
		unpriced_requests: 0,
		unpriced_tokens: 0,
		premium_requests: 0,
	};
}

function addRequest(into: UsageRollup, usage: Usage | undefined, premium: number): void {
	if (!usage) {
		into.unknown_requests++;
		return;
	}
	into.requests++;
	addUsageTotals(into.usage, usage);
	into.premium_requests += premium;
	if (usage.cost.total > 0) into.priced_requests++;
	else if (usage.totalTokens > 0) {
		into.unpriced_requests++;
		into.unpriced_tokens += usage.totalTokens;
	}
}

function mergeRollup(into: UsageRollup, from: UsageRollup): void {
	into.requests += from.requests;
	into.unknown_requests += from.unknown_requests;
	addUsageTotals(into.usage, from.usage);
	into.priced_requests += from.priced_requests;
	into.unpriced_requests += from.unpriced_requests;
	into.unpriced_tokens += from.unpriced_tokens;
	into.premium_requests += from.premium_requests;
}

const nullRef = null;

/** Task skeleton written to evidence/task.template.json; fill one copy per task into trial.json. */
export function taskTemplate(): Rec {
	return {
		work_key: "",
		title: "",
		repo: "",
		domains: [],
		status: "pending",
		acceptance: {
			criteria_ref: nullRef,
			checks: [
				{
					id: "",
					result: "pass",
					evidence_ref: nullRef,
					known_bad: null,
				},
			],
		},
		reuse: { intake_ref: nullRef, plan_ref: nullRef },
		retrieved_rules: [],
		records: { outcome_ref: nullRef, audit_ref: nullRef, closeout_ref: nullRef },
		usage: { exports: [] },
		interventions: null,
		time: { elapsed_seconds: null, stalls: [] },
		workflow_growth: { unplanned_steps: null, mandatory_additions: [], notes_ref: nullRef },
		false_acceptance: { occurred: null, evidence_ref: nullRef },
		false_blocking: { occurred: null, evidence_ref: nullRef },
	};
}

export function trialSkeleton(trialId: string): Rec {
	return {
		version: TRIAL_VERSION,
		trial_id: trialId,
		repos: [],
		pack: null,
		config: { before: null, after: null },
		tasks: [],
		stop_conditions: Object.fromEntries(STOP_CONDITIONS.map(k => [k, { observed: null, evidence_ref: nullRef }])),
	};
}

export async function initTrial(root: string, trialId?: string): Promise<void> {
	const trialPath = path.join(root, "trial.json");
	if (await Bun.file(trialPath).exists()) throw new Error(`${trialPath} already exists; refusing to overwrite`);
	for (const dir of ["sessions", "accounting", "tasks"])
		await fs.mkdir(path.join(root, "evidence", dir), { recursive: true });
	await Bun.write(
		path.join(root, "evidence", "task.template.json"),
		`${JSON.stringify(taskTemplate(), null, "\t")}\n`,
	);
	await Bun.write(
		trialPath,
		`${JSON.stringify(trialSkeleton(trialId ?? path.basename(path.resolve(root))), null, "\t")}\n`,
	);
}

class Ctx {
	errors: string[] = [];
	incomplete: string[] = [];
	constructor(readonly root: string) {}

	err(where: string, msg: string): void {
		this.errors.push(`${where}: ${msg}`);
	}
	missing(where: string, msg = "not recorded"): void {
		this.incomplete.push(`${where}: ${msg}`);
	}

	resolve(ref: string): string {
		return path.resolve(this.root, ref);
	}

	async exists(ref: string): Promise<boolean> {
		try {
			await fs.stat(this.resolve(ref));
			return true;
		} catch (e) {
			if (isEnoent(e)) return false;
			throw e;
		}
	}

	/** A recorded path. `null` means not recorded yet; `need` makes that incomplete. */
	async ref(v: Json, where: string, need: boolean): Promise<string | null> {
		if (v === null || v === undefined) {
			if (need) this.missing(where);
			return null;
		}
		if (typeof v !== "string" || v === "") {
			this.err(where, "must be a non-empty path string or null");
			return null;
		}
		if (!(await this.exists(v))) {
			this.err(where, `recorded path does not exist: ${v}`);
			return null;
		}
		return v;
	}

	async readJson(ref: string, where: string): Promise<Json | undefined> {
		try {
			return await Bun.file(this.resolve(ref)).json();
		} catch {
			this.err(where, `not readable JSON: ${ref}`);
			return undefined;
		}
	}
}

interface Req {
	key: string;
	usage: Usage | undefined;
	premium: number;
}

interface SessionFacts {
	taskKey: string;
	role: "primary" | "supporting";
	file: string;
	sessionId: string;
	parentSession: string | undefined;
	childrenLinked: boolean;
	reportedChildRequests: number;
	models: Set<string>;
	levels: Set<string>;
	fallbackActivations: number;
}

interface LoadedExport {
	reqs: Req[];
	role: "primary" | "supporting";
	session?: SessionFacts;
}

async function loadSession(
	ctx: Ctx,
	file: string,
	where: string,
	taskKey: string,
	role: "primary" | "supporting",
	childrenLinked: boolean,
): Promise<LoadedExport | undefined> {
	const text = await Bun.file(ctx.resolve(file)).text();
	const parsed = parseSessionContent(text);
	const header = parsed.entries[0];
	if (parsed.invalidHeader || header?.type !== "session") {
		ctx.err(where, `not a session export (no valid header): ${file}`);
		return undefined;
	}
	if (parsed.malformedRecords > 0) {
		ctx.missing(where, `${parsed.malformedRecords} malformed record(s); usage may be missing`);
	}
	const facts: SessionFacts = {
		taskKey,
		role,
		file: ctx.resolve(file),
		sessionId: header.id,
		parentSession: header.parentSession,
		childrenLinked,
		reportedChildRequests: 0,
		models: new Set(),
		levels: new Set(),
		fallbackActivations: 0,
	};
	const reqs: Req[] = [];
	for (const entry of parsed.entries.slice(1) as SessionEntry[]) {
		if (entry.type === "model_change") {
			facts.models.add(entry.model);
			if (entry.resolvedModelIsFallback) facts.fallbackActivations++;
		} else if (entry.type === "thinking_level_change") {
			if (entry.thinkingLevel) facts.levels.add(entry.thinkingLevel);
		} else if (entry.type === "model_usage") {
			facts.models.add(`${entry.provider}/${entry.model}`);
			reqs.push({
				key: `${header.id}#${entry.id}`,
				usage: extractToolResultUsage({ usage: entry.usage }),
				premium: entry.usage?.premiumRequests ?? 0,
			});
		} else if (entry.type === "message") {
			const m = entry.message;
			if (m.role === "assistant") {
				facts.models.add(`${m.provider}/${m.model}`);
				const usage = extractToolResultUsage({ usage: m.usage });
				reqs.push({
					key: m.responseId ? `req:${m.responseId}` : `${header.id}#${entry.id}`,
					usage,
					premium: usage?.premiumRequests ?? 0,
				});
			} else if (m.role === "toolResult" && USAGE_REPORTING_TOOL_NAMES.has(m.toolName)) {
				const usage = extractToolResultUsage(m.details);
				// Child spend reported by the parent. Absent usage on a `task` result is unknown spend.
				if (!usage && m.toolName !== "task") continue;
				facts.reportedChildRequests++;
				if (childrenLinked) continue;
				reqs.push({ key: `${header.id}#${entry.id}`, usage, premium: usage?.premiumRequests ?? 0 });
			}
		}
	}
	return { reqs, role, session: facts };
}

async function loadAccounting(
	ctx: Ctx,
	file: string,
	where: string,
	role: "primary" | "supporting",
): Promise<LoadedExport | undefined> {
	const data = await ctx.readJson(file, where);
	if (data === undefined) return undefined;
	if (!isRec(data) || !Array.isArray(data.requests)) {
		ctx.err(where, 'accounting export must be {"requests":[{"id":string,"usage":Usage|null}]}');
		return undefined;
	}
	const reqs: Req[] = [];
	for (const [i, r] of data.requests.entries()) {
		if (!isRec(r) || typeof r.id !== "string" || r.id === "") {
			ctx.err(`${where}.requests[${i}]`, "needs a non-empty string id");
			continue;
		}
		const usage = r.usage === null || r.usage === undefined ? undefined : extractToolResultUsage({ usage: r.usage });
		if (r.usage !== null && r.usage !== undefined && !usage) {
			ctx.err(`${where}.requests[${i}]`, "usage is malformed; use null for unknown usage");
			continue;
		}
		reqs.push({ key: `req:${r.id}`, usage, premium: usage?.premiumRequests ?? 0 });
	}
	return { reqs, role };
}

function oneOf<T extends string>(v: Json, allowed: readonly T[]): v is T {
	return typeof v === "string" && (allowed as readonly string[]).includes(v);
}

function sortKeys(v: Json): Json {
	if (Array.isArray(v)) return v.map(sortKeys);
	if (isRec(v))
		return Object.fromEntries(
			Object.keys(v)
				.sort()
				.map(k => [k, sortKeys(v[k])]),
		);
	return v;
}

function allowedValues(snap: Rec | null, key: "models" | "effort"): Set<string> {
	const out = new Set<string>();
	if (!snap) return out;
	const group = snap[key];
	if (isRec(group)) for (const v of Object.values(group)) if (typeof v === "string") out.add(v);
	const fallback = snap.fallback;
	if (key === "models" && isRec(fallback) && Array.isArray(fallback.models)) {
		for (const v of fallback.models) if (typeof v === "string") out.add(v);
	}
	return out;
}

function modelAllowed(allowed: Set<string>, observed: string): boolean {
	const bare = observed.includes("/") ? observed.slice(observed.indexOf("/") + 1) : observed;
	return allowed.has(observed) || allowed.has(bare);
}

async function checkConfig(
	ctx: Ctx,
	trial: Rec,
): Promise<{ before: Rec | null; after: Rec | null; held: boolean; violations: string[] }> {
	const violations: string[] = [];
	const snaps: Record<"before" | "after", Rec | null> = { before: null, after: null };
	const cfg = trial.config;
	if (!isRec(cfg)) {
		ctx.err("config", "must be an object with before/after");
		return { ...snaps, held: false, violations };
	}
	for (const side of ["before", "after"] as const) {
		const snap = cfg[side];
		if (snap === null || snap === undefined) {
			ctx.missing(`config.${side}`);
			continue;
		}
		if (!isRec(snap)) {
			ctx.err(`config.${side}`, "must be an object or null");
			continue;
		}
		snaps[side] = snap;
		for (const key of CONFIG_KEYS) {
			if (snap[key] === null || snap[key] === undefined) ctx.missing(`config.${side}.${key}`);
		}
		if (isRec(snap.models)) {
			for (const [role, model] of Object.entries(snap.models)) {
				if (typeof model !== "string" || model === "")
					ctx.err(`config.${side}.models.${role}`, "must be a model id string");
			}
		} else if (snap.models !== null && snap.models !== undefined) {
			ctx.err(`config.${side}.models`, "must be a role -> model object");
		}
	}
	if (snaps.before && snaps.after && !Bun.deepEquals(sortKeys(snaps.before), sortKeys(snaps.after), true)) {
		violations.push(
			"config.before differs from config.after: models/effort/fallback/advisor/assistance were not held fixed",
		);
	}
	return { ...snaps, held: violations.length === 0 && snaps.before !== null && snaps.after !== null, violations };
}

function checkPack(ctx: Ctx, pack: Json): Rec | null {
	if (pack === null || pack === undefined) {
		ctx.missing("pack");
		return null;
	}
	if (!isRec(pack)) {
		ctx.err("pack", "must be an object or null");
		return null;
	}
	for (const key of ["install_hash", "pack_hash"]) {
		const v = pack[key];
		if (v === null || v === undefined) ctx.missing(`pack.${key}`);
		else if (typeof v !== "string" || !HASH_RE.test(v)) ctx.err(`pack.${key}`, "must be a hex hash");
	}
	return pack;
}

interface RepoFacts {
	isOmp: boolean;
}

function checkRepos(ctx: Ctx, repos: Json): Map<string, RepoFacts> {
	const out = new Map<string, RepoFacts>();
	if (!Array.isArray(repos)) {
		ctx.err("repos", "must be an array");
		return out;
	}
	for (const [i, r] of repos.entries()) {
		const where = `repos[${i}]`;
		if (!isRec(r) || typeof r.id !== "string" || r.id === "") {
			ctx.err(where, "needs a non-empty string id");
			continue;
		}
		if (out.has(r.id)) ctx.err(where, `duplicate repo id ${r.id}`);
		if (typeof r.is_omp !== "boolean") ctx.err(`${where}.is_omp`, "must be boolean");
		if (typeof r.name !== "string" || r.name === "") ctx.err(`${where}.name`, "must be a non-empty string");
		if (typeof r.baseline_commit !== "string" || !COMMIT_RE.test(r.baseline_commit)) {
			ctx.err(`${where}.baseline_commit`, "must be a 40-hex commit id");
		}
		out.set(r.id, { isOmp: r.is_omp === true });
	}
	return out;
}

interface TaskResult {
	workKey: string;
	status: Status | "invalid";
	domains: Domain[];
	repo: string;
	nonOmp: boolean;
	primary: UsageRollup;
	supporting: UsageRollup;
	usageStatus: "measured" | "partial" | "unknown";
	interventionCount: number | null;
	interventionMinutes: number | null;
	unplannedRepairs: number;
	elapsed: number | null;
	stallCount: number;
	stallSeconds: number;
	unrecoveredStalls: number;
	unplannedSteps: number | null;
	mandatoryAdditions: number;
	rules: number;
	falseAcceptance: boolean | null;
	falseBlocking: boolean | null;
	knownBad: { checks: number; rejected: number; accepted: number };
}

function num(ctx: Ctx, v: Json, where: string): number | null {
	if (v === null || v === undefined) return null;
	if (typeof v !== "number" || !Number.isFinite(v) || v < 0) {
		ctx.err(where, "must be a finite number >= 0 or null");
		return null;
	}
	return v;
}

async function flag(ctx: Ctx, v: Json, where: string, needed: boolean): Promise<boolean | null> {
	if (!isRec(v)) {
		ctx.err(where, "must be an object");
		return null;
	}
	const occurred = v.occurred;
	if (occurred === null || occurred === undefined) {
		if (needed) ctx.missing(`${where}.occurred`);
		return null;
	}
	if (typeof occurred !== "boolean") {
		ctx.err(`${where}.occurred`, "must be boolean or null");
		return null;
	}
	const ev = await ctx.ref(v.evidence_ref, `${where}.evidence_ref`, false);
	if (occurred && ev === null) ctx.err(`${where}.evidence_ref`, "required when occurred is true");
	return occurred;
}

async function checkTask(
	ctx: Ctx,
	task: Json,
	i: number,
	repos: Map<string, RepoFacts>,
	seenKeys: Set<string>,
	exportsOut: LoadedExport[],
	reqOwners: Map<string, string>,
): Promise<TaskResult | undefined> {
	const base = `tasks[${i}]`;
	if (!isRec(task)) {
		ctx.err(base, "must be an object");
		return undefined;
	}
	const workKey = typeof task.work_key === "string" ? task.work_key.trim() : "";
	if (workKey === "") {
		ctx.err(`${base}.work_key`, "needs a non-empty native work key");
		return undefined;
	}
	const where = `task ${workKey}`;
	if (seenKeys.has(workKey)) ctx.err(where, "duplicate work_key");
	seenKeys.add(workKey);
	if (typeof task.title !== "string" || task.title === "") ctx.err(`${where}.title`, "needs a non-empty title");
	const repoId = typeof task.repo === "string" ? task.repo : "";
	const repo = repos.get(repoId);
	if (!repo) ctx.err(`${where}.repo`, `unknown repo id "${repoId}"`);
	const domains: Domain[] = [];
	if (!Array.isArray(task.domains) || task.domains.length === 0)
		ctx.err(`${where}.domains`, "needs at least one domain");
	else {
		for (const d of task.domains) {
			if (oneOf(d, DOMAINS)) domains.push(d);
			else ctx.err(`${where}.domains`, `unknown domain ${String(d)}; use ${DOMAINS.join("|")}`);
		}
	}
	if (!oneOf(task.status, STATUSES)) {
		ctx.err(`${where}.status`, `must be ${STATUSES.join("|")}`);
		return undefined;
	}
	const status = task.status;
	const done = status !== "pending";
	if (!done) ctx.missing(where, "status is pending");
	const accepted = status === "accepted";

	// Acceptance and check evidence.
	const acc = isRec(task.acceptance) ? task.acceptance : undefined;
	const knownBad = { checks: 0, rejected: 0, accepted: 0 };
	if (!acc) ctx.err(`${where}.acceptance`, "must be an object");
	else {
		await ctx.ref(acc.criteria_ref, `${where}.acceptance.criteria_ref`, accepted);
		const checks = Array.isArray(acc.checks) ? acc.checks : undefined;
		if (!checks) ctx.err(`${where}.acceptance.checks`, "must be an array");
		else {
			let passes = 0;
			let evidenced = 0;
			for (const [j, c] of checks.entries()) {
				const cw = `${where}.acceptance.checks[${j}]`;
				if (!isRec(c) || !oneOf(c.result, ["pass", "fail"] as const)) {
					ctx.err(cw, 'needs result "pass"|"fail"');
					continue;
				}
				if (c.result === "pass") passes++;
				else if (accepted) ctx.err(cw, "failing check on an accepted task");
				if ((await ctx.ref(c.evidence_ref, `${cw}.evidence_ref`, done)) !== null) evidenced++;
				if (c.known_bad !== null && c.known_bad !== undefined) {
					const kb = c.known_bad;
					if (!isRec(kb) || typeof kb.rejected !== "boolean") {
						ctx.err(`${cw}.known_bad`, "needs candidate_ref, evidence_ref and boolean rejected");
						continue;
					}
					const cand = await ctx.ref(kb.candidate_ref, `${cw}.known_bad.candidate_ref`, false);
					const ev = await ctx.ref(kb.evidence_ref, `${cw}.known_bad.evidence_ref`, false);
					if (cand === null || ev === null)
						ctx.err(`${cw}.known_bad`, "candidate_ref and evidence_ref are required");
					knownBad.checks++;
					if (kb.rejected) knownBad.rejected++;
					else knownBad.accepted++;
				}
			}
			if (accepted && (passes === 0 || evidenced === 0))
				ctx.missing(`${where}.acceptance.checks`, "accepted task needs evidenced passing check");
		}
	}

	// Intake/plan reuse refs live in existing records; only refs are stored here.
	const reuse = isRec(task.reuse) ? task.reuse : undefined;
	if (!reuse) ctx.err(`${where}.reuse`, "must be an object");
	else {
		await ctx.ref(reuse.intake_ref, `${where}.reuse.intake_ref`, accepted);
		await ctx.ref(reuse.plan_ref, `${where}.reuse.plan_ref`, accepted);
	}

	let rules = 0;
	if (!Array.isArray(task.retrieved_rules)) ctx.err(`${where}.retrieved_rules`, "must be an array");
	else {
		for (const [j, r] of task.retrieved_rules.entries()) {
			const rw = `${where}.retrieved_rules[${j}]`;
			if (!isRec(r) || typeof r.uri !== "string" || !r.uri.startsWith("rule://")) {
				ctx.err(rw, "needs a rule:// uri");
				continue;
			}
			await ctx.ref(r.evidence_ref, `${rw}.evidence_ref`, false);
			rules++;
		}
	}

	const rec = isRec(task.records) ? task.records : undefined;
	if (!rec) ctx.err(`${where}.records`, "must be an object");
	else {
		await ctx.ref(rec.outcome_ref, `${where}.records.outcome_ref`, done);
		await ctx.ref(rec.audit_ref, `${where}.records.audit_ref`, accepted);
		await ctx.ref(rec.closeout_ref, `${where}.records.closeout_ref`, accepted);
	}

	// Usage exports.
	const primary = emptyRollup();
	const supporting = emptyRollup();
	let primaryExports = 0;
	const ux = isRec(task.usage) && Array.isArray(task.usage.exports) ? task.usage.exports : undefined;
	if (!ux) ctx.err(`${where}.usage.exports`, "must be an array");
	else {
		for (const [j, e] of ux.entries()) {
			const ew = `${where}.usage.exports[${j}]`;
			if (
				!isRec(e) ||
				!oneOf(e.kind, ["session", "accounting"] as const) ||
				!oneOf(e.role, ["primary", "supporting"] as const)
			) {
				ctx.err(ew, 'needs kind "session"|"accounting" and role "primary"|"supporting"');
				continue;
			}
			const file = await ctx.ref(e.path, `${ew}.path`, true);
			if (file === null) continue;
			const loaded =
				e.kind === "session"
					? await loadSession(ctx, file, ew, workKey, e.role, e.children === "linked")
					: await loadAccounting(ctx, file, ew, e.role);
			if (!loaded) continue;
			exportsOut.push(loaded);
			if (e.role === "primary") primaryExports++;
			const rollup = e.role === "primary" ? primary : supporting;
			for (const r of loaded.reqs) {
				const owner = reqOwners.get(r.key);
				if (owner !== undefined) {
					ctx.err(ew, `duplicate accounting: request ${r.key} already counted by ${owner}`);
					continue;
				}
				reqOwners.set(r.key, ew);
				addRequest(rollup, r.usage, r.premium);
			}
		}
	}
	let usageStatus: TaskResult["usageStatus"] = "measured";
	if (primaryExports === 0 || primary.requests === 0) usageStatus = "unknown";
	else if (primary.unknown_requests + supporting.unknown_requests > 0) usageStatus = "partial";
	if (done && usageStatus === "unknown") ctx.missing(`${where}.usage`, "no primary usage export; usage is unknown");
	if (done && usageStatus === "partial")
		ctx.missing(`${where}.usage`, "some requests have no usage; usage is partial");

	// Interventions.
	let interventionCount: number | null = null;
	let interventionMinutes: number | null = null;
	let unplannedRepairs = 0;
	if (task.interventions === null || task.interventions === undefined) {
		if (done) ctx.missing(`${where}.interventions`);
	} else if (!Array.isArray(task.interventions)) ctx.err(`${where}.interventions`, "must be an array or null");
	else {
		interventionCount = 0;
		interventionMinutes = 0;
		for (const [j, iv] of task.interventions.entries()) {
			const iw = `${where}.interventions[${j}]`;
			const minutes = isRec(iv) ? num(ctx, iv.minutes, `${iw}.minutes`) : null;
			if (
				!isRec(iv) ||
				typeof iv.kind !== "string" ||
				minutes === null ||
				typeof iv.unplanned_workflow_repair !== "boolean"
			) {
				ctx.err(iw, "needs kind, minutes >= 0 and boolean unplanned_workflow_repair");
				continue;
			}
			interventionCount++;
			interventionMinutes += minutes;
			if (iv.unplanned_workflow_repair) unplannedRepairs++;
		}
	}

	// Elapsed time and stalls.
	const time = isRec(task.time) ? task.time : undefined;
	let elapsed: number | null = null;
	let stallCount = 0;
	let stallSeconds = 0;
	let unrecoveredStalls = 0;
	if (!time) ctx.err(`${where}.time`, "must be an object");
	else {
		elapsed = num(ctx, time.elapsed_seconds, `${where}.time.elapsed_seconds`);
		if (done && elapsed === null) ctx.missing(`${where}.time.elapsed_seconds`);
		if (!Array.isArray(time.stalls)) ctx.err(`${where}.time.stalls`, "must be an array");
		else {
			for (const [j, s] of time.stalls.entries()) {
				const sw = `${where}.time.stalls[${j}]`;
				const secs = isRec(s) ? num(ctx, s.seconds, `${sw}.seconds`) : null;
				if (!isRec(s) || secs === null || typeof s.recovered !== "boolean") {
					ctx.err(sw, "needs seconds >= 0 and boolean recovered");
					continue;
				}
				stallCount++;
				stallSeconds += secs;
				if (!s.recovered) unrecoveredStalls++;
			}
		}
	}

	// Workflow growth.
	const growth = isRec(task.workflow_growth) ? task.workflow_growth : undefined;
	let unplannedSteps: number | null = null;
	let mandatoryAdditions = 0;
	if (!growth) ctx.err(`${where}.workflow_growth`, "must be an object");
	else {
		unplannedSteps = num(ctx, growth.unplanned_steps, `${where}.workflow_growth.unplanned_steps`);
		if (done && unplannedSteps === null) ctx.missing(`${where}.workflow_growth.unplanned_steps`);
		await ctx.ref(growth.notes_ref, `${where}.workflow_growth.notes_ref`, false);
		if (!Array.isArray(growth.mandatory_additions))
			ctx.err(`${where}.workflow_growth.mandatory_additions`, "must be an array");
		else {
			for (const [j, m] of growth.mandatory_additions.entries()) {
				const mw = `${where}.workflow_growth.mandatory_additions[${j}]`;
				if (!isRec(m) || !oneOf(m.kind, MANDATORY_KINDS)) ctx.err(mw, `needs kind ${MANDATORY_KINDS.join("|")}`);
				else {
					await ctx.ref(m.ref, `${mw}.ref`, true);
					mandatoryAdditions++;
				}
			}
		}
	}

	const falseAcceptance = await flag(ctx, task.false_acceptance, `${where}.false_acceptance`, done);
	const falseBlocking = await flag(ctx, task.false_blocking, `${where}.false_blocking`, done);

	return {
		workKey,
		status,
		domains,
		repo: repoId,
		nonOmp: repo ? !repo.isOmp : false,
		primary,
		supporting,
		usageStatus,
		interventionCount,
		interventionMinutes,
		unplannedRepairs,
		elapsed,
		stallCount,
		stallSeconds,
		unrecoveredStalls,
		unplannedSteps,
		mandatoryAdditions,
		rules,
		falseAcceptance,
		falseBlocking,
		knownBad,
	};
}

function rollupJson(r: UsageRollup): Rec {
	const u = r.usage;
	return {
		requests: r.requests,
		unknown_requests: r.unknown_requests,
		measured: {
			input: u.input,
			output: u.output,
			cacheRead: u.cacheRead,
			cacheWrite: u.cacheWrite,
			totalTokens: u.totalTokens,
		},
		estimated_cost: { total: u.cost.total, priced_requests: r.priced_requests },
		unpriced: { requests: r.unpriced_requests, tokens: r.unpriced_tokens },
		premium_requests: r.premium_requests,
	};
}

function sum(xs: readonly (number | null)[]): number {
	return xs.reduce<number>((a, b) => a + (b ?? 0), 0);
}

/** Parent/child accounting and observed-config checks across all linked session exports. */
function crossCheckSessions(
	ctx: Ctx,
	sessions: SessionFacts[],
	cfg: { before: Rec | null; after: Rec | null },
	violations: string[],
): { models: string[]; fallbackActivations: number } {
	const byId = new Map<string, SessionFacts>();
	const byFile = new Map<string, SessionFacts>();
	for (const s of sessions) {
		const dup = byId.get(s.sessionId);
		if (dup) ctx.err(`task ${s.taskKey}`, `duplicate accounting: session ${s.sessionId} is linked twice`);
		byId.set(s.sessionId, s);
		byFile.set(s.file, s);
	}
	const linkedParents = new Set<SessionFacts>();
	for (const s of sessions) {
		if (!s.parentSession) continue;
		const parent = byId.get(s.parentSession) ?? byFile.get(path.resolve(s.parentSession));
		if (!parent || parent === s) continue;
		linkedParents.add(parent);
		if (!parent.childrenLinked && parent.reportedChildRequests > 0) {
			ctx.err(
				`task ${parent.taskKey}`,
				`duplicate accounting: session ${parent.sessionId} reports child spend and its child ${s.sessionId} is also linked; set children:"linked" on the parent export`,
			);
		}
	}
	for (const s of sessions) {
		if (s.childrenLinked && !linkedParents.has(s)) {
			ctx.missing(
				`task ${s.taskKey}`,
				`session ${s.sessionId} declares children:"linked" but no linked child session names it as parent`,
			);
		}
	}
	const allowedModels = new Set([...allowedValues(cfg.before, "models"), ...allowedValues(cfg.after, "models")]);
	const allowedLevels = new Set([...allowedValues(cfg.before, "effort"), ...allowedValues(cfg.after, "effort")]);
	const observed = new Set<string>();
	let fallbackActivations = 0;
	for (const s of sessions) {
		fallbackActivations += s.fallbackActivations;
		for (const m of s.models) {
			observed.add(m);
			if (allowedModels.size > 0 && !modelAllowed(allowedModels, m)) {
				violations.push(
					`task ${s.taskKey}: session ${s.sessionId} used model ${m} outside the recorded configuration`,
				);
			}
		}
		for (const l of s.levels) {
			if (allowedLevels.size > 0 && !allowedLevels.has(l)) {
				violations.push(
					`task ${s.taskKey}: session ${s.sessionId} ran effort ${l} outside the recorded configuration`,
				);
			}
		}
	}
	return { models: [...observed].sort(), fallbackActivations };
}

async function checkStops(ctx: Ctx, stops: Json): Promise<Rec[]> {
	const out: Rec[] = [];
	if (!isRec(stops)) {
		ctx.err("stop_conditions", "must be an object");
		return out;
	}
	for (const key of Object.keys(stops)) {
		if (!(STOP_CONDITIONS as readonly string[]).includes(key))
			ctx.err(`stop_conditions.${key}`, "unknown §8 condition");
	}
	for (const key of STOP_CONDITIONS) {
		const where = `stop_conditions.${key}`;
		const s = stops[key];
		if (!isRec(s)) {
			ctx.missing(where);
			out.push({ condition: key, observed: null, evidence_ref: null });
			continue;
		}
		let observed: boolean | null = null;
		if (s.observed === null || s.observed === undefined) ctx.missing(`${where}.observed`, "not assessed");
		else if (typeof s.observed !== "boolean") ctx.err(`${where}.observed`, "must be boolean or null");
		else observed = s.observed;
		const ev = await ctx.ref(s.evidence_ref, `${where}.evidence_ref`, false);
		if (observed === true && ev === null) ctx.err(`${where}.evidence_ref`, "required when observed is true");
		out.push({ condition: key, observed, evidence_ref: ev });
	}
	return out;
}

export async function evaluate(root: string): Promise<Evaluation> {
	const ctx = new Ctx(root);
	let trial: Json;
	try {
		trial = await Bun.file(path.join(root, "trial.json")).json();
	} catch (e) {
		const why = isEnoent(e) ? "missing; run init first" : "not valid JSON";
		return { errors: [`trial.json: ${why}`], incomplete: [], summary: {} };
	}
	if (!isRec(trial) || trial.version !== TRIAL_VERSION) {
		return { errors: [`trial.json: must be an object with version ${TRIAL_VERSION}`], incomplete: [], summary: {} };
	}
	const trialId = typeof trial.trial_id === "string" && trial.trial_id !== "" ? trial.trial_id : null;
	if (!trialId) ctx.err("trial_id", "needs a non-empty string");

	const pack = checkPack(ctx, trial.pack);
	const repos = checkRepos(ctx, trial.repos);
	const config = await checkConfig(ctx, trial);

	const results: TaskResult[] = [];
	const exportsAll: LoadedExport[] = [];
	const seenKeys = new Set<string>();
	const reqOwners = new Map<string, string>();
	if (!Array.isArray(trial.tasks)) ctx.err("tasks", "must be an array");
	else {
		for (const [i, t] of trial.tasks.entries()) {
			const r = await checkTask(ctx, t, i, repos, seenKeys, exportsAll, reqOwners);
			if (r) results.push(r);
		}
	}
	if (results.length < MIN_TASKS || results.length > MAX_TASKS) {
		ctx.missing("tasks", `need ${MIN_TASKS}-${MAX_TASKS} tasks, have ${results.length}`);
	}
	const covered = new Set(results.flatMap(t => t.domains));
	const missingCoverage: string[] = [];
	for (const d of ["python-postgres", "typescript", "rust"] as const) if (!covered.has(d)) missingCoverage.push(d);
	if (!results.some(t => t.nonOmp)) missingCoverage.push("non-omp");
	for (const d of missingCoverage) ctx.missing("coverage", `no task covers ${d}`);

	const stops = await checkStops(ctx, trial.stop_conditions);

	const violations = [...config.violations];
	const observed = crossCheckSessions(
		ctx,
		exportsAll.flatMap(e => (e.session ? [e.session] : [])),
		config,
		violations,
	);
	if (!config.held && config.violations.length === 0) violations.push("configuration before/after not both recorded");
	for (const v of violations) ctx.missing("fixed_config", v);

	// Aggregate. Unknown usage/time is listed, never folded into zero.
	const primary = emptyRollup();
	const supporting = emptyRollup();
	for (const t of results) {
		mergeRollup(primary, t.primary);
		mergeRollup(supporting, t.supporting);
	}
	const total = emptyRollup();
	mergeRollup(total, primary);
	mergeRollup(total, supporting);
	const count = (s: Status) => results.filter(t => t.status === s).length;
	const knownBad = {
		checks: sum(results.map(t => t.knownBad.checks)),
		rejected: sum(results.map(t => t.knownBad.rejected)),
		accepted: sum(results.map(t => t.knownBad.accepted)),
	};
	const falseAcceptanceTasks = results.filter(t => t.falseAcceptance === true).map(t => t.workKey);
	const falseBlockingTasks = results.filter(t => t.falseBlocking === true).map(t => t.workKey);
	const nonAccepted = results.filter(t => t.status !== "accepted");
	const complete = ctx.errors.length === 0 && ctx.incomplete.length === 0;
	const summary: Rec = {
		version: TRIAL_VERSION,
		trial_id: trialId,
		pack,
		repos: trial.repos,
		complete,
		incomplete_reasons: ctx.incomplete,
		fixed_config: {
			held: config.held && violations.length === 0,
			violations,
			config: config.after,
			observed_models: observed.models,
			fallback_activations: observed.fallbackActivations,
		},
		fixed_config_success:
			complete && config.held && violations.length === 0 && results.every(t => t.status === "accepted"),
		coverage: {
			domains: Object.fromEntries(
				DOMAINS.map(d => [d, results.filter(t => t.domains.includes(d)).map(t => t.workKey)]),
			),
			non_omp: results.filter(t => t.nonOmp).map(t => t.workKey),
			missing: missingCoverage,
		},
		outcomes: {
			tasks: results.length,
			accepted: count("accepted"),
			failed: count("failed"),
			blocked: count("blocked"),
			abandoned: count("abandoned"),
			pending: count("pending"),
			not_accepted: nonAccepted.map(t => ({ work_key: t.workKey, status: t.status })),
		},
		false_acceptance: {
			tasks: falseAcceptanceTasks,
			known_bad_checks: knownBad.checks,
			known_bad_rejected: knownBad.rejected,
			known_bad_accepted: knownBad.accepted,
			unassessed: results.filter(t => t.falseAcceptance === null).map(t => t.workKey),
		},
		false_blocking: {
			tasks: falseBlockingTasks,
			unassessed: results.filter(t => t.falseBlocking === null).map(t => t.workKey),
		},
		usage: {
			total: rollupJson(total),
			primary: rollupJson(primary),
			supporting: rollupJson(supporting),
			unknown_tasks: results.filter(t => t.usageStatus === "unknown").map(t => t.workKey),
			partial_tasks: results.filter(t => t.usageStatus === "partial").map(t => t.workKey),
			supporting_on_non_accepted_tasks: sum(nonAccepted.map(t => t.supporting.usage.totalTokens)),
		},
		interventions: {
			count: sum(results.map(t => t.interventionCount)),
			minutes: sum(results.map(t => t.interventionMinutes)),
			unplanned_workflow_repairs: sum(results.map(t => t.unplannedRepairs)),
			unrecorded_tasks: results
				.filter(t => t.status !== "pending" && t.interventionCount === null)
				.map(t => t.workKey),
		},
		time: {
			elapsed_seconds_known: sum(results.map(t => t.elapsed)),
			elapsed_unknown_tasks: results.filter(t => t.elapsed === null).map(t => t.workKey),
			stalls: {
				count: sum(results.map(t => t.stallCount)),
				seconds: sum(results.map(t => t.stallSeconds)),
				unrecovered: sum(results.map(t => t.unrecoveredStalls)),
			},
		},
		workflow_growth: {
			unplanned_steps: sum(results.map(t => t.unplannedSteps)),
			mandatory_additions: sum(results.map(t => t.mandatoryAdditions)),
			unrecorded_tasks: results.filter(t => t.status !== "pending" && t.unplannedSteps === null).map(t => t.workKey),
		},
		retrieved_rules: sum(results.map(t => t.rules)),
		stop_conditions: stops,
		tasks: results.map(t => ({
			work_key: t.workKey,
			status: t.status,
			repo: t.repo,
			domains: t.domains,
			usage_status: t.usageStatus,
			usage: { primary: rollupJson(t.primary), supporting: rollupJson(t.supporting) },
			elapsed_seconds: t.elapsed,
			interventions: t.interventionCount,
			intervention_minutes: t.interventionMinutes,
		})),
	};
	return { errors: ctx.errors, incomplete: ctx.incomplete, summary };
}

function parseArgs(argv: readonly string[]): { cmd: string; root: string; id?: string; complete: boolean } | string {
	const [cmd, ...rest] = argv;
	if (cmd !== "init" && cmd !== "check" && cmd !== "summarize") return "unknown command";
	let root: string | undefined;
	let id: string | undefined;
	let complete = false;
	for (let i = 0; i < rest.length; i++) {
		const a = rest[i];
		if (a === "--root") root = rest[++i];
		else if (a === "--id") id = rest[++i];
		else if (a === "--complete") complete = true;
		else return `unknown argument ${a}`;
	}
	if (!root) return "--root DIR is required";
	return { cmd, root, id, complete };
}

function line(stream: NodeJS.WriteStream, text: string): void {
	stream.write(`${text}\n`);
}

export async function main(argv: readonly string[]): Promise<number> {
	const args = parseArgs(argv);
	if (typeof args === "string") {
		line(process.stderr, args);
		line(
			process.stderr,
			"usage: bun scripts/ecc-workflow-trial.ts init|check|summarize --root DIR [--id ID] [--complete]",
		);
		return 2;
	}
	if (args.cmd === "init") {
		try {
			await initTrial(args.root, args.id);
		} catch (e) {
			line(process.stderr, e instanceof Error ? e.message : String(e));
			return 1;
		}
		line(process.stdout, `initialised ${args.root}`);
		return 0;
	}
	const result = await evaluate(args.root);
	for (const e of result.errors) line(process.stderr, `error: ${e}`);
	if (result.errors.length > 0) return 1;
	for (const m of result.incomplete) line(process.stderr, `incomplete: ${m}`);
	if (args.cmd === "check") {
		if (args.complete && result.incomplete.length > 0) return 3;
		line(
			process.stdout,
			result.incomplete.length === 0 ? "ok: complete" : `ok: valid, ${result.incomplete.length} incomplete item(s)`,
		);
		return 0;
	}
	const out = path.join(args.root, "summary.json");
	await Bun.write(out, `${JSON.stringify(result.summary, null, "\t")}\n`);
	line(process.stdout, `wrote ${out}`);
	return 0;
}

if (import.meta.main) {
	process.exit(await main(process.argv.slice(2)));
}
