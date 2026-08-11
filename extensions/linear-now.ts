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
import { completeSimple } from "@oh-my-pi/pi-ai";
import { discoverAuthStorage, getAgentDir, type ExtensionAPI, type ExtensionContext, type ExtensionModelQuery, type Theme } from "@oh-my-pi/pi-coding-agent";
import { Ellipsis, matchesKey, truncateToWidth, type TUI, visibleWidth, wrapTextWithAnsi } from "@oh-my-pi/pi-tui";

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
	doneStateId?: string;
	canceledStateId?: string;
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

const HEALTH_GLYPH: Record<string, string> = { onTrack: "🟢", atRisk: "🟡", offTrack: "🔴" };
const STATE_BAND: Record<string, number> = { started: 0, planned: 1 };
const DONE_SUFFIX: Record<string, string> = { completed: " (done)", canceled: " (done)" };

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
	let state: NowState = {};
	let digestPending = false;
	let digestInjectedThisSession = false;
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
		return { surfaces, capped: d.issues.nodes.length === 100 };
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
	const digestKeyByProvider = new Map<string, Promise<string | undefined>>();

	function digestApiKey(provider: string): Promise<string | undefined> {
		let p = digestKeyByProvider.get(provider);
		if (!p) {
			p = (async () => (await discoverAuthStorage(getAgentDir())).getApiKey(provider))();
			digestKeyByProvider.set(provider, p);
		}
		return p;
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

	/** Owner-verdict close: state change first (the act), then the verdict comment (the record). */
	async function closeWithVerdict(issueId: string, identifier: string, outcome: "done" | "canceled", reason: string | undefined, ctx: ExtensionContext): Promise<string> {
		const stateId = await stateIdFor(outcome === "canceled" ? "canceled" : "completed");
		const upd = await gql<{ issueUpdate: { success: boolean } }>(
			`mutation($id:String!,$input:IssueUpdateInput!){ issueUpdate(id:$id,input:$input){success} }`,
			{ id: issueId, input: { stateId } },
		);
		if (!upd.issueUpdate.success) throw new Error(`Linear refused the close (issueUpdate success:false) for ${identifier} — no verdict recorded`);
		try {
			const cmt = await gql<{ commentCreate: { success: boolean } }>(`mutation($input:CommentCreateInput!){ commentCreate(input:$input){success} }`, {
				input: {
					issueId,
					body: `**Owner verdict in session: close${outcome === "canceled" ? " (canceled — not doing it)" : ""}** — ${reason ?? "done"} (omp session ${new Date().toISOString().slice(0, 10)})`,
				},
			});
			if (!cmt.commentCreate.success) throw new Error("commentCreate returned success:false");
		} catch (e) {
			try {
				ctx.ui.notify(`verdict comment failed (${String(e)}) — close stands`, "warning");
			} catch {
				/* headless */
			}
		}
		if (issueId === state.issueId) await clearNow(ctx, true);
		return `${identifier} → ${outcome === "canceled" ? "Canceled" : "Done"} (owner verdict)`;
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
		models = ctx.models;
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
					return;
				}
				if (arg) {
					const issue = await findIssue(arg);
					await setNow({ id: issue.id, identifier: issue.identifier, title: issue.title, project: issue.project?.name }, ctx);
					return;
				}
				const map = await mapData();
				if (!map.surfaces.length || !map.surfaces.some(s => s.issues.length)) {
					ctx.ui.notify("No open issues found", "warning");
					return;
				}
				const pick = await ctx.ui.custom<MapIssue | undefined>(nowWindowFactory(map), { overlay: true });
				if (!pick) return;
				const yes = await ctx.ui.confirm(`Make ${pick.identifier} your NOW?`, `"${pick.title}"\n${pick.project ?? ""}`);
				if (!yes) return;
				await setNow({ id: pick.id, identifier: pick.identifier, title: pick.title, project: pick.project }, ctx);
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
				const CLOSE = "Close now — my verdict, moves to Done";
				const PROPOSE = "Propose close — comment + queue label";
				const CLEAR = "Just clear NOW — no close, no comment";
				const choice = await ctx.ui.select(
					`Done with ${identifier}?`,
					[
						{ label: CLOSE, description: `posts "Owner verdict in session: close" on ${identifier}` },
						{ label: PROPOSE, description: "you click the final close in the app (today's behavior)" },
						{ label: CLEAR },
					],
					{ helpText: "esc = keep NOW as is" },
				);
				if (choice === undefined) return;
				if (choice === CLOSE) {
					const yes = await ctx.ui.confirm(
						`This is your verdict — close ${identifier}?`,
						`"${title}"\n\nMoves to Done + posts the verdict comment. Not reversible from here.`,
					);
					if (!yes) return;
					ctx.ui.notify(await closeWithVerdict(issueId, identifier, "done", undefined, ctx), "info");
					return;
				}
				if (choice === CLEAR) {
					await clearNow(ctx, true);
					ctx.ui.notify(`${identifier} cleared from NOW (no close, no comment)`, "info");
					return;
				}
				await clearNow(ctx, true);
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
				ctx.ui.notify("/now (window) · /now <issue>|clear · /done (close-now|propose|clear) · /capture <text> · /linear status|digest", "info");
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
			"replacement for raw GraphQL). Other writes (create_issue, propose_close, update_health, set_now, close_issue, archive_issue) are",
			"owner-confirmed, ALWAYS two-phase: the first call writes nothing and returns a payload preview",
			"to show the owner verbatim; repeat with confirm:true only after his yes.",
			"create_issue queue:true adds the waiting-on-chris label at creation; queue_issue adds it to an",
			"existing issue — use them for ANYTHING parked on the owner (the decision queue is that label).",
			"Never assume a write landed without success:true. Never dump large result sets into prose; summarize.",
			"Closes are the owner's verdict: close_issue/archive_issue require his on-screen yes (two-phase); propose_close remains the async path.",
		].join(" "),
		parameters: z.object({
			action: z.enum(["get_issue", "tree", "waiting", "my_now", "comment", "create_issue", "queue_issue", "propose_close", "update_health", "set_now", "close_issue", "archive_issue"]),
			issue: z.string().optional().describe("Issue identifier like HOME-31 (get_issue, comment, queue_issue, propose_close, set_now, close_issue, archive_issue)"),
			title: z.string().optional().describe("Issue title (create_issue)"),
			description: z.string().optional().describe("Issue description markdown (create_issue)"),
			project: z.string().optional().describe("Project name (create_issue target, update_health)"),
			health: z.enum(["onTrack", "atRisk", "offTrack"]).optional().describe("Project health (update_health)"),
			body: z.string().optional().describe("Comment body, one-line update, or close reason (comment, update_health, propose_close)"),
			queue: z.boolean().optional().describe("create_issue: also add the waiting-on-chris label so the issue lands in the owner decision queue"),
			outcome: z.enum(["done", "canceled"]).optional().describe("close_issue: done (default) or canceled for never-doing-it"),
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
					case "close_issue": {
						if (!params.issue) return deny("issue identifier required");
						const issue = await findIssue(params.issue);
						const outcome = params.outcome ?? "done";
						const gate = await ownerGate(
							ctx,
							"Model wants to CLOSE an issue — this is your verdict",
							`${issue.identifier} ${issue.title}\n→ moves to ${outcome === "canceled" ? "Canceled (never doing it)" : "Done"}\nReason: ${params.body ?? "(none given)"}\n\nThis is not a proposal — confirming closes it and posts "Owner verdict in session: close".`,
							params.confirm,
							true,
						);
						if (!gate.approved) return deny(gate.preview ?? "owner declined — issue NOT closed");
						return okText(await closeWithVerdict(issue.id, issue.identifier, outcome, params.body, ctx));
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
