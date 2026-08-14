/**
 * linear-now — Linear weave for omp (owner page: linear.app/spec-kit, team HOME)
 *
 * - Injects the Linear bookend digest into context at session start (new + resume)
 * - NOW pointer: persistent footer line, /now //done //capture commands
 * - `linear` tool: bounded reads free, every write confirmed on screen first
 *
 * Truth for NOW lives in Linear as the single-holder `now` label; a local cache
 * keeps the footer instant and a session entry restores it across resume.
 * Everything fails open: Linear down = one honest line, never a blocked session.
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { type AuthStorage, completeSimple } from "@oh-my-pi/pi-ai";
import { discoverAuthStorage, getAgentDir, type ExtensionAPI, type ExtensionContext, type ExtensionModelQuery, type Theme } from "@oh-my-pi/pi-coding-agent";
import { Ellipsis, matchesKey, truncateToWidth, type TUI, visibleWidth, wrapTextWithAnsi } from "@oh-my-pi/pi-tui";

const TEAM_KEY = resolveMarker(".linear-team") ?? "HOME";
const NOW_LABEL = "now";
const QUEUE_LABEL = "waiting-on-chris";
const DRAIN_MAX_QUEUE = 8;
const DRAIN_MAX_AGE_DAYS = 14;
const KEY_FILE = join(homedir(), ".config", "linear.env");
const CACHE_FILE = join(homedir(), ".omp", "agent", TEAM_KEY === "HOME" ? "linear-now.json" : `linear-now-${TEAM_KEY.toLowerCase()}.json`);
const API = "https://api.linear.app/graphql";

export const WORKFLOW_SEQUENCE =
	"WORKFLOW SEQUENCE: /intake creates and selects → /plan approves, stamps, and executes → execution handoff → /summary reviews → /done closes. Linear tracks every stage; Chris never moves state, re-identifies the issue, or answers bookkeeping prompts.";

/** HOME-114: closeout is explicit-command only. */
export const CLOSEOUT_BOUNDARY =
	"Closeout is explicit-command only: /summary (questionyourself + whatsmissing + Linear close ritual) and /done run ONLY when Chris literally enters them — never start a summary, close proposal, health update, capture triage, or NOW handoff because work, a todo list, or the session looks finished; a keep-open verdict blocks every closeout action until he enters one.";
export const STOP_REMINDER_BOUNDARY =
	"Post the checkpoint silently. Never narrate bookkeeping or start /summary or /done.";
export const PLAN_APPROVED_PREFIX = "**Plan approved**";
export const EXECUTION_HANDOFF_PREFIX = "**Execution handoff**";
export const SESSION_REVIEW_PREFIX = "**Session review**";
export const CLOSEOUT_LOCK_REFUSAL =
	"REFUSED — closeout lock (HOME-114): no owner-entered /summary or /done this session. update_health, propose_close, and archive_issue are wrap-up writes; they unlock only when Chris literally enters /summary or /done. If he wants this write, he must enter one of those commands himself — do not retry on your own.";

/** Resolve a committed single-line marker file (e.g. .linear-project, .linear-team)
 *  from the git toplevel of cwd, falling back to the primary checkout root via
 *  --git-common-dir (covers worktrees whose branch predates the marker).
 *  No marker / non-git cwd → null. */
function resolveMarker(marker: string): string | null {
	const git = (...args: string[]): string | null => {
		const r = spawnSync("git", ["-C", process.cwd(), ...args], { encoding: "utf8" });
		return r.status === 0 ? r.stdout.trim() : null;
	};
	const roots: string[] = [];
	const top = git("rev-parse", "--show-toplevel");
	if (top) roots.push(top);
	const common = git("rev-parse", "--path-format=absolute", "--git-common-dir");
	if (common && basename(common) === ".git") {
		const primary = dirname(common);
		if (!roots.includes(primary)) roots.push(primary);
	}
	for (const root of roots) {
		try {
			const raw = readFileSync(join(root, marker), "utf8").trim();
			if (raw) return raw;
		} catch {
			/* next root */
		}
	}
	return null;
}

/** Run git in cwd; timeoutMs guards network ops (push). `raw` is untrimmed stdout —
 *  porcelain -z parsing needs the leading space of the first `XY path` entry. */
function runGit(cwd: string, args: string[], timeoutMs = 10_000): { ok: boolean; out: string; raw: string; err: string } {
	const r = spawnSync("git", ["-C", cwd, ...args], { encoding: "utf8", timeout: timeoutMs });
	const raw = r.stdout ?? "";
	return { ok: r.status === 0, out: raw.trim(), raw, err: (r.stderr ?? "").trim() };
}

/** Parse `git status --porcelain -z` output → workdir-relative dirty paths.
 *  NUL-separated; entries with X status R or C carry the ORIGINAL path as the
 *  next NUL token — skip it, keep the new path. Untracked (`??`) included. */
export function parsePorcelain(z: string): string[] {
	const tokens = z.split("\0").filter(t => t.length > 0);
	const paths: string[] = [];
	for (let i = 0; i < tokens.length; i++) {
		const entry = tokens[i];
		paths.push(entry.slice(3));
		if (entry[0] === "R" || entry[0] === "C") i++; // next token = original path; not stageable
	}
	return paths;
}

