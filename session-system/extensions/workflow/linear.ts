/**
 * workflow/linear.ts — the Linear backend: GraphQL I/O extracted unchanged from
 * the pre-HOME-147 linear-now monolith. Storage semantics live here; session
 * state, obligations, confirmations, and rendering live in the host.
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import {
	BatchPartialError,
	EXECUTION_HANDOFF_PREFIX,
	PLAN_APPROVED_PREFIX,
	SESSION_REVIEW_PREFIX,
	type BackendHooks,
	type BackendIssue,
	type BatchOutcome,
	type CreateBatchInput,
	type EvidenceKind,
	type EvidenceMeta,
	type GoalTree,
	type IssueDetail,
	type MapSurface,
	type NowRef,
	type PlanStamp,
	type SummaryGateBlocked,
	type SummaryGateOk,
	type TreeItem,
	type WorkStateCarrier,
	type WorkflowBackend,
	type WorkflowCheckpoint,
} from "./backend";
import { ompWorkConfigDir } from "./config";

/** Resolve a committed single-line marker file (e.g. .linear-project, .work-project)
 *  from the git toplevel of cwd, falling back to the primary checkout root via
 *  --git-common-dir (covers worktrees whose branch predates the marker).
 *  No marker / non-git cwd → null. */
export function resolveMarker(marker: string): string | null {
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

const TEAM_KEY = resolveMarker(".linear-team") ?? "HOME";
const NOW_LABEL = "now";
const QUEUE_LABEL = "waiting-on-chris";
const DRAIN_MAX_QUEUE = 8;
const DRAIN_MAX_AGE_DAYS = 14;
const KEY_FILE = join(homedir(), ".config", "linear.env");
/** Written atomically by `ops cutover execute`; presence freezes every Linear mutation. */
const FREEZE_FILE = join(ompWorkConfigDir(), "linear-frozen.json");
const API = "https://api.linear.app/graphql";
const DONE_SUFFIX: Record<string, string> = { completed: " (done)", canceled: " (done)" };
const STATE_BAND: Record<string, number> = { started: 0, planned: 1 };

export function apiKey(): string | null {
	try {
		const line = readFileSync(KEY_FILE, "utf8")
			.split("\n")
			.find(l => l.startsWith("LINEAR_API_KEY="));
		return line ? line.slice("LINEAR_API_KEY=".length).trim() : null;
	} catch {
		return null;
	}
}

async function gql<T>(query: string, variables?: Record<string, unknown>): Promise<T> {
	const key = apiKey();
	if (!key) throw new Error(`no key file at ${KEY_FILE}`);
	if (/^\s*mutation\b/.test(query)) {
		let frozen = false;
		try {
			readFileSync(FREEZE_FILE);
			frozen = true;
		} catch {
			/* marker absent — writes still allowed */
		}
		if (frozen) throw new Error("linear_frozen: Linear writes are frozen by the Work Ledger cutover — request never sent");
	}
	const res = await fetch(API, {
		method: "POST",
		headers: { Authorization: key, "Content-Type": "application/json" },
		body: JSON.stringify({ query, variables }),
		signal: AbortSignal.timeout(6000),
	});
	if (!res.ok) throw new Error(`Linear HTTP ${res.status}`);
	const body = (await res.json()) as { data?: T; errors?: { message: string }[] };
	if (body.errors?.length) throw new Error(`Linear: ${body.errors[0]!.message}`);
	if (!body.data) throw new Error("Linear: empty response");
	return body.data;
}

export interface LinearBackendDeps {
		/** Footer tree-counts cache refresh (host persists it). Optional: the host
		 *  also applies counts from the goalTree return value. */
		onTreeCounts?(counts: { done: number; total: number; stuck: number; onyou: number }): void;
}

export function createLinearBackend(deps: LinearBackendDeps): WorkflowBackend {
	// Linear-internal id caches — in-memory only; re-resolved once per session.
	let teamIdCache: string | undefined;
	let nowLabelIdCache: string | undefined;
	let doneStateIdCache: string | undefined;
	let canceledStateIdCache: string | undefined;

	async function teamId(): Promise<string> {
		if (teamIdCache) return teamIdCache;
		const d = await gql<{ teams: { nodes: { id: string; key: string }[] } }>(
			`query($key:String!){ teams(filter:{key:{eq:$key}}){nodes{id key}} }`,
			{ key: TEAM_KEY },
		);
		const t = d.teams.nodes[0];
		if (!t) throw new Error(`team ${TEAM_KEY} not found`);
		teamIdCache = t.id;
		return t.id;
	}

	async function nowLabelId(): Promise<string> {
		if (nowLabelIdCache) return nowLabelIdCache;
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
		nowLabelIdCache = id;
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
		const cached = kind === "canceled" ? canceledStateIdCache : doneStateIdCache;
		if (cached) return cached;
		const d = await gql<{ teams: { nodes: { states: { nodes: { id: string; name: string; type: string }[] } }[] } }>(
			`query($key:String!){ teams(filter:{key:{eq:$key}}){nodes{states(first:20){nodes{id name type}}}} }`,
			{ key: TEAM_KEY },
		);
		const s = d.teams.nodes[0]?.states.nodes.find(n => n.type === kind);
		if (!s) throw new Error(`no ${kind} workflow state on team ${TEAM_KEY}`);
		if (kind === "canceled") canceledStateIdCache = s.id;
		else doneStateIdCache = s.id;
		return s.id;
	}

	async function labelHolder(): Promise<NowRef | null> {
		const d = await gql<{ issues: { nodes: { id: string; identifier: string; title: string; project?: { name: string } }[] } }>(
			`query($label:String!,$team:String!){ issues(first:2,filter:{team:{key:{eq:$team}},labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{id identifier title project{name}}} }`,
			{ label: NOW_LABEL, team: TEAM_KEY },
		);
		const n = d.issues.nodes[0];
		return n ? { id: n.id, key: n.identifier, title: n.title, project: n.project?.name } : null;
	}

	async function projectIdFor(name: string): Promise<string> {
		const d = await gql<{ projects: { nodes: { id: string }[] } }>(
			`query($name:String!){ projects(filter:{name:{eq:$name}}){nodes{id}} }`,
			{ name },
		);
		const id = d.projects.nodes[0]?.id;
		if (!id) throw new Error(`project "${name}" not found in Linear — refusing to file without its project`);
		return id;
	}

	async function postComment(issueId: string, body: string): Promise<void> {
		const result = await gql<{ commentCreate: { success: boolean } }>(
			`mutation($input:CommentCreateInput!){ commentCreate(input:$input){success} }`,
			{ input: { issueId, body } },
		);
		if (!result.commentCreate.success) throw new Error("Linear refused the comment (success:false)");
	}

	return {
		name: "linear",
		serviceLabel: "Linear",
		markerFile: ".linear-project",
		scopeFix: 'create the project in Linear first, then echo "<Exact Project Name>" > .linear-project at the repo root',
		cacheFile: TEAM_KEY === "HOME" ? "linear-now.json" : `linear-now-${TEAM_KEY.toLowerCase()}.json`,
		queueNoun: QUEUE_LABEL,
		reviewKind: "closeout",
		evidenceKinds: ["handoff", "verification", "audit", "closeout"],
		bookendTitle: "── Linear bookend (linear.app/spec-kit) ──",

		readCarrier: () => ({}),

		async healthProbe(): Promise<void> {
			await gql(`query{viewer{id}}`);
		},

		async projectScopeExists(project: string): Promise<boolean> {
			const d = await gql<{ projects: { nodes: { name: string }[] } }>(
				`query($name:String!){ projects(first:10,filter:{name:{eq:$name}}){nodes{name}} }`,
				{ name: project },
			);
			return d.projects.nodes.some(p => p.name === project);
		},

		/** ONE bounded request for the /now MAP — never query per-issue (free-plan rate). */
		async mapData(nowKey?: string, projectFilter?: string): Promise<{ surfaces: MapSurface[]; capped: boolean }> {
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
			const byProject = new Map<string, BackendIssue[]>();
			for (const n of d.issues.nodes) {
				const key = n.project?.name ?? NO_SURFACE;
				const list = byProject.get(key) ?? [];
				list.push({
					id: n.id,
					key: n.identifier,
					title: n.title,
					state: n.state.name,
					updatedAt: n.updatedAt,
					waiting: n.labels.nodes.some(l => l.name === QUEUE_LABEL),
					isNow: n.identifier === nowKey,
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
		},

		/** ONE bounded request for an issue's history — card raw fields + the digest packet. */
		async issueDetail(key: string): Promise<IssueDetail> {
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
				{ id: key },
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
				title: i.title,
				state: i.state.name,
				project: i.project?.name,
				labels: i.labels.nodes.map(l => l.name),
				description: i.description,
				blockedBy,
				blocks,
				related,
				comments: last20,
				commentsTotal: sorted.length,
				commentsLast7d: sorted.filter(c => Date.parse(c.createdAt) > weekAgo).length,
				digestPacket,
			};
		},

		async findIssue(key: string): Promise<NowRef> {
			const d = await gql<{ issue: { id: string; identifier: string; title: string; project?: { name: string } } }>(
				`query($id:String!){ issue(id:$id){id identifier title project{name}} }`,
				{ id: key.trim() },
			);
			const i = d.issue;
			if (!i) throw new Error(`${key} not found`);
			return { id: i.id, key: i.identifier, title: i.title, project: i.project?.name };
		},

		currentNow: labelHolder,

		/** HOME-109 completion tree — ONE bounded request (never per-issue). */
		async goalTree(now: NowRef): Promise<GoalTree | null> {
			if (!now.project || !now.key) return null;
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
				{ team: TEAM_KEY, project: now.project },
			);
			const proj = d.projects.nodes[0];
			const items: TreeItem[] = d.issues.nodes.map(n => {
				const isNow = n.identifier === now.key;
				const blocked = n.inverseRelations.nodes.find(r => r.type === "blocks" && !DONE_SUFFIX[r.issue.state.type]);
				// First match wins — exactly one bucket per item (by-seeing verdict 2,
				// 2026-08-14: NOW forces working even from Backlog; stuck/onyou still
				// outrank it — they are the "what's blocking" signal).
				const bucket: TreeItem["bucket"] = DONE_SUFFIX[n.state.type]
					? "done"
					: n.labels.nodes.some(l => l.name === QUEUE_LABEL)
						? "onyou"
						: blocked
							? "stuck"
							: isNow || n.state.type === "started"
								? "working"
								: "next";
				return { key: n.identifier, title: n.title, bucket, blocker: bucket === "stuck" ? blocked?.issue.title : undefined, isNow };
			});
			const counts = {
				done: items.filter(i => i.bucket === "done").length,
				total: items.length,
				stuck: items.filter(i => i.bucket === "stuck").length,
				onyou: items.filter(i => i.bucket === "onyou").length,
			};
			deps.onTreeCounts?.(counts);
			return { goal: now.project, health: proj?.health, promise: proj?.projectMilestones.nodes[0]?.name, items, counts };
		},

		async digestExtras(): Promise<string[]> {
			const d = await gql<{ projects: { nodes: { name: string; state: string; health?: string }[] }; issues: { nodes: { identifier: string; title: string; createdAt: string }[] } }>(
				`query($label:String!,$team:String!){ projects(first:50){nodes{name state health}} issues(first:50,orderBy:createdAt,filter:{team:{key:{eq:$team}},labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{identifier title createdAt}} }`,
				{ label: QUEUE_LABEL, team: TEAM_KEY },
			);
			const inflight = d.projects.nodes
				.filter(p => p.state === "started")
				.map(p => `${p.name} [${p.health ?? "?"}]`)
				.join(" · ");
			// ponytail: first:50 cap — beyond 50 queued the count and oldest under-report; paginate if that ever happens
			const n = d.issues.nodes.length;
			const shown = d.issues.nodes.slice(0, 10);
			const queue = shown.map(i => `${i.identifier} ${i.title}`).join(" | ") + (n > shown.length ? ` | …+${n - shown.length} more` : "");
			const oldestDays = Math.max(0, ...d.issues.nodes.map(i => Math.floor((Date.now() - Date.parse(i.createdAt)) / 86_400_000)));
			return [
				`IN FLIGHT: ${inflight || "none"}`,
				`NEEDS CHRIS (${n}${n ? `, oldest ${oldestDays}d` : ""}): ${queue || "empty"}`,
				...(n > DRAIN_MAX_QUEUE || oldestDays > DRAIN_MAX_AGE_DAYS
					? [`DRAIN RULE TRIPPED: queue ${n} deep / oldest ${oldestDays}d — surface the 3 oldest to Chris for rulings this session.`]
					: []),
			];
		},

		async statusLines(now: NowRef | null, ctx: { projectFilter?: string; digestInjected: boolean }): Promise<string[]> {
			const lines: string[] = [];
			lines.push(`project filter: ${ctx.projectFilter ?? "none"}`);
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
				lines.push(`now label holder: ${holder ? `${holder.key} ${holder.title}` : "none"}`);
				const local = now ? `${now.key} ${now.title}` : "none";
				lines.push(`local pointer: ${local}${holder && now && holder.key !== now.key ? "  ⚠ DRIFT vs Linear" : ""}`);
			} catch {
				lines.push("now label holder: unknown (API down)");
			}
			lines.push(`digest this session: ${ctx.digestInjected ? "injected" : "not yet"}`);
			return lines;
		},

		async workflowState(key: string): Promise<WorkflowCheckpoint> {
			const data = await gql<{
				issue: {
					id: string;
					identifier: string;
					title: string;
					project?: { name: string };
					comments: { nodes: { body: string; createdAt: string }[] };
				};
			}>(
				`query($id:String!){ issue(id:$id){id identifier title project{name} comments(last:50){nodes{body createdAt}}} }`,
				{ id: key },
			);
			const issue = data.issue;
			const result: WorkflowCheckpoint = {
				issue: { id: issue.id, key: issue.identifier, title: issue.title, project: issue.project?.name },
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
		},

		async waitingLines(): Promise<string[]> {
			const d = await gql<{ issues: { nodes: { identifier: string; title: string }[] } }>(
				`query($label:String!,$team:String!){ issues(first:50,orderBy:createdAt,filter:{team:{key:{eq:$team}},labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{identifier title}} }`,
				{ label: QUEUE_LABEL, team: TEAM_KEY },
			);
			const lines = d.issues.nodes.map(i => `${i.identifier} ${i.title}`);
			return lines.length ? lines : ["queue empty"];
		},

		async projectTreeLines(): Promise<string[]> {
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
			return lines.length ? lines : ["no projects"];
		},

		async setNowRemote(issue: NowRef): Promise<void> {
			const labelId = await nowLabelId();
			const prev = await labelHolder();
			if (prev && prev.id !== issue.id) {
				await gql(`mutation($id:String!,$labelId:String!){ issueRemoveLabel(id:$id,labelId:$labelId){success} }`, { id: prev.id, labelId });
			}
			await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: issue.id, labelId });
		},

		async clearNowRemote(issueId: string | undefined): Promise<void> {
			if (!issueId) return;
			const labelId = await nowLabelId();
			await gql(`mutation($id:String!,$labelId:String!){ issueRemoveLabel(id:$id,labelId:$labelId){success} }`, { id: issueId, labelId });
		},

		async stampPlan(target: NowRef, stamp: PlanStamp): Promise<{ issue: NowRef }> {
			const workflow = await this.workflowState(target.key);
			if (workflow.plan?.hash !== stamp.hash) {
				await postComment(workflow.issue.id, stamp.body);
			}
			return { issue: workflow.issue };
		},

		async appendEvidence(issue: NowRef, kind: EvidenceKind, body: string, meta: EvidenceMeta): Promise<void> {
			// Neutral kinds (HOME-147): the Work Ledger carries each as a typed
			// receipt; Linear translates to its typed comment. `closeout` is the
			// session review (was the "review" kind pre-cutover).
			const prefixed =
				kind === "handoff"
					? `${EXECUTION_HANDOFF_PREFIX}\n\n${body}`
					: kind === "closeout"
						? `${SESSION_REVIEW_PREFIX}\n\nPlan SHA-256: \`${meta.planHash}\`\n\n${body}`
						: kind === "verification"
							? `**Verification**\n\n${body}`
							: `**Audit${meta.verdict ? ` — ${meta.verdict}` : ""}**\n\n${body}`;
			await postComment(issue.id, prefixed);
		},

		async createIssue(input: { title: string; description?: string; project?: string; queue?: boolean }): Promise<NowRef> {
			const created = await gql<{ issueCreate: { issue: { id: string; identifier: string } } }>(
				`mutation($input:IssueCreateInput!){ issueCreate(input:$input){issue{id identifier}} }`,
				{
					input: {
						teamId: await teamId(),
						title: input.title,
						description: input.description,
						...(input.project ? { projectId: await projectIdFor(input.project) } : {}),
					},
				},
			);
			const issue = created.issueCreate.issue;
			if (input.queue) {
				const qId = await queueLabelId();
				if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: issue.id, labelId: qId });
			}
			return { id: issue.id, key: issue.identifier, title: input.title, project: input.project };
		},

		async createBatch(input: CreateBatchInput): Promise<BatchOutcome> {
			const entries = input.entries;
			const n = entries.length;
			const landed: string[] = [];
			const edgesLanded: string[] = [];
			const edgeLines: string[] = [];
			for (let k = 0; k < n; k++) for (const j of entries[k]!.blocks ?? []) edgeLines.push(`[${k}] blocks [${j}]`);
			const childIds: string[] = [];
			const childKeys: string[] = [];
			try {
				const tid = await teamId();
				const projectId = input.parent.project ? await projectIdFor(input.parent.project) : undefined;
				const created = await gql<{ issueCreate: { issue: { id: string; identifier: string } } }>(
					`mutation($input:IssueCreateInput!){ issueCreate(input:$input){issue{id identifier}} }`,
					{ input: { teamId: tid, title: input.parent.title, description: input.parent.description, ...(projectId ? { projectId } : {}) } },
				);
				const parentId = created.issueCreate.issue.id;
				const parentKey = created.issueCreate.issue.identifier;
				landed.push(parentKey);
				if (input.parent.queue) {
					const qId = await queueLabelId();
					if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: parentId, labelId: qId });
				}
				for (let k = 0; k < n; k++) {
					const child = await gql<{ issueCreate: { issue: { id: string; identifier: string } } }>(
						`mutation($input:IssueCreateInput!){ issueCreate(input:$input){issue{id identifier}} }`,
						{ input: { teamId: tid, title: entries[k]!.title, description: entries[k]!.description, parentId, ...(projectId ? { projectId } : {}) } },
					);
					childIds[k] = child.issueCreate.issue.id;
					childKeys[k] = child.issueCreate.issue.identifier;
					landed.push(child.issueCreate.issue.identifier);
				}
				for (let k = 0; k < n; k++) {
					for (const j of entries[k]!.blocks ?? []) {
						const rel = await gql<{ issueRelationCreate: { success: boolean } }>(
							`mutation($input:IssueRelationCreateInput!){ issueRelationCreate(input:$input){success} }`,
							{ input: { issueId: childIds[k], relatedIssueId: childIds[j], type: "blocks" } },
						);
						if (!rel.issueRelationCreate.success) throw new Error(`relation [${k}]→[${j}] refused (success:false)`);
						edgesLanded.push(`${childKeys[k]} blocks ${childKeys[j]}`);
					}
				}
				return {
					parent: { id: parentId, key: parentKey, title: input.parent.title, project: input.parent.project },
					children: childKeys.map((key, k) => ({ id: childIds[k]!, key, title: entries[k]!.title, project: input.parent.project })),
					edges: edgesLanded,
					text: `batch published: parent ${parentKey} + children ${childKeys.join(", ")}; ${edgesLanded.length} blocks edge(s)${input.parent.queue ? ` + ${QUEUE_LABEL} on parent` : ""}`,
				};
			} catch (e) {
				if (e instanceof BatchPartialError) throw e;
				const notCreated: string[] = [];
				if (landed.length === 0) notCreated.push(`parent "${input.parent.title}"`);
				for (let k = 0; k < n; k++) if (!childIds[k]) notCreated.push(`[${k}] "${entries[k]!.title}"`);
				// edges execute in the same k-outer/j-inner order as edgeLines
				throw new BatchPartialError(e, landed, edgesLanded, [...notCreated, ...edgeLines.slice(edgesLanded.length)]);
			}
		},

		async queueIssue(issue: NowRef): Promise<void> {
			const qId = await queueLabelId();
			if (!qId) throw new Error(`${QUEUE_LABEL} label not found in Linear`);
			await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: issue.id, labelId: qId });
		},

		async proposeClose(issue: NowRef, reason: string | undefined): Promise<void> {
			await postComment(issue.id, `**Close proposed** — ${reason ?? "work complete"} (omp session ${new Date().toISOString().slice(0, 10)})`);
			const qId = await queueLabelId();
			if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: issue.id, labelId: qId });
		},

		async archiveIssue(issue: NowRef): Promise<void> {
			const arch = await gql<{ issueArchive: { success: boolean } }>(`mutation($id:String!){ issueArchive(id:$id){success} }`, { id: issue.id });
			if (!arch.issueArchive.success) throw new Error(`Linear refused the archive (success:false) for ${issue.key} — nothing hidden`);
		},

		async reviseWork(issue: NowRef, fields: { title?: string; description?: string }): Promise<void> {
			const upd = await gql<{ issueUpdate: { success: boolean } }>(
				`mutation($id:String!,$input:IssueUpdateInput!){ issueUpdate(id:$id,input:$input){success} }`,
				{ id: issue.id, input: fields },
			);
			if (!upd.issueUpdate.success) throw new Error(`Linear refused the revision (success:false) for ${issue.key}`);
		},

		async recordHealth(project: string, health: "onTrack" | "atRisk" | "offTrack", body: string): Promise<void> {
			await gql(`mutation($input:ProjectUpdateCreateInput!){ projectUpdateCreate(input:$input){success} }`, {
				input: { projectId: await projectIdFor(project), health, body },
			});
		},

		async closeBlocker(now: NowRef, _carrier: WorkStateCarrier): Promise<string | null> {
			const workflow = await this.workflowState(now.id);
			if (!workflow.plan) return "Run /plan first.";
			if (!workflow.review) return "Run /summary first.";
			return null;
		},

		/** Owner-verdict close: state change first (the act), then the verdict comment (the record). */
		async closeWithVerdict(now: NowRef, outcome: "done" | "canceled", reason: string | undefined, _carrier: WorkStateCarrier, hooks: BackendHooks): Promise<string> {
			const stateId = await stateIdFor(outcome === "canceled" ? "canceled" : "completed");
			const upd = await gql<{ issueUpdate: { success: boolean } }>(
				`mutation($id:String!,$input:IssueUpdateInput!){ issueUpdate(id:$id,input:$input){success} }`,
				{ id: now.id, input: { stateId } },
			);
			if (!upd.issueUpdate.success) throw new Error(`Linear refused the close (issueUpdate success:false) for ${now.key} — no verdict recorded`);
			try {
				await postComment(
					now.id,
					`**Owner verdict in session: close${outcome === "canceled" ? " (canceled — not doing it)" : ""}** — ${reason ?? "done"} (omp session ${new Date().toISOString().slice(0, 10)})`,
				);
			} catch (e) {
				hooks.ui.notify(`verdict comment failed (${String(e)}) — close stands`, "warning");
			}
			return `${now.key} → ${outcome === "canceled" ? "Canceled" : "Done"} (owner verdict)`;
		},

		async summaryGate(now: NowRef, _carrier: WorkStateCarrier, _hooks: BackendHooks): Promise<SummaryGateOk | SummaryGateBlocked> {
			const workflow = await this.workflowState(now.id);
			if (!workflow.plan) {
				return { ok: true, issue: workflow.issue, warning: `No approved plan is stamped on ${now.key}. Review can run, but /done stays blocked; run /plan first.` };
			}
			return { ok: true, issue: workflow.issue, planHash: workflow.plan.hash };
		},
	};
}
