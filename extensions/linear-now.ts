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
import { readFileSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const TEAM_KEY = "HOME";
const NOW_LABEL = "now";
const QUEUE_LABEL = "waiting-on-chris";
const KEY_FILE = join(homedir(), ".config", "linear.env");
const CACHE_FILE = join(homedir(), ".omp", "agent", "linear-now.json");
const API = "https://api.linear.app/graphql";

interface NowState {
	issueId?: string;
	identifier?: string;
	title?: string;
	project?: string;
	setAt?: number;
	lastDone?: { identifier: string; title: string; at: number };
	teamId?: string;
	nowLabelId?: string;
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
	let state: NowState = {};
	let digestPending = false;
	let digestInjectedThisSession = false;

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
			issueId: state.issueId,
			identifier: state.identifier,
			title: state.title,
			project: state.project,
			setAt: state.setAt,
		});
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
			`query($name:String!){ issueLabels(filter:{name:{eq:$name}}){nodes{id name}} }`,
			{ name: NOW_LABEL },
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

	async function labelHolder(): Promise<{ id: string; identifier: string; title: string; project?: string } | null> {
		const d = await gql<{ issues: { nodes: { id: string; identifier: string; title: string; project?: { name: string } }[] } }>(
			`query($label:String!){ issues(first:2,filter:{labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{id identifier title project{name}}} }`,
			{ label: NOW_LABEL },
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

	function footer(ctx: ExtensionContext) {
		try {
			const theme = ctx.ui.theme;
			if (state.identifier) {
				const elapsed = state.setAt ? ` · ${fmtElapsed(Date.now() - state.setAt)}` : "";
				const proj = state.project ? `${state.project} · ` : "";
				ctx.ui.setStatus("linear-now", theme.fg("accent", `◆ NOW · ${proj}${state.identifier} ${state.title ?? ""}${theme.fg("dim", elapsed)}`));
			} else if (state.lastDone) {
				ctx.ui.setStatus(
					"linear-now",
					theme.fg("dim", `✓ last: ${state.lastDone.identifier} ${state.lastDone.title} · ${fmtElapsed(Date.now() - state.lastDone.at)} ago — /now to refocus`),
				);
			} else {
				ctx.ui.setStatus("linear-now", theme.fg("dim", "◇ no NOW — /now to pick"));
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

	async function buildDigest(): Promise<string> {
		const d = await gql<{
			projects: { nodes: { name: string; state: string; health?: string }[] };
			issues: { nodes: { identifier: string; title: string }[] };
		}>(
			`query($label:String!){ projects(first:50){nodes{name state health}} issues(first:10,filter:{labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{identifier title}} }`,
			{ label: QUEUE_LABEL },
		);
		const inflight = d.projects.nodes
			.filter(p => p.state === "started")
			.map(p => `${p.name} [${p.health ?? "?"}]`)
			.join(" · ");
		const queue = d.issues.nodes.map(i => `${i.identifier} ${i.title}`).join(" | ");
		const now = state.identifier ? `NOW: ${state.project ? `${state.project} · ` : ""}${state.identifier} ${state.title}` : "NOW: unset — /now to pick";
		return [
			"── Linear bookend (linear.app/spec-kit) ──",
			now,
			`IN FLIGHT: ${inflight || "none"}`,
			`NEEDS CHRIS (${d.issues.nodes.length}): ${queue || "empty"}`,
			"CLOSE CONTRACT: update touched projects (health + one line) · triage captures · propose closes (owner verdict closes) · archive >1-day-closed",
		].join("\n");
	}

	// ---- lifecycle: digest injection (new + resume), footer, state restore ----

	pi.on("session_start", async (_e, ctx) => {
		await loadCache();
		// session entry wins over cache for NOW restore (survives cache loss)
		try {
			for (const entry of ctx.sessionManager.getBranch()) {
				if (entry.type === "custom" && (entry as { customType?: string }).customType === "linear-now") {
					const data = (entry as { data?: NowState }).data;
					if (data) Object.assign(state, data);
				}
			}
		} catch {
			/* fresh session */
		}
		digestPending = true;
		digestInjectedThisSession = false;
		footer(ctx);
	});

	pi.on("session_switch", async (event, ctx) => {
		if (event.reason === "resume" || event.reason === "new") {
			digestPending = true;
			digestInjectedThisSession = false;
		}
		footer(ctx);
	});

	pi.on("before_agent_start", async () => {
		if (!digestPending || digestInjectedThisSession) return;
		digestPending = false;
		try {
			const digest = await buildDigest();
			digestInjectedThisSession = true;
			return { message: { customType: "linear-digest", content: digest } };
		} catch (e) {
			pi.logger.warn("linear-now: digest failed", { error: String(e) });
			return { message: { customType: "linear-digest", content: `[linear] digest unavailable (${String(e)}) — session unblocked` } };
		}
	});

	pi.on("turn_start", async (_e, ctx) => footer(ctx));
	pi.on("turn_end", async (_e, ctx) => footer(ctx));

	// ---- commands ----

	pi.registerCommand("now", {
		description: "Set/show the NOW issue (bare = picker, or /now HOME-31, /now clear)",
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
					return;
				}
				if (arg) {
					const issue = await findIssue(arg);
					await setNow({ id: issue.id, identifier: issue.identifier, title: issue.title, project: issue.project?.name }, ctx);
					return;
				}
				const d = await gql<{ issues: { nodes: { id: string; identifier: string; title: string; project?: { name: string } }[] } }>(
					`query($team:String!){ issues(first:25,filter:{team:{key:{eq:$team}},state:{type:{nin:["completed","canceled"]}}},orderBy:updatedAt){nodes{id identifier title project{name}}} }`,
					{ team: TEAM_KEY },
				);
				const nodes = d.issues.nodes;
				if (!nodes.length) {
					ctx.ui.notify("No open issues found", "warning");
					return;
				}
				const labels = nodes.map(n => `${n.identifier} · ${n.project?.name ?? "no project"} · ${n.title}`);
				const picked = await ctx.ui.select("Set NOW — which issue?", labels);
				if (!picked) return;
				const issue = nodes[labels.indexOf(picked)];
				await setNow({ id: issue.id, identifier: issue.identifier, title: issue.title, project: issue.project?.name }, ctx);
			} catch (e) {
				ctx.ui.notify(`/now failed: ${String(e)}`, "error");
			}
		},
	});

	pi.registerCommand("done", {
		description: "Clear NOW + propose the close (owner verdict closes)",
		handler: async (_args, ctx) => {
			if (!state.issueId || !state.identifier) {
				ctx.ui.notify("No NOW set", "warning");
				return;
			}
			const { issueId, identifier, title } = { issueId: state.issueId, identifier: state.identifier, title: state.title };
			try {
				const propose = await ctx.ui.confirm(
					`Done with ${identifier}?`,
					`"${title}"\n\nThis will:\n1. Clear the NOW pointer\n2. Comment "Close proposed from omp session" on ${identifier}\n3. Add the ${QUEUE_LABEL} label so it lands in your decision queue\n\nThe issue is NOT closed — your verdict closes it.`,
				);
				await clearNow(ctx, true);
				if (propose) {
					await gql(`mutation($input:CommentCreateInput!){ commentCreate(input:$input){success} }`, {
						input: { issueId, body: `**Close proposed** from omp session ${new Date().toISOString().slice(0, 10)} — owner verdict closes.` },
					});
					const d = await gql<{ issueLabels: { nodes: { id: string }[] } }>(
						`query($name:String!){ issueLabels(filter:{name:{eq:$name}}){nodes{id}} }`,
						{ name: QUEUE_LABEL },
					);
					const qId = d.issueLabels.nodes[0]?.id;
					if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: issueId, labelId: qId });
					ctx.ui.notify(`${identifier} → close proposed, in your queue`, "info");
				} else {
					ctx.ui.notify(`${identifier} cleared from NOW (no proposal)`, "info");
				}
			} catch (e) {
				ctx.ui.notify(`/done failed: ${String(e)}`, "error");
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
				const target = state.project ? `project "${state.project}"` : "team HOME (no project)";
				const ok = await ctx.ui.confirm("File this capture?", `"${text}"\n→ ${target}`);
				if (!ok) return;
				let projectId: string | undefined;
				if (state.project) {
					const d = await gql<{ projects: { nodes: { id: string; name: string }[] } }>(
						`query($name:String!){ projects(filter:{name:{eq:$name}}){nodes{id name}} }`,
						{ name: state.project },
					);
					projectId = d.projects.nodes[0]?.id;
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
				ctx.ui.notify("/now [issue|clear] · /done · /capture <text> · /linear status|digest", "info");
				return;
			}
			const lines: string[] = [];
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
			"Reads are free: get_issue, tree, waiting, my_now. comment posts evidence immediately (the bounded",
			"replacement for raw GraphQL). Other writes (create_issue, propose_close, update_health, set_now) are",
			"owner-confirmed, ALWAYS two-phase: the first call writes nothing and returns a payload preview",
			"to show the owner verbatim; repeat with confirm:true only after his yes.",
			"create_issue queue:true adds the waiting-on-chris label at creation; queue_issue adds it to an",
			"existing issue — use them for ANYTHING parked on the owner (the decision queue is that label).",
			"Never assume a write landed without success:true. Never dump large result sets into prose; summarize.",
			"Issues close only by owner verdict — propose, never close.",
		].join(" "),
		parameters: z.object({
			action: z.enum(["get_issue", "tree", "waiting", "my_now", "comment", "create_issue", "queue_issue", "propose_close", "update_health", "set_now"]),
			issue: z.string().optional().describe("Issue identifier like HOME-31 (get_issue, comment, queue_issue, propose_close, set_now)"),
			title: z.string().optional().describe("Issue title (create_issue)"),
			description: z.string().optional().describe("Issue description markdown (create_issue)"),
			project: z.string().optional().describe("Project name (create_issue target, update_health)"),
			health: z.enum(["onTrack", "atRisk", "offTrack"]).optional().describe("Project health (update_health)"),
			body: z.string().optional().describe("Comment body, one-line update, or close reason (comment, update_health, propose_close)"),
			queue: z.boolean().optional().describe("create_issue: also add the waiting-on-chris label so the issue lands in the owner decision queue"),
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
				switch (params.action) {
					case "my_now":
						return okText(state.identifier ? `NOW: ${state.project ? `${state.project} · ` : ""}${state.identifier} ${state.title}` : "NOW unset");
					case "waiting": {
						const d = await gql<{ issues: { nodes: { identifier: string; title: string }[] } }>(
							`query($label:String!){ issues(first:15,filter:{labels:{some:{name:{eq:$label}}},state:{type:{nin:["completed","canceled"]}}}){nodes{identifier title}} }`,
							{ label: QUEUE_LABEL },
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
						const d = await gql<{ issue: { identifier: string; title: string; description?: string; state: { name: string }; project?: { name: string }; labels: { nodes: { name: string }[] } } }>(
							`query($id:String!){ issue(id:$id){identifier title description state{name} project{name} labels{nodes{name}}} }`,
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
						const issue = await findIssue(params.issue);
						await gql(`mutation($input:CommentCreateInput!){ commentCreate(input:$input){success} }`, {
							input: { issueId: issue.id, body: params.body },
						});
						return okText(`comment posted on ${issue.identifier}`);
					}
					case "create_issue": {
						if (!params.title) return deny("title required");
						const target = params.project ?? state.project;
						const gate = await ownerGate(
							ctx,
							"Model wants to file an issue",
							`"${params.title}"\n→ ${target ? `project ${target}` : "team HOME"}${params.queue ? `\n→ + ${QUEUE_LABEL} label (lands in your decision queue)` : ""}\n\n${(params.description ?? "").slice(0, 400)}`,
							params.confirm,
							true,
						);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — issue NOT created");
						let projectId: string | undefined;
						if (target) {
							const dp = await gql<{ projects: { nodes: { id: string }[] } }>(`query($name:String!){ projects(filter:{name:{eq:$name}}){nodes{id}} }`, { name: target });
							projectId = dp.projects.nodes[0]?.id;
						}
						const created = await gql<{ issueCreate: { issue: { id: string; identifier: string } } }>(
							`mutation($input:IssueCreateInput!){ issueCreate(input:$input){issue{id identifier}} }`,
							{ input: { teamId: await teamId(), title: params.title, description: params.description, ...(projectId ? { projectId } : {}) } },
						);
						if (params.queue) {
							const dl = await gql<{ issueLabels: { nodes: { id: string }[] } }>(`query($name:String!){ issueLabels(filter:{name:{eq:$name}}){nodes{id}} }`, { name: QUEUE_LABEL });
							const qId = dl.issueLabels.nodes[0]?.id;
							if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: created.issueCreate.issue.id, labelId: qId });
						}
						return okText(`created ${created.issueCreate.issue.identifier}${params.queue ? ` + ${QUEUE_LABEL}` : ""}`, { identifier: created.issueCreate.issue.identifier });
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
						const dl = await gql<{ issueLabels: { nodes: { id: string }[] } }>(`query($name:String!){ issueLabels(filter:{name:{eq:$name}}){nodes{id}} }`, { name: QUEUE_LABEL });
						const qId = dl.issueLabels.nodes[0]?.id;
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
						const dl = await gql<{ issueLabels: { nodes: { id: string }[] } }>(`query($name:String!){ issueLabels(filter:{name:{eq:$name}}){nodes{id}} }`, { name: QUEUE_LABEL });
						const qId = dl.issueLabels.nodes[0]?.id;
						if (qId) await gql(`mutation($id:String!,$labelId:String!){ issueAddLabel(id:$id,labelId:$labelId){success} }`, { id: issue.id, labelId: qId });
						return okText(`close proposed on ${issue.identifier} — owner verdict closes`);
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