const SECRET_PATTERNS: [string, RegExp][] = [
	["linear-key", /lin_api_[A-Za-z0-9]/],
	["secret-key", /\bsk-[A-Za-z0-9_-]{16,}/],
	["aws-key", /\bAKIA[0-9A-Z]{16}\b/],
	["github-token", /\bgh[pousr]_[A-Za-z0-9]{20,}/],
	["slack-token", /\bxox[baprs]-/],
	["private-key", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
];

/** Scan ADDED lines of a cached diff for credential shapes → matched pattern names ([] = clean). */
export function findSecrets(diff: string): string[] {
	const hits = new Set<string>();
	for (const line of diff.split("\n")) {
		if (!line.startsWith("+") || line.startsWith("+++")) continue;
		for (const [name, re] of SECRET_PATTERNS) if (re.test(line)) hits.add(name);
	}
	return [...hits];
}

/** /done commit step (HOME-118): owner-confirmed exact-path staging, credential scan,
 *  commit `session close: <identifier>`, guarded push. Returns a one-line notice for
 *  pendingNotices, or null when nothing happened. NEVER throws. */
export async function commitSessionWork(
	ui: { confirm(title: string, body: string): Promise<boolean>; notify(msg: string, level?: "info" | "warning" | "error"): void },
	cwd: string,
	identifier: string,
): Promise<string | null> {
	try {
		const top = runGit(cwd, ["rev-parse", "--show-toplevel"]);
		if (!top.ok) {
			ui.notify("no git repo here — nothing to commit", "info");
			return null;
		}
		const root = top.out;
		const status = runGit(root, ["status", "--porcelain", "-z"]);
		const all = status.ok ? parsePorcelain(status.raw) : [];
		const excluded = all.filter(p => p.startsWith("packages/")); // Chris's own lane — never staged (HOME-117/118)
		const commitable = all.filter(p => !p.startsWith("packages/"));
		const leftAlone = excluded.length ? `left alone: ${excluded.length} file(s) under packages/ (yours)` : "";
		if (commitable.length === 0) {
			ui.notify(`nothing to commit${leftAlone ? ` — ${leftAlone}` : ""}`, "info");
			return null;
		}
		const listed = commitable.slice(0, 20).join("\n") + (commitable.length > 20 ? `\n+${commitable.length - 20} more` : "");
		const body = leftAlone ? `${listed}\n\n${leftAlone}` : listed;
		const yes = await ui.confirm(`Commit session work? (${commitable.length} files → session close: ${identifier})`, body);
		if (!yes) return null;
		const add = runGit(root, ["add", "--", ...commitable]); // exact paths only — never `git add .`
		if (!add.ok) {
			ui.notify(`commit failed at staging: ${add.err.split("\n")[0]}`, "error");
			return null;
		}
		const secrets = findSecrets(runGit(root, ["diff", "--cached"]).out);
		if (secrets.length) {
			runGit(root, ["reset", "-q", "--", ...commitable]);
			ui.notify(`commit refused — possible secret in staged diff: ${secrets.join(", ")}. Commit manually if false alarm.`, "error");
			return null;
		}
		const commit = runGit(root, ["commit", "-m", `session close: ${identifier}`]);
		if (!commit.ok) {
			runGit(root, ["reset", "-q", "--", ...commitable]);
			ui.notify(`commit failed: ${commit.err.split("\n")[0] || commit.out.split("\n")[0]}`, "error");
			return null;
		}
		const n = commitable.length;
		if (root === join(homedir(), ".claude")) return `[linear] /done committed ${n} file(s) on ${identifier} (not pushed — ~/.claude)`;
		if (!runGit(root, ["remote", "get-url", "origin"]).ok) return `[linear] /done committed ${n} file(s) on ${identifier} (not pushed — no remote)`;
		const push = runGit(root, ["push"], 30_000);
		if (!push.ok) {
			ui.notify(`committed; push failed: ${push.err.split("\n")[0]}`, "warning");
			return `[linear] /done committed ${n} file(s) on ${identifier} (not pushed — push failed)`;
		}
		return `[linear] /done committed and pushed ${n} file(s) for ${identifier}`;
	} catch (e) {
		ui.notify(`/done commit step failed: ${String(e)}`, "error");
		return null;
	}
}

interface NowState {
	team?: string; // owning team key; restore skips entries from another team (absent = legacy HOME)
	issueId?: string;
	identifier?: string;
	title?: string;
	project?: string;
	setAt?: number;
	lastDone?: { identifier: string; title: string; at: number };
	teamId?: string;
	nowLabelId?: string;
	doneStateId?: string;
	canceledStateId?: string;
	// HOME-122 workflow carrier. Linear comments are authoritative; this cache
	// preserves the active issue and hidden obligation across session replacement.
	executingIssue?: { id: string; identifier: string };
	approvedPlan?: { hash: string; at: number };
	obligationHandoff?: { armed: boolean; blockedOnce: boolean };
	obligationReview?: { armed: boolean; blockedOnce: boolean };
	treeCounts?: { done: number; total: number; stuck: number; onyou: number; at: number }; // HOME-109 footer cache — refreshed by every goalTree()
}

interface MapIssue {
	id: string;
	identifier: string;
	title: string;
	stateName: string;
	updatedAt: string;
	waiting: boolean;
	isNow: boolean;
	description?: string;
	labels: string[];
	project?: string;
}

interface MapSurface {
	name: string;
	health?: string;
	state: string;
	issues: MapIssue[];
	waiting: number;
}

interface IssueDetail {
	blockedBy: string[];
	blocks: string[];
	related: string[];
	comments: { at: string; author: string; head: string }[];
	commentsTotal: number;
	commentsLast7d: number;
	digestPacket: string;
}

interface WorkflowComment {
	body: string;
	createdAt: string;
}

interface WorkflowState {
	issue: { id: string; identifier: string; title: string; project?: string };
	plan?: { hash: string; at: string };
	handoff?: { at: string };
	review?: { hash: string; at: string };
}

interface TreeItem {
	identifier: string;
	title: string;
	bucket: "done" | "working" | "stuck" | "onyou" | "next";
	blocker?: string;
	isNow: boolean;
}

interface GoalTree {
	goal: string;
	health?: string;
	promise?: string;
	items: TreeItem[];
	counts: { done: number; total: number; stuck: number; onyou: number };
}

function apiKey(): string | null {
	try {
		const line = readFileSync(KEY_FILE, "utf8")
			.split("\n")
			.find(l => l.startsWith("LINEAR_API_KEY="));
		return line ? line.slice("LINEAR_API_KEY=".length).trim() : null;
	} catch {
		return null;
	}
}

async function gql<T = any>(query: string, variables?: Record<string, unknown>): Promise<T> {
	const key = apiKey();
	if (!key) throw new Error(`no key file at ${KEY_FILE}`);
	const res = await fetch(API, {
		method: "POST",
		headers: { Authorization: key, "Content-Type": "application/json" },
		body: JSON.stringify({ query, variables }),
		signal: AbortSignal.timeout(6000),
	});
	if (!res.ok) throw new Error(`Linear HTTP ${res.status}`);
	const body = (await res.json()) as { data?: T; errors?: { message: string }[] };
	if (body.errors?.length) throw new Error(`Linear: ${body.errors[0].message}`);
	if (!body.data) throw new Error("Linear: empty response");
	return body.data;
}

function fmtElapsed(ms: number): string {
	const m = Math.floor(ms / 60000);
	if (m < 60) return `${m}m`;
	return `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ""}`;
}

/**
 * Word-wrap plain text into at most maxLines lines of the given width.
 * Retained per the v3 plan's keep-machinery list. Currently uncalled: the pane
 * wraps content via pi-tui wrapTextWithAnsi instead, because this helper never
 * splits a word longer than `width` (a long URL yields an overlong line that
 * cell truncation would clip silently).
 */
function wrap(text: string, width: number, maxLines: number): string[] {
	const out: string[] = [];
	let line = "";
	for (const word of text.split(/\s+/)) {
		if (!word) continue;
		if (line && line.length + 1 + word.length > width) {
			out.push(line);
			if (out.length === maxLines) return out;
			line = word;
		} else {
			line = line ? `${line} ${word}` : word;
		}
	}
	if (line && out.length < maxLines) out.push(line);
	return out;
}

const HEALTH_GLYPH: Record<string, string> = { onTrack: "🟢", atRisk: "🟡", offTrack: "🔴" };
const STATE_BAND: Record<string, number> = { started: 0, planned: 1 };
const DONE_SUFFIX: Record<string, string> = { completed: " (done)", canceled: " (done)" };
// HOME-109 completion tree — glyph vocabulary is a v1 prototype pending owner by-seeing; revise here only.
const TREE_GLYPH: Record<TreeItem["bucket"], string> = { done: "✔", working: "▶", stuck: "✖", onyou: "✋", next: "○" };
const HEALTH_WORDS: Record<string, string> = { onTrack: "on track", atRisk: "at risk", offTrack: "off track" };

const DIGEST_INSTRUCTION = [
	"Analyze this Linear issue for Chris (non-engineer, first read). Output ONLY labeled lines in this order, plain language, no markdown, no preamble:",
	"PROBLEM: the problem statement — what is wrong or wanted and why the household cares (1-2 lines)",
	"MAKEUP: what the work is made of — the moving parts, scope, what done involves (1-3 lines)",
	'DECISIONS: every verdict/proof/retraction on record, dated, one line each, oldest first (max 5; exactly "DECISIONS: none recorded" if empty)',
	'BLOCKERS: what actually blocks this — linked blockers AND blockers described in prose ("BLOCKERS: none recorded" if none)',
	"ON-YOU: the exact question parked on Chris and since when — ONLY when the issue is marked waiting on him, else omit this line entirely",
	"EVIDENCE: commits, proofs, live-test results cited in the record, dated (max 3 lines; omit if none cited)",
	"STATE: where it stands right now (1 line)",
	"NEXT: the single next step (1 line)",
	"SUGGEST: your recommended action on this issue — one direct line",
].join("\n");

const DIGEST_LABELS = new Set(["PROBLEM", "MAKEUP", "DECISIONS", "BLOCKERS", "ON-YOU", "EVIDENCE", "STATE", "NEXT", "SUGGEST"]);

/**
 * Owner approval for writes. Human slash commands (//done, //capture) → on-screen
 * dialog. Model-initiated tool writes (forceTwoPhase) → ALWAYS two-phase, never
 * the dialog: the payload must land in the transcript for Chris to read, because
 * a mid-flow dialog invites a reflexive approve and leaves no auditable record
 * (2026-08-10 post-restart incident: propose_close wrote on the first call —
 * owner vetoed the dialog path for tool writes). First call writes NOTHING and
 * returns the payload preview; the repeat call with confirm:true writes.
 */
async function ownerGate(
	ctx: ExtensionContext,
	title: string,
	detail: string,
	confirmed: boolean | undefined,
	forceTwoPhase = false,
): Promise<{ approved: boolean; preview?: string }> {
	// ctx.ui is typed present but absent at runtime in agent tool-execute context
	// (the HOME-30 walkthrough crash) — guard, don't trust the type.
	if (!forceTwoPhase && ctx.ui && typeof ctx.ui.confirm === "function") {
		try {
			return { approved: await ctx.ui.confirm(title, detail) };
		} catch {
			/* dialog unavailable after all — fall through to two-phase */
		}
	}
	if (confirmed) return { approved: true };
	return {
		approved: false,
		preview: [
			"CONFIRM REQUIRED — nothing written.",
			"",
			title,
			detail,
			"",
			"Show this to Chris verbatim. If he says yes, call again with the same arguments plus confirm:true.",
		].join("\n"),
	};
}

export default function linearNow(pi: ExtensionAPI) {
	const projectFilter = resolveMarker(".linear-project"); // exact Linear project name, one line

	// Day-1 scoping, enforced natively (owner law: enforcement/piping is omp-native,
	// never LLM-only rules): the digest states scope every session, session start
	// notifies when unscoped, and writes from an unscoped git repo (no marker, no
	// explicit project, NOW carries none) are REFUSED with the scoping instructions.
	// NOW stays global (single-holder label); .linear-project scopes /now + filing only.
	const gitRooted = spawnSync("git", ["-C", process.cwd(), "rev-parse", "--show-toplevel"], { encoding: "utf8" }).status === 0;
	const SCOPE_FIX = 'create the project in Linear first, then echo "<Exact Project Name>" > .linear-project at the repo root';
	const scopeLine = !gitRooted
		? "SCOPE: not a git repo — /now unfiltered, filing lands on team HOME; git init + .linear-project to scope this directory"
		: projectFilter
			? `SCOPE: "${projectFilter}" (.linear-project) — /now + filing scoped to this project; NOW itself stays global`
			: `SCOPE: no .linear-project marker — /now unfiltered; unscoped writes refused. Day 1: ${SCOPE_FIX}`;
	/** Native write gate: null = scoped, string = refusal reason. */
	function unscopedRefusal(target: string | undefined): string | null {
		if (target || !gitRooted) return null;
		return `refused: unscoped git repo (no .linear-project marker, no explicit project) — ${SCOPE_FIX}`;
	}
	/** Filing target: explicit param wins, then the repo marker. The global NOW's project
	 *  (state.project) is NOT an implicit target inside a git repo — it would silently
	 *  route this repo's work into whatever project NOW happens to live in. Outside git
	 *  repos (scratch dirs, no marker possible) the NOW fallback still applies. */
	function fileTarget(explicit: string | undefined): string | undefined {
		return explicit ?? projectFilter ?? (gitRooted ? undefined : state.project);
	}
	let state: NowState = {};
	let digestPending = false;
	let digestInjectedThisSession = false;
	let intakeActive = false;
	let intakeSelected = false;
	let planTarget: { id: string; identifier: string; title: string; project?: string } | undefined;
	let summaryAuthorized = false;
	/** Validate the exact marker name against Linear; an empty issue list cannot
	 *  distinguish an empty project from a stale/nonexistent marker. */
	async function projectScopeExists(): Promise<boolean> {
		if (!projectFilter) return true;
		const data = await gql<{ projects: { nodes: { name: string }[] } }>(
			`query($name:String!){ projects(first:10,filter:{name:{eq:$name}}){nodes{name}} }`,
			{ name: projectFilter },
		);
		return data.projects.nodes.some(project => project.name === projectFilter);
	}
	let models: ExtensionModelQuery | undefined;

	async function loadCache() {
		try {
			state = JSON.parse(await readFile(CACHE_FILE, "utf8"));
		} catch {
			state = {};
		}
	}
	async function saveCache() {
		try {
			await writeFile(CACHE_FILE, JSON.stringify(state, null, 2));
		} catch (e) {
			pi.logger.warn("linear-now: cache write failed", { error: String(e) });
		}
	}
	function persistSession() {
		pi.appendEntry("linear-now", {
			team: TEAM_KEY,
			issueId: state.issueId,
			identifier: state.identifier,
			title: state.title,
			project: state.project,
			setAt: state.setAt,
			executingIssue: state.executingIssue,
			approvedPlan: state.approvedPlan,
			obligationHandoff: state.obligationHandoff,
			obligationReview: state.obligationReview,
		});
	}

	// ---- HOME-122 workflow carrier ----

	function armExecution(issue: { id: string; identifier: string }, hash: string) {
		state.executingIssue = issue;
		state.approvedPlan = { hash, at: Date.now() };
		state.obligationHandoff = { armed: true, blockedOnce: false };
		state.obligationReview = undefined;
		persistSession();
		void saveCache();
	}

	function settleCheckpoint(issueId: string, kind: "handoff" | "review", ctx: ExtensionContext) {
		if (state.executingIssue?.id !== issueId) return;
		if (kind === "review") {
			state.obligationHandoff = undefined;
			state.obligationReview = undefined;
		} else {
			state.obligationHandoff = undefined;
		}
		persistSession();
		void saveCache();
		footer(ctx);
	}

	async function teamId(): Promise<string> {
		if (state.teamId) return state.teamId;
		const d = await gql<{ teams: { nodes: { id: string; key: string }[] } }>(
			`query($key:String!){ teams(filter:{key:{eq:$key}}){nodes{id key}} }`,
			{ key: TEAM_KEY },
		);
		const t = d.teams.nodes[0];
		if (!t) throw new Error(`team ${TEAM_KEY} not found`);
		state.teamId = t.id;
		await saveCache();
		return t.id;
	}

	async function nowLabelId(): Promise<string> {
		if (state.nowLabelId) return state.nowLabelId;
		const d = await gql<{ issueLabels: { nodes: { id: string; name: string }[] } }>(
			`query($name:String!,$team:String!){ issueLabels(filter:{name:{eq:$name},team:{key:{eq:$team}}}){nodes{id name}} }`,
			{ name: NOW_LABEL, team: TEAM_KEY },
		);
		let id = d.issueLabels.nodes[0]?.id;
		if (!id) {
			const created = await gql<{ issueLabelCreate: { issueLabel: { id: string } } }>(
				`mutation($input:IssueLabelCreateInput!){ issueLabelCreate(input:$input){issueLabel{id}} }`,
				{ input: { name: NOW_LABEL, teamId: await teamId(), color: "#f2c94c" } },
			);
			id = created.issueLabelCreate.issueLabel.id;
		}
		state.nowLabelId = id;
		await saveCache();
		return id;
	}

	async function queueLabelId(): Promise<string | undefined> {
		const d = await gql<{ issueLabels: { nodes: { id: string }[] } }>(
			`query($name:String!,$team:String!){ issueLabels(filter:{name:{eq:$name},team:{key:{eq:$team}}}){nodes{id}} }`,
			{ name: QUEUE_LABEL, team: TEAM_KEY },
		);
		return d.issueLabels.nodes[0]?.id;
	}

	async function stateIdFor(kind: "completed" | "canceled"): Promise<string> {
		const cached = kind === "canceled" ? state.canceledStateId : state.doneStateId;
		if (cached) return cached;
		const d = await gql<{ teams: { nodes: { states: { nodes: { id: string; name: string; type: string }[] } }[] } }>(
			`query($key:String!){ teams(filter:{key:{eq:$key}}){nodes{states(first:20){nodes{id name type}}}} }`,
			{ key: TEAM_KEY },
		);
		const s = d.teams.nodes[0]?.states.nodes.find(n => n.type === kind);
		if (!s) throw new Error(`no ${kind} workflow state on team ${TEAM_KEY}`);
		if (kind === "canceled") state.canceledStateId = s.id;
		else state.doneStateId = s.id;
		await saveCache();
		return s.id;
	}

	/** ONE bounded request for the /now MAP — never query per-issue (free-plan rate). */
	async function mapData(): Promise<{ surfaces: MapSurface[]; capped: boolean }> {
		const d = await gql<{
			projects: { nodes: { name: string; state: string; health?: string }[] };
			issues: {
				nodes: {
					id: string;
					identifier: string;
					title: string;
					description?: string;
					updatedAt: string;
					state: { name: string };
					project?: { name: string };
					labels: { nodes: { name: string }[] };
				}[];
			};
		}>(
			`query($team:String!){ projects(first:50){nodes{name state health}} issues(first:100,filter:{team:{key:{eq:$team}},state:{type:{nin:["completed","canceled"]}}},orderBy:updatedAt){nodes{id identifier title description updatedAt state{name} project{name} labels(first:10){nodes{name}}}} }`,
			{ team: TEAM_KEY },
		);
		const NO_SURFACE = "(no surface)";
		const byProject = new Map<string, MapIssue[]>();
		for (const n of d.issues.nodes) {
			const key = n.project?.name ?? NO_SURFACE;
			const list = byProject.get(key) ?? [];
			list.push({
				id: n.id,
				identifier: n.identifier,
				title: n.title,
				stateName: n.state.name,
				updatedAt: n.updatedAt,
				waiting: n.labels.nodes.some(l => l.name === QUEUE_LABEL),
				isNow: n.identifier === state.identifier,
				description: n.description,
				labels: n.labels.nodes.map(l => l.name),
				project: n.project?.name,
			});
			byProject.set(key, list);
		}
		const known = new Map(d.projects.nodes.map(p => [p.name, p]));
		const names = new Set<string>([...d.projects.nodes.filter(p => p.state === "started").map(p => p.name), ...byProject.keys()]);
		names.delete(NO_SURFACE);
		const surfaces: MapSurface[] = [...names]
			.map(name => {
				const p = known.get(name);
				const issues = byProject.get(name) ?? [];
				return { name, health: p?.health, state: p?.state ?? "?", issues, waiting: issues.filter(i => i.waiting).length };
			})
			.sort((a, b) => (STATE_BAND[a.state] ?? 2) - (STATE_BAND[b.state] ?? 2) || a.name.localeCompare(b.name));
		const orphans = byProject.get(NO_SURFACE);
		if (orphans) surfaces.push({ name: NO_SURFACE, state: "?", issues: orphans, waiting: orphans.filter(i => i.waiting).length });
		if (projectFilter) return { surfaces: surfaces.filter(s => s.name === projectFilter), capped: d.issues.nodes.length === 100 };
		return { surfaces, capped: d.issues.nodes.length === 100 };
	}

	/** HOME-109 completion tree — ONE bounded request (mapData's law, never per-issue):
	 *  the current goal (NOW's project) at root, every project issue as a branch in
	 *  exactly one plain-words bucket. */
	async function goalTree(): Promise<GoalTree | null> {
		if (!state.project || !state.identifier) return null;
		const d = await gql<{
			issues: {
				nodes: {
					identifier: string;
					title: string;
					state: { name: string; type: string };
					labels: { nodes: { name: string }[] };
					inverseRelations: { nodes: { type: string; issue: { identifier: string; title: string; state: { type: string } } }[] };
				}[];
			};
			projects: { nodes: { name: string; health?: string; projectMilestones: { nodes: { name: string }[] } }[] };
		}>(
			`query($team:String!,$project:String!){ issues(first:100,orderBy:updatedAt,filter:{team:{key:{eq:$team}},project:{name:{eq:$project}}}){nodes{identifier title state{name type} labels(first:10){nodes{name}} inverseRelations(first:10){nodes{type issue{identifier title state{type}}}}}} projects(first:10,filter:{name:{eq:$project}}){nodes{name health projectMilestones(first:5){nodes{name}}}} }`,
			{ team: TEAM_KEY, project: state.project },
		);
		const proj = d.projects.nodes[0];
		const items: TreeItem[] = d.issues.nodes.map(n => {
			const isNow = n.identifier === state.identifier;
			const blocked = n.inverseRelations.nodes.find(r => r.type === "blocks" && !DONE_SUFFIX[r.issue.state.type]);
			// First match wins — exactly one bucket per item (by-seeing verdict 2,
			// 2026-08-14: NOW forces working even from Backlog; stuck/onyou still
			// outrank it — they are the "what's blocking" signal). isNow also
			// drives the "← working now" arrow in the render.
			const bucket: TreeItem["bucket"] = DONE_SUFFIX[n.state.type]
				? "done"
				: n.labels.nodes.some(l => l.name === QUEUE_LABEL)
					? "onyou"
					: blocked
						? "stuck"
						: isNow || n.state.type === "started"
							? "working"
							: "next";
			return { identifier: n.identifier, title: n.title, bucket, blocker: bucket === "stuck" ? blocked?.issue.title : undefined, isNow };
		});
		const counts = {
			done: items.filter(i => i.bucket === "done").length,
			total: items.length,
			stuck: items.filter(i => i.bucket === "stuck").length,
			onyou: items.filter(i => i.bucket === "onyou").length,
		};
		state.treeCounts = { ...counts, at: Date.now() };
		void saveCache();
		return { goal: state.project, health: proj?.health, promise: proj?.projectMilestones.nodes[0]?.name, items, counts };
	}

	/** Deterministic plain-words render — zero LLM, zero paths/hashes. Identifiers
	 *  stay: they are the owner's own handles, visible on his Linear. */
	function renderGoalTree(t: GoalTree): string[] {
		const lines = [`GOAL: ${t.goal}${t.promise ? ` — ${t.promise}` : ""}${t.health ? ` [${HEALTH_WORDS[t.health] ?? t.health}]` : ""}`];
		const by = (b: TreeItem["bucket"]) => t.items.filter(i => i.bucket === b);
		for (const i of by("working")) lines.push(`  ${TREE_GLYPH.working} ${i.identifier} ${i.title}${i.isNow ? " ← working now" : ""}`);
		for (const i of by("stuck")) lines.push(`  ${TREE_GLYPH.stuck} ${i.identifier} ${i.title} — stuck: ${i.blocker}`);
		for (const i of by("onyou")) lines.push(`  ${TREE_GLYPH.onyou} ${i.identifier} ${i.title} — waiting on you`);
		const nowTitle = t.items.find(i => i.isNow)?.title ?? state.title ?? "";
		const parts = [`${t.counts.done} of ${t.counts.total} pieces done.`];
		if (nowTitle) parts.push(`Working on: ${nowTitle}.`);
		if (t.counts.stuck) parts.push(`${t.counts.stuck} stuck.`);
		if (t.counts.onyou) parts.push(`${t.counts.onyou} waiting on you.`);
		const queued = by("next").length;
		if (queued) parts.push(`${queued} queued next.`);
		lines.push(parts.join(" "));
		return lines;
	}

	/** ONE bounded request for an issue's history — card raw fields + the digest packet. */
	async function fetchIssueDetail(ref: string): Promise<IssueDetail> {
		const d = await gql<{
			issue: {
				identifier: string;
				title: string;
				description?: string;
				updatedAt: string;
				state: { name: string };
				project?: { name: string; health?: string };
				labels: { nodes: { name: string }[] };
				comments: { nodes: { body: string; createdAt: string; user?: { name: string } }[] };
				relations: { nodes: { type: string; relatedIssue: { identifier: string; title: string; state: { type: string } } }[] };
				inverseRelations: { nodes: { type: string; issue: { identifier: string; title: string; state: { type: string } } }[] };
			};
		}>(
			`query($id:String!){ issue(id:$id){ identifier title description updatedAt state{name} project{name health} labels(first:10){nodes{name}} comments(first:50){nodes{body createdAt user{name}}} relations(first:20){nodes{type relatedIssue{identifier title state{type}}}} inverseRelations(first:20){nodes{type issue{identifier title state{type}}}} } }`,
			{ id: ref },
		);
		const i = d.issue;
		const sorted = [...i.comments.nodes].sort((a, b) => a.createdAt.localeCompare(b.createdAt));
		const last20 = sorted.slice(-20).map(c => ({ at: c.createdAt, author: c.user?.name ?? "?", head: c.body.slice(0, 400) }));
		const weekAgo = Date.now() - 7 * 24 * 3600_000;
		const blockedBy = i.inverseRelations.nodes.filter(r => r.type === "blocks").map(r => `${r.issue.identifier} ${r.issue.title}${DONE_SUFFIX[r.issue.state.type] ?? ""}`);
		const blocks = i.relations.nodes.filter(r => r.type === "blocks").map(r => `${r.relatedIssue.identifier} ${r.relatedIssue.title}${DONE_SUFFIX[r.relatedIssue.state.type] ?? ""}`);
		const related = [
			...i.relations.nodes.filter(r => r.type !== "blocks").map(r => `${r.type}: ${r.relatedIssue.identifier} ${r.relatedIssue.title}`),
			...i.inverseRelations.nodes.filter(r => r.type !== "blocks").map(r => `${r.type}: ${r.issue.identifier} ${r.issue.title}`),
		];
		const digestPacket = [
			`${i.identifier} ${i.title}`,
			`surface: ${i.project?.name ?? "none"}${i.project?.health ? ` [${i.project.health}]` : ""} · state: ${i.state.name} · labels: ${i.labels.nodes.map(l => l.name).join(",") || "none"} · updated: ${i.updatedAt.slice(0, 10)}`,
			"DESCRIPTION:",
			(i.description ?? "(none)").slice(0, 3000),
			"COMMENTS (oldest→newest, last 20):",
			last20.map(c => `[${c.at.slice(0, 10)} ${c.author}] ${c.head}`).join("\n") || "(no comments)",
			`BLOCKED BY:\n${blockedBy.join("\n") || "(none recorded)"}`,
			`BLOCKS:\n${blocks.join("\n") || "(none recorded)"}`,
			...(related.length ? [`RELATED:\n${related.join("\n")}`] : []),
		].join("\n");
		return {
			blockedBy,
			blocks,
			related,
			comments: last20,
			commentsTotal: sorted.length,
			commentsLast7d: sorted.filter(c => Date.parse(c.createdAt) > weekAgo).length,
			digestPacket,
		};
	}

	// ---- in-card digest engine (owner ruling R4: auto per highlighted issue) ----

	const detailCache = new Map<string, IssueDetail>();
	const digestCache = new Map<string, { updatedAt: string; lines: string[] }>();
	let digestAuthStorage: Promise<AuthStorage> | undefined;

	function digestApiKey(provider: string): Promise<string | undefined> {
		// Cache the storage DISCOVERY, never the key: getApiKey() refreshes expired
		// OAuth bearers on each call, and antigravity/etc. tokens rotate hourly — a
		// session-lifetime key cache serves a dead bearer to every digest after ~1h.
		// A failed discovery evicts itself so one hiccup can't poison the session.
		if (!digestAuthStorage) {
			const p = discoverAuthStorage(getAgentDir());
			digestAuthStorage = p;
			p.catch(() => {
				if (digestAuthStorage === p) digestAuthStorage = undefined;
			});
		}
		return digestAuthStorage.then(storage => storage.getApiKey(provider));
	}

	async function digestFor(issue: MapIssue, detail: IssueDetail, signal: AbortSignal): Promise<string[]> {
		const cached = digestCache.get(issue.id);
		if (cached && cached.updatedAt === issue.updatedAt) return cached.lines;
		const model = models?.resolve("@smol");
		if (!model) throw new Error("no smol model configured (@smol role) — digests never fall through to the session model");
		const key = await digestApiKey(model.provider);
		if (!key) throw new Error(`no credentials for ${model.provider}`);
		const res = await completeSimple(
			model,
			{ messages: [{ role: "user", content: `${DIGEST_INSTRUCTION}\nwaiting-on-chris label: ${issue.waiting ? "yes" : "no"}\n── issue ──\n${detail.digestPacket}`, timestamp: Date.now() }] },
			{ apiKey: key, disableReasoning: true, signal },
		);
		// pi-ai surfaces provider failures IN-BAND (stopReason "error"/"aborted" +
		// errorMessage, content empty) — completeSimple does not throw. Without this
		// guard every provider error (429 quota, auth refresh race, thinking-loop
		// retry marker) collapses into a lying "digest came back empty".
		if (res.stopReason === "error" || res.stopReason === "aborted") {
			throw new Error(res.errorMessage || `${model.provider}/${model.id} call ${res.stopReason} with no detail`);
		}
		const text = res.content
			.filter(c => c.type === "text")
			.map(c => c.text)
			.join("\n");
		const all = text.split("\n").map(l => l.trim()).filter(Boolean);
		const labelOf = (l: string) => {
			const m = /^([A-Z][A-Z-]*):/.exec(l);
			return m && DIGEST_LABELS.has(m[1]) ? m[1] : undefined;
		};
		const first = all.findIndex(l => labelOf(l) !== undefined);
		// Per-section line caps (label line + continuations). Overflow trims WITHIN a
		// section only — a later section is never dropped (owner contract: every
		// labeled section survives; DECISIONS max 5 / EVIDENCE max 3 per instruction).
		// Only KNOWN contract labels start a section — model prose like "URL:" is a
		// continuation line of the current section, never a section reset.
		const lines: string[] = [];
		const seen = new Set<string>();
		let section = "";
		let sectionLines = 0;
		for (const line of first >= 0 ? all.slice(first) : all) {
			const l = labelOf(line);
			if (l) {
				section = l;
				seen.add(l);
				sectionLines = 0;
			}
			const cap = section === "DECISIONS" ? 5 : 3;
			if (sectionLines < cap) {
				lines.push(line);
				sectionLines++;
			}
		}
		if (!lines.length) throw new Error("digest came back empty");
		// Contract validation BEFORE caching — a bad reply is never cached; the pane
		// shows the explicit "✦ digest unavailable (…)" line instead (law 12).
		const required = ["PROBLEM", "MAKEUP", "DECISIONS", "BLOCKERS", "STATE", "NEXT", "SUGGEST"];
		if (issue.waiting) required.push("ON-YOU");
		const missing = required.filter(s => !seen.has(s));
		if (missing.length) throw new Error(`digest missing required section(s): ${missing.join(", ")}`);
		if (!issue.waiting && seen.has("ON-YOU")) throw new Error("digest included ON-YOU for an issue not parked on Chris");
		digestCache.set(issue.id, { updatedAt: issue.updatedAt, lines });
		return lines;
	}

	// ---- the NOW window (owner rulings R1-R5: one screen, card on highlight, enter→confirm→NOW) ----

	function nowWindowFactory(map: { surfaces: MapSurface[]; capped: boolean }) {
		return (tui: TUI, theme: Theme, _kb: unknown, done: (r: MapIssue | undefined) => void) => {
			type Row = { kind: "header"; s: MapSurface } | { kind: "issue"; i: MapIssue };
			const rows: Row[] = [];
			for (const s of map.surfaces) {
				rows.push({ kind: "header", s });
				for (const i of s.issues) rows.push({ kind: "issue", i });
			}
			const issueRows: { rowIdx: number; issue: MapIssue }[] = [];
			rows.forEach((r, idx) => {
				if (r.kind === "issue") issueRows.push({ rowIdx: idx, issue: r.i });
			});
			let cursor = Math.max(0, issueRows.findIndex(e => e.issue.isNow));
			let detail: IssueDetail | undefined;
			let detailErr: string | undefined;
			let detailPending = true;
			let digest: string[] | undefined;
			let digestErr: string | undefined;
			let digestPending = true;
			let dwell: NodeJS.Timeout | undefined;
			let inflight: AbortController | undefined;
			let gen = 0;
			let listTop = 0; // first visible left-column row; cursor kept in view
			let paneScroll = 0; // →/← page offset INTO oversized pane content; reset on every highlight

			const current = () => issueRows[cursor].issue;

			function arm() {
				clearTimeout(dwell);
				inflight?.abort();
				gen++; // ALWAYS invalidate: a still-running load() from the previous highlight must never paint this one
				paneScroll = 0;
				const issue = current();
				detail = detailCache.get(issue.id);
				detailErr = digestErr = undefined;
				detailPending = !detail;
				const dig = digestCache.get(issue.id);
				digest = dig && dig.updatedAt === issue.updatedAt ? dig.lines : undefined;
				digestPending = !digest;
				if (detail && digest) return;
				const g = gen;
				dwell = setTimeout(() => void load(g), 350);
			}

			async function load(g: number) {
				const issue = current();
				inflight = new AbortController();
				try {
					let det = detailCache.get(issue.id);
					if (!det) {
						det = await fetchIssueDetail(issue.identifier);
						detailCache.set(issue.id, det);
					}
					if (g !== gen) return;
					detail = det;
					detailPending = false;
					tui.requestRender();
					if (!digest) {
						const lines = await digestFor(issue, det, inflight.signal);
						if (g !== gen) return;
						digest = lines;
						digestPending = false;
					}
					tui.requestRender();
				} catch (e) {
					if (g !== gen) return;
					if (!detail) {
						detailErr = String(e);
						detailPending = false;
						digestPending = false;
					} else {
						digestErr = String(e);
						digestPending = false;
					}
					tui.requestRender();
				}
			}

			function cleanup() {
				if (dwell) clearTimeout(dwell);
				inflight?.abort();
				gen++;
			}

			arm();

			return {
				render(width: number): string[] {
					// R1-v3 split layout (owner ruling 2026-08-11): static one-line list rows
					// LEFT, framed detail pane RIGHT consuming all remaining width, following
					// the highlight. List geometry never changes on arrow press.
					const w = Math.max(60, width);
					const listW = Math.min(48, Math.max(24, Math.floor(w * 0.38)));
					const paneW = w - listW - 1;
					const innerW = paneW - 4;
					const vh = Math.max(12, (process.stdout.rows ?? 40) - 6);
					const i = current();

					// Left column: plain cell → exact-width pad → style the whole cell (ANSI
					// is zero-width; emoji handled by truncateToWidth's width tables).
					const cells = rows.map((row, r) => {
						if (row.kind === "header") {
							const s = row.s;
							return truncateToWidth(`${HEALTH_GLYPH[s.health ?? ""] ?? "◇"} ${s.name} · ${s.issues.length} open${s.waiting ? ` · ⏳${s.waiting} on Chris` : ""}`, listW, Ellipsis.Unicode, true);
						}
						const it = row.i;
						const isCur = issueRows[cursor].rowIdx === r;
						const cell = truncateToWidth(`  ${isCur ? "❯ " : "  "}${it.isNow ? "◆ " : ""}${it.identifier} · ${it.title}${it.waiting ? " ⏳" : ""}`, listW, Ellipsis.Unicode, true);
						return isCur ? cell : theme.fg("dim", cell);
					});
					const curRow = issueRows[cursor].rowIdx;
					if (curRow < listTop) listTop = Math.max(0, curRow - 1);
					if (curRow >= listTop + vh) listTop = curRow - vh + 1;
					listTop = Math.max(0, Math.min(listTop, Math.max(0, rows.length - vh)));
					const blankCell = " ".repeat(listW);
					const left: string[] = [];
					for (let r = listTop; r < listTop + vh; r++) left.push(cells[r] ?? blankCell);

					// Right pane content, each line pre-wrapped to the frame interior.
					const content: string[] = [];
					const push = (line: string) => {
						for (const wl of wrapTextWithAnsi(line, innerW)) content.push(wl);
					};
					push(theme.fg("muted", `${i.stateName} · updated ${fmtElapsed(Date.now() - Date.parse(i.updatedAt))} ago · ${i.labels.join(",") || "no labels"}`));
					if (detailErr) {
						push(theme.fg("error", `⚠ history unavailable (${detailErr})`));
					} else if (detailPending || !detail) {
						push(theme.fg("dim", "…loading history"));
					} else {
						const lastC = detail.comments[detail.comments.length - 1];
						push(
							`${detail.commentsTotal >= 20 ? "20+" : detail.commentsTotal} comments · ${detail.commentsLast7d} this week · ` +
								(lastC ? `last: [${lastC.at.slice(5, 10)} ${lastC.author}] ${lastC.head.slice(0, 60)}` : "(no comments)"),
						);
					}
					content.push("");
					// Whitespace collapsed for paragraph flow; wrapTextWithAnsi hard-splits
					// words longer than innerW (long URLs) so the body-cell truncation never
					// silently clips content. Cap ≤6 lines.
					const desc = (i.description ?? "").slice(0, 480).replace(/\s+/g, " ").trim();
					if (desc) for (const l of wrapTextWithAnsi(desc, innerW).slice(0, 6)) content.push(l);
					else content.push(theme.fg("dim", "(no description)"));
					if (detail) {
						push(`blocked by: ${detail.blockedBy.join(", ") || "none recorded"}`);
						if (detail.blocks.length) push(`unblocks: ${detail.blocks.join(", ")}`);
					}
					content.push("");
					if (detailErr) {
						/* no digest line — the history-unavailable error above covers the whole fetch chain */
					} else if (digestErr) push(theme.fg("warning", `✦ digest unavailable (${digestErr})`));
					else if (digestPending || !digest) push(theme.fg("dim", "✦ digesting…"));
					else {
						for (const l of digest) {
							const m = /^([A-Z][A-Z-]*):/.exec(l);
							if (m && DIGEST_LABELS.has(m[1])) push(theme.bold(theme.fg("accent", `${m[1]}:`)) + l.slice(m[0].length));
							else push(l);
						}
					}

					// Frame the pane: exactly vh lines, frame chars border-colored, body text
					// as styled above (hierarchy: only frame + hints are dim/border).
					const bd = (s: string) => theme.fg("border", s);
					const bodyH = vh - 2;
					const paneOverflow = Math.max(0, content.length - bodyH);
					paneScroll = Math.min(paneScroll, paneOverflow);
					const head = truncateToWidth(`┌─ ${i.identifier} · ${i.title} `, paneW - 1, Ellipsis.Unicode);
					const pane: string[] = [bd(head + "─".repeat(Math.max(0, paneW - 1 - visibleWidth(head))) + "┐")];
					for (const line of content.slice(paneScroll, paneScroll + bodyH)) pane.push(bd("│ ") + truncateToWidth(line, innerW, Ellipsis.Omit, true) + bd(" │"));
					while (pane.length < vh - 1) pane.push(bd("│ ") + " ".repeat(innerW) + bd(" │"));
					pane.push(bd(`└${"─".repeat(Math.max(0, paneW - 2))}┘`));

					const out: string[] = [];
					for (let r = 0; r < vh; r++) out.push(`${left[r]} ${pane[r]}`);
					out.push(
						theme.fg(
							"dim",
							`enter = make it NOW · esc = close${paneOverflow > 0 ? ` · →/← page details (+${paneOverflow - paneScroll} below)` : ""}${map.capped ? " · ⚠ 100-issue cap" : ""}`,
						),
					);
					return out;
				},
				handleInput(data: string) {
					if (matchesKey(data, "up")) {
						if (cursor > 0) {
							cursor--;
							arm();
							tui.requestRender();
						}
						return;
					}
					if (matchesKey(data, "down")) {
						if (cursor < issueRows.length - 1) {
							cursor++;
							arm();
							tui.requestRender();
						}
						return;
					}
					if (matchesKey(data, "pageDown") || matchesKey(data, "right")) {
						paneScroll += 5;
						tui.requestRender();
						return;
					}
					if (matchesKey(data, "pageUp") || matchesKey(data, "left")) {
						paneScroll = Math.max(0, paneScroll - 5);
						tui.requestRender();
						return;
					}
					if (matchesKey(data, "enter") || matchesKey(data, "return")) {
						const picked = current();
						cleanup();
						done(picked);
						return;
					}
					if (matchesKey(data, "escape")) {
						cleanup();
						done(undefined);
					}
				},
				dispose() {
					cleanup();
				},
			};
		};
	}

	async function labelHolder(): Promise<{ id: string; identifier: string; title: string; project?: string } | null> {
		const d = await gql<{ issues: { nodes: { id: string; identifier: string; title: string; project?: { name: string } }[] } }>(
			`query($label:String!,$team:String!){ issues(first:2,filter:{team:{key:{eq:$team}},labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{id identifier title project{name}}} }`,
			{ label: NOW_LABEL, team: TEAM_KEY },
		);
		const n = d.issues.nodes[0];
		return n ? { id: n.id, identifier: n.identifier, title: n.title, project: n.project?.name } : null;
	}

	async function findIssue(ref: string): Promise<{ id: string; identifier: string; title: string; project?: { name: string } }> {
		const d = await gql<{ issue: { id: string; identifier: string; title: string; project?: { name: string } } }>(
			`query($id:String!){ issue(id:$id){id identifier title project{name}} }`,
			{ id: ref.trim() },
		);
		return d.issue;
	}

	function sectionItems(content: string, heading: "Approach" | "Verification"): string[] {
		const lines = content.split("\n");
		const start = lines.findIndex(line => line.trim() === `## ${heading}`);
		if (start < 0) return [];
		const items: string[] = [];
		for (const line of lines.slice(start + 1)) {
			if (/^##\s+/.test(line)) break;
			const match = line.match(/^(?:\d+\.|-)\s+(.+)$/);
			if (match) items.push(match[1].trim());
		}
		return items;
	}

	function preparePlanStamp(plan: { planFilePath: string; planContent: string; title: string }):
		| { hash: string; body: string }
		| { reason: string } {
		const approach = sectionItems(plan.planContent, "Approach");
		const verification = sectionItems(plan.planContent, "Verification");
		if (approach.length === 0 || verification.length === 0) {
			return { reason: "Plan approval requires non-empty ## Approach and ## Verification lists." };
		}
		const hash = Bun.SHA256.hash(plan.planContent, "hex");
		return {
			hash,
			body: [
				PLAN_APPROVED_PREFIX,
				"",
				`# ${plan.title}`,
				`- Plan: \`${plan.planFilePath}\``,
				`- SHA-256: \`${hash}\``,
				"",
				"## Approach",
				...approach.map((item, index) => `${index + 1}. ${item}`),
				"",
				"## Verification",
				...verification.map((item, index) => `${index + 1}. ${item}`),
			].join("\n"),
		};
	}

	async function workflowState(ref: string): Promise<WorkflowState> {
		const data = await gql<{
			issue: {
				id: string;
				identifier: string;
				title: string;
				project?: { name: string };
				comments: { nodes: WorkflowComment[] };
			};
		}>(
			`query($id:String!){ issue(id:$id){id identifier title project{name} comments(last:50){nodes{body createdAt}}} }`,
			{ id: ref },
		);
		const issue = data.issue;
		const result: WorkflowState = {
			issue: { id: issue.id, identifier: issue.identifier, title: issue.title, project: issue.project?.name },
		};
		for (const comment of [...issue.comments.nodes].sort((a, b) => a.createdAt.localeCompare(b.createdAt))) {
			if (comment.body.startsWith(PLAN_APPROVED_PREFIX)) {
				const hash = comment.body.match(/SHA-256: `([a-f0-9]{64})`/)?.[1];
				if (!hash) continue;
				result.plan = { hash, at: comment.createdAt };
				result.handoff = undefined;
				result.review = undefined;
				continue;
			}
			if (!result.plan || comment.createdAt <= result.plan.at) continue;
			if (comment.body.startsWith(EXECUTION_HANDOFF_PREFIX)) {
				result.handoff = { at: comment.createdAt };
				continue;
			}
			if (comment.body.startsWith(SESSION_REVIEW_PREFIX)) {
				const hash = comment.body.match(/Plan SHA-256: `([a-f0-9]{64})`/)?.[1];
				if (hash === result.plan.hash) result.review = { hash, at: comment.createdAt };
			}
		}
		return result;
	}

	async function postComment(issueId: string, body: string): Promise<void> {
		const result = await gql<{ commentCreate: { success: boolean } }>(
			`mutation($input:CommentCreateInput!){ commentCreate(input:$input){success} }`,
			{ input: { issueId, body } },
		);
		if (!result.commentCreate.success) throw new Error("Linear refused the comment (success:false)");
	}


	function footer(ctx: ExtensionContext) {
		try {
			const theme = ctx.ui.theme;
			const warn = state.obligationHandoff?.armed || state.obligationReview?.armed ? " ⚠" : "";
			// HOME-130: the active issue lives inline on the main status line,
			// beside the path/branch; the lower line keeps project + counts only.
			ctx.ui.setStatus(
				"linear-now-current",
				state.identifier
					? theme.fg("accent", `▶ ${state.identifier}${state.title ? ` ${truncateToWidth(state.title, 40, Ellipsis.Unicode)}` : ""}`)
					: undefined,
				{ placement: "inline" },
			);
			if (state.identifier && state.treeCounts) {
				// HOME-109 plain-words footer — never calls the network (runs every turn).
				// ponytail: counts staleness ceiling = last goalTree() call (session start +
				// every my_now); wire a refresh timer only if the owner notices.
				const c = state.treeCounts;
				const bits = [state.project ?? "", `${c.done}/${c.total} done`, `${c.stuck} stuck`, `${c.onyou} on you`];
				ctx.ui.setStatus("linear-now", theme.fg("accent", `◆ ${bits.filter(Boolean).join(" · ")}${warn}`));
			} else if (state.identifier) {
				const elapsed = state.setAt ? ` · ${fmtElapsed(Date.now() - state.setAt)}` : "";
				const proj = state.project ? ` · ${state.project}` : "";
				ctx.ui.setStatus("linear-now", theme.fg("accent", `◆ NOW${proj}${theme.fg("dim", elapsed)}${warn}`));
			} else if (state.lastDone) {
				ctx.ui.setStatus(
					"linear-now",
					theme.fg("dim", `✓ last: ${state.lastDone.identifier} ${state.lastDone.title} · ${fmtElapsed(Date.now() - state.lastDone.at)} ago — /now to refocus${warn}`),
				);
			} else {
				ctx.ui.setStatus("linear-now", theme.fg("dim", `◇ no NOW — /now to pick${warn}`));
			}
		} catch {
			/* headless: no footer */
		}
	}

	async function setNow(issue: { id: string; identifier: string; title: string; project?: string }, ctx: ExtensionContext) {
		const labelId = await nowLabelId();
		const prev = await labelHolder();
		if (prev && prev.id !== issue.id) {
			await gql(`mutation($id:String!,$labelId:String!){ issueRemoveLabel(id:$id,labelId:$labelId){success} }`, {
				id: prev.id,
				labelId,
			});
		}
		await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, {
			id: issue.id,
			labelId,
		});
		state.issueId = issue.id;
		state.identifier = issue.identifier;
		state.title = issue.title;
		state.project = issue.project;
		state.setAt = Date.now();
		state.treeCounts = undefined; // previous goal's counts — next goalTree() refreshes
		await saveCache();
		persistSession();
		footer(ctx);
		ctx.ui.notify(`NOW → ${issue.identifier} ${issue.title}`, "info");
	}

	async function clearNow(ctx: ExtensionContext, markDone: boolean) {
		if (state.issueId) {
			try {
				const labelId = await nowLabelId();
				await gql(`mutation($id:String!,$labelId:String!){ issueRemoveLabel(id:$id,labelId:$labelId){success} }`, {
					id: state.issueId,
					labelId,
				});
			} catch (e) {
				ctx.ui.notify(`Linear label not cleared (${String(e)}) — cleared locally`, "warning");
			}
		}
		if (markDone && state.identifier) {
			state.lastDone = { identifier: state.identifier, title: state.title ?? "", at: Date.now() };
		}
		state.issueId = state.identifier = state.title = state.project = undefined;
		state.setAt = undefined;
		await saveCache();
		persistSession();
		footer(ctx);
	}

	/** Owner-verdict close: state change first (the act), then the verdict comment (the record). */
	async function closeWithVerdict(issueId: string, identifier: string, outcome: "done" | "canceled", reason: string | undefined, ctx: ExtensionContext): Promise<string> {
		const stateId = await stateIdFor(outcome === "canceled" ? "canceled" : "completed");
		const upd = await gql<{ issueUpdate: { success: boolean } }>(
			`mutation($id:String!,$input:IssueUpdateInput!){ issueUpdate(id:$id,input:$input){success} }`,
			{ id: issueId, input: { stateId } },
		);
		if (!upd.issueUpdate.success) throw new Error(`Linear refused the close (issueUpdate success:false) for ${identifier} — no verdict recorded`);
		try {
			await postComment(
				issueId,
				`**Owner verdict in session: close${outcome === "canceled" ? " (canceled — not doing it)" : ""}** — ${reason ?? "done"} (omp session ${new Date().toISOString().slice(0, 10)})`,
			);
		} catch (e) {
			try {
				ctx.ui.notify(`verdict comment failed (${String(e)}) — close stands`, "warning");
			} catch {
				/* headless */
			}
		}
		if (issueId === state.issueId) await clearNow(ctx, true);
		// A verdict close settles every workflow obligation on the closed issue.
		if (state.executingIssue?.id === issueId) {
			state.executingIssue = undefined;
			state.approvedPlan = undefined;
			state.obligationHandoff = undefined;
			state.obligationReview = undefined;
			persistSession();
			void saveCache();
		}
		return `${identifier} → ${outcome === "canceled" ? "Canceled" : "Done"} (owner verdict)`;
	}

	async function buildDigest(): Promise<string> {
		const treeLines = goalTree()
			.then(t => (t ? renderGoalTree(t) : ["TREE: no goal picked — /now to pick"]))
			.catch(e => [`TREE: unavailable (${String(e)}) — session unblocked`]);
		const now = state.identifier ? `NOW: ${state.project ? `${state.project} · ` : ""}${state.identifier} ${state.title}` : "NOW: unset — /now to pick";
		const contracts = [WORKFLOW_SEQUENCE, CLOSEOUT_BOUNDARY];
		let d: { projects: { nodes: { name: string; state: string; health?: string }[] }; issues: { nodes: { identifier: string; title: string; createdAt: string }[] } };
		try {
			d = await gql(
				`query($label:String!,$team:String!){ projects(first:50){nodes{name state health}} issues(first:50,orderBy:createdAt,filter:{team:{key:{eq:$team}},labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{identifier title createdAt}} }`,
				{ label: QUEUE_LABEL, team: TEAM_KEY },
			);
		} catch (e) {
			// Fail open with what we have: cached NOW + the tree's own honest line
			// (missing key / API down degrades the bookend, never blocks the session).
			return ["── Linear bookend (linear.app/spec-kit) ──", now, scopeLine, ...(await treeLines), `[linear] queue/in-flight unavailable (${String(e)}) — session unblocked`, ...contracts].join("\n");
		}
		const inflight = d.projects.nodes
			.filter(p => p.state === "started")
			.map(p => `${p.name} [${p.health ?? "?"}]`)
			.join(" · ");
	// ponytail: first:50 cap — beyond 50 queued the count and oldest under-report again; paginate if that ever happens
		const n = d.issues.nodes.length;
		const shown = d.issues.nodes.slice(0, 10);
		const queue =
			shown.map(i => `${i.identifier} ${i.title}`).join(" | ") + (n > shown.length ? ` | …+${n - shown.length} more` : "");
		const oldestDays = Math.max(0, ...d.issues.nodes.map(i => Math.floor((Date.now() - Date.parse(i.createdAt)) / 86_400_000)));
		return [
			"── Linear bookend (linear.app/spec-kit) ──",
			now,
			scopeLine,
			...(await treeLines),
			`IN FLIGHT: ${inflight || "none"}`,
			`NEEDS CHRIS (${n}${n ? `, oldest ${oldestDays}d` : ""}): ${queue || "empty"}`,
			...(n > DRAIN_MAX_QUEUE || oldestDays > DRAIN_MAX_AGE_DAYS
				? [`DRAIN RULE TRIPPED: queue ${n} deep / oldest ${oldestDays}d — surface the 3 oldest to Chris for rulings this session.`]
				: []),
			...contracts,
		].join("\n");
	}

	// ---- lifecycle: digest injection (new + resume), footer, state restore ----

	// HOME-114 mechanical lock: flips ONLY on host-observed owner entry of /summary or /done —
	// TUI raw text (input event), the structured user-attributed skill-prompt message the host
	// composes for a user-invoked summary skill (message_start; models cannot author custom
	// messages, so prompt bytes never unlock), or the /done command handler.
	// FAIL CLOSED on unknown depth: subagent sessions (taskDepth > 0) and hosts predating
	// ctx.taskDepth never unlock — on an old omp build every wrap-up write stays refused until
	// the rebuilt host is live. Authorization is per-transcript: reset on session start/switch.
	let closeoutAuthorized = false;
	const ownerSession = (ctx: { taskDepth?: number } | undefined): boolean => ctx?.taskDepth === 0;

	async function authorizeSummary(ctx: ExtensionContext): Promise<void> {
		if (summaryAuthorized) return;
		summaryAuthorized = true;
		closeoutAuthorized = true;
		if (!state.issueId || !state.identifier) {
			ctx.ui.notify("No NOW is selected. Review can run, but /done stays blocked; run /intake first.", "warning");
			return;
		}
		try {
			const workflow = await workflowState(state.issueId);
			if (!workflow.plan) {
				ctx.ui.notify(`No approved plan is stamped on ${state.identifier}. Review can run, but /done stays blocked; run /plan first.`, "warning");
				return;
			}
			state.executingIssue = { id: workflow.issue.id, identifier: workflow.issue.identifier };
			state.approvedPlan = { hash: workflow.plan.hash, at: Date.parse(workflow.plan.at) };
			state.obligationReview = { armed: true, blockedOnce: false };
			persistSession();
			await saveCache();
			footer(ctx);
		} catch (error) {
			ctx.ui.notify(`Could not load ${state.identifier} workflow state (${String(error)}). Review can run, but /done stays blocked.`, "warning");
		}
	}

	pi.on("session_start", async (_e, ctx) => {
		closeoutAuthorized = false;
		summaryAuthorized = false;
		intakeActive = false;
		intakeSelected = false;
		planTarget = undefined;
		await loadCache();
		models = ctx.models;
		// session entry wins over cache for NOW restore (survives cache loss)
		try {
			for (const entry of ctx.sessionManager.getBranch()) {
				if (entry.type !== "custom" || !("customType" in entry) || entry.customType !== "linear-now") continue;
				if (!("data" in entry) || !entry.data || typeof entry.data !== "object") continue;
				const data = entry.data as NowState; // own persisted shape — written by persistSession above
				if ((data.team ?? "HOME") === TEAM_KEY) Object.assign(state, data);
			}
		} catch {
			/* fresh session */
		}
		digestPending = true;
		digestInjectedThisSession = false;
		if (gitRooted && !projectFilter) {
			try {
				ctx.ui.notify(`Unscoped git repo: no .linear-project — /now unfiltered, unscoped writes refused. ${SCOPE_FIX}`, "warning");
			} catch {
				/* headless */
			}
		} else if (projectFilter) {
			try {
				if (!(await projectScopeExists())) {
					ctx.ui.notify(`Project "${projectFilter}" from .linear-project does not exist in Linear — create it or fix the marker`, "error");
				}
			} catch (error) {
				try {
					ctx.ui.notify(`Could not verify .linear-project "${projectFilter}" (${String(error)}) — session unblocked`, "warning");
				} catch {
					/* headless */
				}
			}
		}
		footer(ctx);
	});

	pi.on("session_switch", async (event, ctx) => {
		closeoutAuthorized = false; // authorization never crosses transcripts (advisor F1)
		summaryAuthorized = false;
		intakeActive = false;
		intakeSelected = false;
		planTarget = undefined;
		if (event.reason === "resume" || event.reason === "new") {
			digestPending = true;
			digestInjectedThisSession = false;
		}
		footer(ctx);
	});

	// HOME-56: owner slash-command actions (/now, /done) were invisible to the agent —
	// it argued from the session-start digest. Queue one-line notices, flush next turn.
	const pendingNotices: string[] = [];
	pi.on("input", async (event, ctx) => {
		if (!ownerSession(ctx) || event.source === "extension") return undefined;
		if (/^\s*\/plan\b/.test(event.text)) {
			if (!state.issueId || !state.identifier || !state.title) {
				ctx.ui.notify("Run /intake first, or choose an issue with /now.", "warning");
				return { handled: true };
			}
			planTarget = {
				id: state.issueId,
				identifier: state.identifier,
				title: state.title,
				project: state.project,
			};
		}
		if (/^\s*\/summary\b/.test(event.text)) await authorizeSummary(ctx);
		else if (/^\s*\/done\b/.test(event.text)) closeoutAuthorized = true;
		return undefined;
	});
	pi.on("message_start", async (event, ctx) => {
		if (!ownerSession(ctx)) return;
		const m = event.message as { role?: string; customType?: string; attribution?: string; details?: { name?: string } };
		if (m.role !== "custom" || m.customType !== "skill-prompt" || m.attribution !== "user") return;
		if (m.details?.name === "intake") {
			intakeActive = true;
			return;
		}
		if (m.details?.name === "summary") await authorizeSummary(ctx);
	});
	pi.on("before_agent_start", async () => {
		const notices = pendingNotices.splice(0).join("\n");
		if (!digestPending || digestInjectedThisSession) {
			return notices ? { message: { customType: "linear-notice", content: notices } } : undefined;
		}
		digestPending = false;
		try {
			const digest = await buildDigest();
			digestInjectedThisSession = true;
			return { message: { customType: "linear-digest", content: notices ? `${digest}\n${notices}` : digest } };
		} catch (e) {
			pi.logger.warn("linear-now: digest failed", { error: String(e) });
			return { message: { customType: "linear-digest", content: `[linear] digest unavailable (${String(e)}) — session unblocked` } };
		}
	});

	pi.on("plan_approved", async (event, ctx) => {
		const target =
			planTarget ??
			(state.issueId && state.identifier && state.title
				? { id: state.issueId, identifier: state.identifier, title: state.title, project: state.project }
				: undefined);
		if (!target) {
			return { cancel: true, reason: "Run /intake first, or choose an issue with /now." };
		}
		const stamp = preparePlanStamp(event);
		if ("reason" in stamp) return { cancel: true, reason: stamp.reason };
		try {
			const workflow = await workflowState(target.id);
			if (workflow.plan?.hash !== stamp.hash) {
				await postComment(workflow.issue.id, stamp.body);
			}
			armExecution({ id: workflow.issue.id, identifier: workflow.issue.identifier }, stamp.hash);
			planTarget = undefined;
			summaryAuthorized = false;
			closeoutAuthorized = false;
			footer(ctx);
			return undefined;
		} catch (error) {
			return {
				cancel: true,
				reason: `Could not stamp the approved plan on ${target.identifier} (${String(error)}). Plan mode remains active.`,
			};
		}
	});

	pi.on("turn_start", async (_e, ctx) => footer(ctx));
	pi.on("turn_end", async (_e, ctx) => footer(ctx));

	// ---- HOME-122 hidden checkpoint continuation ----

	pi.on("session_stop", async event => {
		try {
			if (event.stop_hook_active || !state.executingIssue) return;
			const reviewOwed = state.obligationReview?.armed && !state.obligationReview.blockedOnce;
			const handoffOwed = state.obligationHandoff?.armed && !state.obligationHandoff.blockedOnce;
			const kind = reviewOwed ? "review" : handoffOwed ? "handoff" : undefined;
			if (!kind) return;
			if (state.obligationHandoff?.armed) state.obligationHandoff.blockedOnce = true;
			if (state.obligationReview?.armed) state.obligationReview.blockedOnce = true;
			await saveCache();
			return {
				continue: true,
				additionalContext: [
					`Post one silent workflow checkpoint on ${state.executingIssue.identifier} with the linear tool: action:"comment", issue:"${state.executingIssue.identifier}", kind:"${kind}".`,
					kind === "review"
						? "The body carries the session review: completed work, verification evidence, remaining risks, and exact resume state."
						: "The body carries the execution handoff: done, remaining, and exact resume steps.",
					STOP_REMINDER_BOUNDARY,
					"This continuation fires once; later turns show only ⚠ while the checkpoint remains owed.",
				].join(" "),
			};
		} catch (error) {
			pi.logger.warn("linear-now: stop obligation check failed (fail-open)", { error: String(error) });
			return;
		}
	});

	pi.on("session_shutdown", async () => {
		try {
			if (state.obligationHandoff?.armed || state.obligationReview?.armed) await saveCache();
		} catch {
			/* fail open */
		}
	});

	// ---- commands ----

	pi.registerCommand("now", {
		description: "The NOW window (bare = map + detail pane, or /now HOME-31, /now clear)",
		getArgumentCompletions: prefix => {
			const p = prefix.trim().toUpperCase();
			if (!p) return null;
			const ids = [state.identifier, state.lastDone?.identifier].filter(Boolean) as string[];
			return ids.filter(i => i.startsWith(p)).map(i => ({ value: i, label: i })) as any;
		},
		handler: async (args, ctx) => {
			const arg = args.trim();
			try {
				if (arg === "clear") {
					await clearNow(ctx, false);
					ctx.ui.notify("NOW cleared (no close proposed)", "info");
					pendingNotices.push("[linear] Owner cleared NOW via /now clear — no close proposed.");
					return;
				}
				if (arg) {
					const issue = await findIssue(arg);
					await setNow({ id: issue.id, identifier: issue.identifier, title: issue.title, project: issue.project?.name }, ctx);
					pendingNotices.push(`[linear] Owner set NOW to ${issue.identifier} via /now.`);
					return;
				}
				if (projectFilter && !(await projectScopeExists())) {
					ctx.ui.notify(`Project "${projectFilter}" from .linear-project does not exist in Linear — create it or fix the marker`, "error");
					return;
				}
				const map = await mapData();
				if (!map.surfaces.length || !map.surfaces.some(s => s.issues.length)) {
					ctx.ui.notify(projectFilter ? `No open issues in project "${projectFilter}" (.linear-project filter)` : "No open issues found", "warning");
					return;
				}
				// Prefer ctx.ui.custom (TUI overlay with framed detail pane) when the
				// host supports it; otherwise fall back to a plain select() so
				// non-TUI surfaces (omp-webui browser client, Slack bridge, headless
				// scripting) can still drive /now with the same MapIssue list.
				let pick: MapIssue | undefined;
				const supportsCustom = typeof (ctx.ui as { custom?: unknown }).custom === "function";
				if (supportsCustom) {
					try {
						pick = await ctx.ui.custom<MapIssue | undefined>(nowWindowFactory(map), { overlay: true });
					} catch {
						pick = undefined;
					}
				}
				if (!supportsCustom || pick === undefined) {
					const flat: { label: string; issue: MapIssue }[] = [];
					for (const s of map.surfaces) {
						for (const i of s.issues) {
							const proj = i.project ? `${i.project} · ` : "";
							const age = fmtElapsed(Date.now() - Date.parse(i.updatedAt));
							const mark = i.isNow ? "● " : "  ";
							flat.push({ label: `${mark}[${s.name}] ${proj}${i.identifier} ${i.title} · ${age}`.slice(0, 200), issue: i });
						}
					}
					if (!flat.length) {
						ctx.ui.notify("No open issues found", "warning");
						return;
					}
					// Move any issue Linear labels as `now` to the top so the list mirrors
					// the TUI overlay's ordering intent even without live isNow state.
					flat.sort((a, b) => Number(b.issue.isNow) - Number(a.issue.isNow));
					const choice = await ctx.ui.select(
						`Pick your NOW${map.capped ? " (list capped)" : ""}`,
						flat.map(f => f.label),
					);
					if (typeof choice !== "string" || !choice) return;
					pick = flat.find(f => f.label === choice)?.issue;
				}
				if (!pick) return;
				const yes = await ctx.ui.confirm(`Make ${pick.identifier} your NOW?`, `"${pick.title}"\n${pick.project ?? ""}`);
				if (!yes) return;
				await setNow({ id: pick.id, identifier: pick.identifier, title: pick.title, project: pick.project }, ctx);
				pendingNotices.push(`[linear] Owner set NOW to ${pick.identifier} via /now.`);
			} catch (e) {
				ctx.ui.notify(`/now failed: ${String(e)}`, "error");
			}
		},
	});

	pi.registerCommand("done", {
		description: "Close the reviewed NOW issue with the owner's verdict",
		handler: async (_args, ctx) => {
			if (!ownerSession(ctx)) {
				ctx.ui.notify("/done is owner-only — refused outside the owner's main session (HOME-114)", "warning");
				return;
			}
			closeoutAuthorized = true;
			if (!state.issueId || !state.identifier) {
				ctx.ui.notify("No NOW set", "warning");
				return;
			}
			const { issueId, identifier, title } = {
				issueId: state.issueId,
				identifier: state.identifier,
				title: state.title ?? "",
			};
			try {
				const workflow = await workflowState(issueId);
				if (!workflow.plan) {
					ctx.ui.notify("Run /plan first.", "warning");
					return;
				}
				if (!workflow.review) {
					ctx.ui.notify("Run /summary first.", "warning");
					return;
				}
				const confirmed = await ctx.ui.confirm(
					`This is your verdict — close ${identifier}?`,
					`"${title}"\n\nMoves to Done + posts the verdict comment. Not reversible from here.`,
				);
				if (!confirmed) return;
				ctx.ui.notify(await closeWithVerdict(issueId, identifier, "done", undefined, ctx), "info");
				pendingNotices.push(`[linear] Owner verdict via /done: ${identifier} closed (Done); NOW cleared.`);
				const commitNotice = await commitSessionWork(ctx.ui, process.cwd(), identifier);
				if (commitNotice) pendingNotices.push(commitNotice);
			} catch (error) {
				ctx.ui.notify(`/done failed: ${String(error)}`, "error");
			}
		},
	});

	pi.registerCommand("capture", {
		description: "Capture a stray thought as a Linear issue without losing your thread",
		handler: async (args, ctx) => {
			const text = args.trim();
			if (!text) {
				ctx.ui.notify("Usage: /capture <the thought>", "warning");
				return;
			}
			try {
				const captureProject = fileTarget(undefined);
				const refusal = unscopedRefusal(captureProject);
				if (refusal) {
					ctx.ui.notify(`/capture ${refusal}`, "error");
					return;
				}
				const target = captureProject ? `project "${captureProject}"` : "team HOME (no project)";
				const ok = await ctx.ui.confirm("File this capture?", `"${text}"\n→ ${target}`);
				if (!ok) return;
				let projectId: string | undefined;
				if (captureProject) {
					const d = await gql<{ projects: { nodes: { id: string; name: string }[] } }>(
						`query($name:String!){ projects(filter:{name:{eq:$name}}){nodes{id name}} }`,
						{ name: captureProject },
					);
					projectId = d.projects.nodes[0]?.id;
					if (!projectId) throw new Error(`project "${captureProject}" not found in Linear — refusing to file without its project`);
				}
				const created = await gql<{ issueCreate: { issue: { identifier: string } } }>(
					`mutation($input:IssueCreateInput!){ issueCreate(input:$input){issue{identifier}} }`,
					{ input: { teamId: await teamId(), title: text, ...(projectId ? { projectId } : {}) } },
				);
				ctx.ui.notify(`Captured → ${created.issueCreate.issue.identifier}`, "info");
			} catch (e) {
				ctx.ui.notify(`/capture failed: ${String(e)}`, "error");
			}
		},
	});

	pi.registerCommand("linear", {
		description: "Linear weave admin: status | digest | help",
		getArgumentCompletions: prefix => {
			const opts = ["status", "digest", "help"];
			return opts.filter(o => o.startsWith(prefix.trim())).map(o => ({ value: o, label: o })) as any;
		},
		handler: async (args, ctx) => {
			const sub = args.trim() || "status";
			if (sub === "digest") {
				try {
					pi.sendMessage({ customType: "linear-digest", content: await buildDigest() }, { deliverAs: "nextTurn" });
					ctx.ui.notify("Digest refreshed into context", "info");
				} catch (e) {
					ctx.ui.notify(`digest failed: ${String(e)}`, "error");
				}
				return;
			}
			if (sub === "help") {
				ctx.ui.notify("/now (window) · /now <issue>|clear · /done (reviewed close) · /capture <text> · /linear status|digest", "info");
				return;
			}
			const lines: string[] = [];
			lines.push(`project filter: ${projectFilter ?? "none"}`);
			lines.push(`key file: ${apiKey() ? "found" : `MISSING (${KEY_FILE})`}`);
			try {
				const t0 = Date.now();
				await gql(`query{viewer{id}}`);
				lines.push(`API: reachable (${Date.now() - t0}ms)`);
			} catch (e) {
				lines.push(`API: UNREACHABLE — ${String(e)}`);
			}
			try {
				const holder = await labelHolder();
				lines.push(`now label holder: ${holder ? `${holder.identifier} ${holder.title}` : "none"}`);
				const local = state.identifier ? `${state.identifier} ${state.title}` : "none";
				lines.push(`local pointer: ${local}${holder && state.identifier && holder.identifier !== state.identifier ? "  ⚠ DRIFT vs Linear" : ""}`);
			} catch {
				lines.push("now label holder: unknown (API down)");
			}
			lines.push(`digest this session: ${digestInjectedThisSession ? "injected" : "not yet"}`);
			pi.sendMessage({ customType: "linear-status", content: `── /linear status @ ${new Date().toISOString().slice(11, 19)}Z ──\n${lines.join("\n")}` }, { deliverAs: "nextTurn" });
			ctx.ui.notify(lines.join(" · "), "info");
		},
	});

	// ---- the bounded `linear` tool ----

	const z = pi.zod;
	pi.registerTool({
		name: "linear",
		label: "Linear",
		description: [
			"Bounded access to the owner's Linear workspace (team HOME; worlds→surfaces→promises→issues).",
			WORKFLOW_SEQUENCE,
			"Reads are free: get_issue, tree, waiting, my_now. comment writes immediately; kind defaults to evidence, while handoff and review are ordered workflow checkpoints.",
			"create_issue, queue_issue, propose_close, update_health, set_now, and archive_issue are owner-confirmed two-phase writes: first call previews and writes nothing; repeat with confirm:true only after Chris says yes.",
			"During user-invoked intake, the first confirmed issue or batch parent becomes NOW in the same previewed operation.",
			"create_issue queue:true and queue_issue add waiting-on-chris. A batch publishes one parent plus children with native parent/block relations behind one preview; partial failure reports the exact landed inventory.",
			"Writes from a scoped git repo require its .linear-project target unless project is explicit. Never assume success without success:true.",
			"Only owner-entered /done closes an issue. propose_close remains /summary's asynchronous recommendation.",
			"update_health, propose_close, and archive_issue require an owner-entered /summary or /done; a keep-open verdict blocks them.",
		].join(" "),
		parameters: z.object({
			action: z.enum(["get_issue", "tree", "waiting", "my_now", "comment", "create_issue", "queue_issue", "propose_close", "update_health", "set_now", "archive_issue"]),
			issue: z.string().optional().describe("Issue identifier like HOME-31"),
			title: z.string().optional().describe("Issue title (create_issue)"),
			description: z.string().optional().describe("Issue description markdown (create_issue)"),
			project: z.string().optional().describe("Project name (create_issue target, update_health)"),
			health: z.enum(["onTrack", "atRisk", "offTrack"]).optional().describe("Project health (update_health)"),
			body: z.string().optional().describe("Comment body or one-line update"),
			kind: z.enum(["evidence", "handoff", "review"]).optional().describe("comment stage; defaults to evidence"),
			queue: z.boolean().optional().describe("create_issue: also add the waiting-on-chris label so the issue lands in the owner decision queue"),
			batch: z
				.array(
					z.object({
						title: z.string(),
						description: z.string().optional(),
						blocks: z.array(z.number().int().nonnegative()).optional()
							.describe("zero-based indexes of sibling batch entries THIS entry blocks"),
					}),
				)
				.optional()
				.describe(
					"create_issue: publish these child issues under the parent (title/description) with native parent links and blocks relations, behind ONE preview — one confirm writes the whole set",
				),
			confirm: z.boolean().optional().describe("Two-phase write approval: pass true ONLY after the owner saw the previewed payload and said yes"),
		}),
		// Signature contract is (toolCallId, params, signal, onUpdate, ctx) — the
		// pre-2026-08-10 4-param version received onUpdate as `ctx`, crashing every
		// write at ctx.ui.confirm (HOME-30 walkthrough step 5).
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const deny = (msg: string) => ({ content: [{ type: "text" as const, text: msg }], details: { success: false } });
			const okText = (text: string, details: Record<string, unknown> = {}) => ({
				content: [{ type: "text" as const, text }],
				details: { success: true, ...details },
			});
			try {
				// HOME-114: wrap-up writes remain host-locked; only /done closes.
				if ((params.action === "update_health" || params.action === "propose_close" || params.action === "archive_issue") && !closeoutAuthorized) {
					return deny(CLOSEOUT_LOCK_REFUSAL);
				}
				switch (params.action) {
					case "my_now": {
						// HOME-109: status-on-demand = completion tree + explanation line.
						try {
							const t = await goalTree();
							if (t) return okText(renderGoalTree(t).join("\n"));
						} catch (e) {
							return okText(`tree unavailable (${String(e)})`);
						}
						// Projectless NOW says it truthfully (by-seeing verdict 3, landed 2026-08-14 — see HOME-109 review comment)
						if (state.identifier) return okText(`NOW: ${state.identifier} ${state.title ?? ""} — no goal attached, so no tree`.trim());
						return okText("NOW unset");
					}
					case "waiting": {
						const d = await gql<{ issues: { nodes: { identifier: string; title: string }[] } }>(
							`query($label:String!,$team:String!){ issues(first:50,orderBy:createdAt,filter:{team:{key:{eq:$team}},labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{identifier title}} }`,
							{ label: QUEUE_LABEL, team: TEAM_KEY },
						);
						return okText(d.issues.nodes.map(i => `${i.identifier} ${i.title}`).join("\n") || "queue empty");
					}
					case "tree": {
						const d = await gql<{
							projects: { nodes: { name: string; state: string; health?: string; initiatives: { nodes: { name: string }[] }; projectMilestones: { nodes: { name: string; targetDate?: string }[] } }[] };
						}>(
							`query{ projects(first:50){nodes{name state health initiatives(first:1){nodes{name}} projectMilestones(first:10){nodes{name targetDate}}}} }`,
						);
						const lines = d.projects.nodes
							.filter(p => p.state === "started" || p.state === "planned")
							.map(p => {
								const ms = p.projectMilestones.nodes.map(m => `    ◦ ${m.name}${m.targetDate ? ` (${m.targetDate})` : ""}`).join("\n");
								return `${p.initiatives.nodes[0]?.name ?? "?"} › ${p.name} [${p.state}${p.health ? `/${p.health}` : ""}]${ms ? `\n${ms}` : ""}`;
							});
						return okText(lines.join("\n") || "no projects");
					}
					case "get_issue": {
						if (!params.issue) return deny("issue identifier required");
						const d = await gql<{ issue: { id: string; identifier: string; title: string; description?: string; state: { name: string }; project?: { name: string }; labels: { nodes: { name: string }[] } } }>(
							`query($id:String!){ issue(id:$id){id identifier title description state{name} project{name} labels{nodes{name}}} }`,
							{ id: params.issue },
						);
						const i = d.issue;
						return okText(
							[
								`${i.identifier} ${i.title}`,
								`state: ${i.state.name} · project: ${i.project?.name ?? "none"} · labels: ${i.labels.nodes.map(l => l.name).join(",") || "none"}`,
								(i.description ?? "").slice(0, 1200),
							].join("\n"),
						);
					}
					case "comment": {
						if (!params.issue || !params.body) return deny("issue identifier and body required");
						const kind = params.kind ?? "evidence";
						const workflow = kind === "evidence" ? undefined : await workflowState(params.issue);
						const currentPlan = workflow?.plan;
						if (kind !== "evidence" && !currentPlan) {
							return deny(`Run /plan first; ${params.issue} has no current approved plan.`);
						}
						if (kind === "review" && !summaryAuthorized) {
							return deny('REFUSED — a review comment requires Chris to literally enter /summary in this owner session.');
						}
						const issue = workflow?.issue ?? (await findIssue(params.issue));
						const body =
							kind === "handoff"
								? `${EXECUTION_HANDOFF_PREFIX}\n\n${params.body}`
								: kind === "review"
									? `${SESSION_REVIEW_PREFIX}\n\nPlan SHA-256: \`${currentPlan.hash}\`\n\n${params.body}`
									: params.body;
						try {
							await postComment(issue.id, body);
						} catch (error) {
							return deny(`${String(error)} on ${issue.identifier} — workflow state unchanged`);
						}
						if (kind !== "evidence") settleCheckpoint(issue.id, kind, ctx);
						return okText(`comment posted on ${issue.identifier}`);
					}
					case "create_issue": {
						if (!params.title) return deny("title required");
						const selectsNow = intakeActive && !intakeSelected;
						if (params.batch !== undefined && params.batch.length > 0) {
						const entries = params.batch;
						const n = entries.length;
						// 2a. validate BEFORE any gate call (invariant 6: reject at preview time)
						for (let k = 0; k < n; k++) {
							for (const j of entries[k].blocks ?? []) {
								if (!Number.isInteger(j) || j < 0 || j >= n || j === k) {
									return deny(`batch entry [${k}] "${entries[k].title}" has invalid blocks index ${j} (valid: 0–${n - 1}, not itself)`);
								}
							}
						}
						// acyclicity — Kahn's algorithm over edges k→j
						const indeg = new Array<number>(n).fill(0);
						for (let k = 0; k < n; k++) for (const j of entries[k].blocks ?? []) indeg[j]++;
						const ready: number[] = [];
						for (let k = 0; k < n; k++) if (indeg[k] === 0) ready.push(k);
						let seen = 0;
						while (ready.length > 0) {
							const k = ready.shift()!;
							seen++;
							for (const j of entries[k].blocks ?? []) if (--indeg[j] === 0) ready.push(j);
						}
						if (seen < n) {
							const leftover: string[] = [];
							for (let k = 0; k < n; k++) if (indeg[k] > 0) leftover.push(`[${k}] "${entries[k].title}"`);
							return deny(`blocks edges form a cycle: ${leftover.join(" ↔ ")} — publish refused`);
						}
						// 2b. resolve project — strict: a mis-landed tree is n+1 wrong issues
						const target = fileTarget(params.project);
						const batchRefusal = unscopedRefusal(target);
						if (batchRefusal) return deny(batchRefusal);
						let projectId: string | undefined;
						if (target) {
							const dp = await gql<{ projects: { nodes: { id: string }[] } }>(`query($name:String!){ projects(filter:{name:{eq:$name}}){nodes{id}} }`, { name: target });
							projectId = dp.projects.nodes[0]?.id;
							if (!projectId) return deny(`project "${target}" not found — refusing to publish a batch without its project`);
						}
						// 2c. ONE preview covering the entire batch (invariant 2)
						const edgeLines: string[] = [];
						for (let k = 0; k < n; k++) for (const j of entries[k].blocks ?? []) edgeLines.push(`[${k}] blocks [${j}]`);
						const detail = [
							`PARENT "${params.title}" → ${target ? `project ${target}` : "team HOME"}${params.queue ? `\n→ + ${QUEUE_LABEL} label on parent` : ""}${selectsNow ? "\n→ becomes NOW" : ""}${params.description ? `\n${params.description.slice(0, 400)}` : ""}`,
							...entries.map((e, k) => `[${k}] "${e.title}"${e.description ? `\n${e.description.slice(0, 200)}` : ""}`),
							edgeLines.length > 0 ? `edges:\n${edgeLines.join("\n")}` : "edges: none",
						].join("\n\n");
						const gate = await ownerGate(ctx, `Model wants to publish a BATCH — 1 parent + ${n} children`, detail, params.confirm, true);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — batch NOT published");
						// 2d. execute with partial-failure inventory (invariant 4) — own try/catch so the
						// outer catch can't flatten the inventory into a generic error.
						const landed: string[] = [];
						const edgesLanded: string[] = [];
						const childIds: string[] = [];
						const childIdentifiers: string[] = [];
						try {
							const tid = await teamId();
							const created = await gql<{ issueCreate: { issue: { id: string; identifier: string } } }>(
								`mutation($input:IssueCreateInput!){ issueCreate(input:$input){issue{id identifier}} }`,
								{ input: { teamId: tid, title: params.title, description: params.description, ...(projectId ? { projectId } : {}) } },
							);
							const parentId = created.issueCreate.issue.id;
							const parentIdentifier = created.issueCreate.issue.identifier;
							landed.push(parentIdentifier);
							if (params.queue) {
								const qId = await queueLabelId();
								if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: parentId, labelId: qId });
							}
							if (selectsNow) {
								await setNow({ id: parentId, identifier: parentIdentifier, title: params.title, project: target }, ctx);
								intakeSelected = true;
							}
							for (let k = 0; k < n; k++) {
								const child = await gql<{ issueCreate: { issue: { id: string; identifier: string } } }>(
									`mutation($input:IssueCreateInput!){ issueCreate(input:$input){issue{id identifier}} }`,
									{ input: { teamId: tid, title: entries[k].title, description: entries[k].description, parentId, ...(projectId ? { projectId } : {}) } },
								);
								childIds[k] = child.issueCreate.issue.id;
								childIdentifiers[k] = child.issueCreate.issue.identifier;
								landed.push(child.issueCreate.issue.identifier);
							}
							for (let k = 0; k < n; k++) {
								for (const j of entries[k].blocks ?? []) {
									const rel = await gql<{ issueRelationCreate: { success: boolean } }>(
										`mutation($input:IssueRelationCreateInput!){ issueRelationCreate(input:$input){success} }`,
										{ input: { issueId: childIds[k], relatedIssueId: childIds[j], type: "blocks" } },
									);
									if (!rel.issueRelationCreate.success) throw new Error(`relation [${k}]→[${j}] refused (success:false)`);
									edgesLanded.push(`${childIdentifiers[k]} blocks ${childIdentifiers[j]}`);
								}
							}
							return okText(
								`batch published: parent ${parentIdentifier} + children ${childIdentifiers.join(", ")}; ${edgesLanded.length} blocks edge(s)${params.queue ? ` + ${QUEUE_LABEL} on parent` : ""}${selectsNow ? " + parent is NOW" : ""}`,
								{ identifier: parentIdentifier, children: childIdentifiers, now: selectsNow },
							);
						} catch (e) {
							const notCreated: string[] = [];
							if (landed.length === 0) notCreated.push(`parent "${params.title}"`);
							for (let k = 0; k < n; k++) if (!childIds[k]) notCreated.push(`[${k}] "${entries[k].title}"`);
							// edges execute in the same k-outer/j-inner order as edgeLines
							const notLandedEdges = edgeLines.slice(edgesLanded.length);
							return {
								content: [{
									type: "text" as const,
									text: [
										`BATCH PARTIAL FAILURE at ${String(e)}`,
										`landed issues: ${landed.join(", ") || "none"}`,
										`landed edges: ${edgesLanded.join(", ") || "none"}`,
										`NOT created: ${[...notCreated, ...notLandedEdges].join(", ") || "none"}`,
										"No rollback exists — surviving issues need an owner verdict to remove.",
									].join("\n"),
								}],
								details: { success: false, landed, edgesLanded },
							};
						}
					}
						const target = fileTarget(params.project);
						const singleRefusal = unscopedRefusal(target);
						if (singleRefusal) return deny(singleRefusal);
						const gate = await ownerGate(
							ctx,
							"Model wants to file an issue",
							`"${params.title}"\n→ ${target ? `project ${target}` : "team HOME"}${params.queue ? `\n→ + ${QUEUE_LABEL} label (lands in your decision queue)` : ""}${selectsNow ? "\n→ becomes NOW" : ""}\n\n${(params.description ?? "").slice(0, 400)}`,
							params.confirm,
							true,
						);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — issue NOT created");
						let projectId: string | undefined;
						if (target) {
							const dp = await gql<{ projects: { nodes: { id: string }[] } }>(`query($name:String!){ projects(filter:{name:{eq:$name}}){nodes{id}} }`, { name: target });
							projectId = dp.projects.nodes[0]?.id;
							if (!projectId) return deny(`project "${target}" not found — refusing to file an issue without its project`);
						}
						const created = await gql<{ issueCreate: { issue: { id: string; identifier: string } } }>(
							`mutation($input:IssueCreateInput!){ issueCreate(input:$input){issue{id identifier}} }`,
							{ input: { teamId: await teamId(), title: params.title, description: params.description, ...(projectId ? { projectId } : {}) } },
						);
						if (params.queue) {
							const qId = await queueLabelId();
							if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: created.issueCreate.issue.id, labelId: qId });
						}
						if (selectsNow) {
							await setNow(
								{
									id: created.issueCreate.issue.id,
									identifier: created.issueCreate.issue.identifier,
									title: params.title,
									project: target,
								},
								ctx,
							);
							intakeSelected = true;
						}
						return okText(
							`created ${created.issueCreate.issue.identifier}${params.queue ? ` + ${QUEUE_LABEL}` : ""}${selectsNow ? " + NOW" : ""}`,
							{ identifier: created.issueCreate.issue.identifier, now: selectsNow },
						);
					}
					case "queue_issue": {
						if (!params.issue) return deny("issue identifier required");
						const issue = await findIssue(params.issue);
						const gate = await ownerGate(
							ctx,
							"Model wants to add an issue to your decision queue",
							`${issue.identifier} ${issue.title}\n\nAdds the ${QUEUE_LABEL} label. Nothing else changes.`,
							params.confirm,
							true,
						);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — label NOT added");
						const qId = await queueLabelId();
						if (!qId) return deny(`${QUEUE_LABEL} label not found in Linear`);
						await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: issue.id, labelId: qId });
						return okText(`${issue.identifier} → ${QUEUE_LABEL}`);
					}
					case "propose_close": {
						if (!params.issue) return deny("issue identifier required");
						const issue = await findIssue(params.issue);
						const gate = await ownerGate(
							ctx,
							"Model wants to propose a close",
							`${issue.identifier} ${issue.title}\nReason: ${params.body ?? "(none given)"}\n\nComments + adds ${QUEUE_LABEL}. Does NOT close.`,
							params.confirm,
							true,
						);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — nothing written");
						await gql(`mutation($input:CommentCreateInput!){ commentCreate(input:$input){success} }`, {
							input: { issueId: issue.id, body: `**Close proposed** — ${params.body ?? "work complete"} (omp session ${new Date().toISOString().slice(0, 10)})` },
						});
						const qId = await queueLabelId();
						if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: issue.id, labelId: qId });
						return okText(`close proposed on ${issue.identifier} — owner verdict closes`);
					}
					case "archive_issue": {
						if (!params.issue) return deny("issue identifier required");
						const issue = await findIssue(params.issue);
						const gate = await ownerGate(
							ctx,
							"Model wants to ARCHIVE an issue",
							`${issue.identifier} ${issue.title}\n\nLinear archive: hides it from views. Does NOT mark it completed. Reversible in-app.`,
							params.confirm,
							true,
						);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — issue NOT archived");
						const arch = await gql<{ issueArchive: { success: boolean } }>(`mutation($id:String!){ issueArchive(id:$id){success} }`, { id: issue.id });
						if (!arch.issueArchive.success) return deny(`Linear refused the archive (success:false) for ${issue.identifier} — nothing hidden`);
						return okText(`${issue.identifier} archived`);
					}
					case "update_health": {
						if (!params.project || !params.health || !params.body) return deny("project, health, body all required");
						const gate = await ownerGate(ctx, "Model wants to post a project update", `${params.project} → ${params.health}\n"${params.body}"`, params.confirm, true);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — nothing written");
						const dp = await gql<{ projects: { nodes: { id: string }[] } }>(`query($name:String!){ projects(filter:{name:{eq:$name}}){nodes{id}} }`, { name: params.project });
						const projectId = dp.projects.nodes[0]?.id;
						if (!projectId) return deny(`project "${params.project}" not found`);
						await gql(`mutation($input:ProjectUpdateCreateInput!){ projectUpdateCreate(input:$input){success} }`, {
							input: { projectId, health: params.health, body: params.body },
						});
						return okText(`update posted: ${params.project} → ${params.health}`);
					}
					case "set_now": {
						if (!params.issue) return deny("issue identifier required");
						const issue = await findIssue(params.issue);
						const gate = await ownerGate(ctx, "Model wants to set NOW", `${issue.identifier} ${issue.title}`, params.confirm, true);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — NOW unchanged");
						await setNow({ id: issue.id, identifier: issue.identifier, title: issue.title, project: issue.project?.name }, ctx);
						return okText(`NOW → ${issue.identifier}`);
					}
				}
			} catch (e) {
				return deny(`linear tool error: ${String(e)}`);
			}
			return deny("unknown action");
		},
	});
}
