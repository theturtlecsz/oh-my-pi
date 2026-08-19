/**
 * workflow/host.ts — the owner-workflow host (HOME-147, HOME-149).
 *
 * Session state, the NOW pointer + footer, the NOW window, the in-card
 * digest engine, the obligation/closeout locks, owner commands (/now /done
 * /capture /work), the bounded tool, transcript-bound confirmation receipts,
 * and the audit-receipt bridge consumer. A WorkflowBackend supplies storage.
 */
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { type AuthStorage, completeSimple } from "@oh-my-pi/pi-ai";
import { discoverAuthStorage, getAgentDir, type ExtensionAPI, type ExtensionContext, type ExtensionModelQuery, type Theme } from "@oh-my-pi/pi-coding-agent";
import { Ellipsis, matchesKey, truncateToWidth, type TUI, visibleWidth, wrapTextWithAnsi } from "@oh-my-pi/pi-tui";
import { prompt } from "@oh-my-pi/pi-utils";
import centerPromptTemplate from "./center-prompt.md" with { type: "text" };
import digestPromptTemplate from "./digest-prompt.md" with { type: "text" };
import kindDescriptionText from "./kind-description.md" with { type: "text" };
import lockRefusalText from "./lock-refusal.md" with { type: "text" };
import sequenceText from "./sequence.md" with { type: "text" };
import toolDescriptionTemplate from "./tool-description.md" with { type: "text" };
import {
	BatchPartialError,
	CLOSEOUT_BOUNDARY,
	STOP_REMINDER_BOUNDARY,
	type BackendIssue,
	type CenterSnapshot,
	type EvidenceKind,
	type EvidenceMeta,
	type GoalTree,
	type IssueDetail,
	type MapSurface,
	type NowRef,
	type TreeItem,
	type WorkflowBackend,
	type WorkStateCarrier,
} from "./backend";
import { confirmWrite, resetConfirmations } from "./confirm";
import { claimAuditReceipt, commitAuditReceipt, releaseAuditReceipt, reportSha256 } from "./audit-bridge";
import { dirtyPaths } from "./git";

/** Tool actions — the canonical action set for the `work` tool. */
export type CanonicalAction =
	| "get_work"
	| "tree"
	| "waiting"
	| "my_now"
	| "status"
	| "append_evidence"
	| "create_work"
	| "queue_work"
	| "revise_work"
	| "set_now"
	| "record_health"
	| "request_closeout"
	| "cancel_work";

const ACTIONS = [
	"get_work", "tree", "waiting", "my_now", "status",
	"append_evidence", "create_work", "queue_work", "revise_work",
	"set_now", "record_health", "request_closeout", "cancel_work",
] as const;
const ACTION_ENUM: [string, ...string[]] = [...ACTIONS];
const TOOL_NAME = "work";
const ADMIN_COMMAND = "work";
const TOOL_LABEL = "Work Ledger";

/** ONE owner-facing workflow surface (plan §2) — identical name, enum,
 *  description, and action set under either backend; backend words survive
 *  only in queueNoun/markerFile/scopeFix/teamNoun. Prompt text lives in the
 *  static .md files beside this host (repo rule), never inline. */
export const WORKFLOW_SEQUENCE = sequenceText.trim();
const LOCK_REFUSAL = lockRefusalText.trim();
const KIND_DESCRIPTION = kindDescriptionText.trim();

export interface HostConfig {
	backend: WorkflowBackend;
	/** "the ledger" — scope/preview prose. */
	teamNoun: string;
	/** Session custom-entry type ("work-now"). */
	entryType: string;
	/** Accept a persisted session entry as this backend's NOW state. */
	acceptEntry(data: Record<string, unknown>): boolean;
	/** Stop-hook continuation hint for the closeout checkpoint (body guidance). */
	reviewCheckpointHint: string;
}
interface HostNowState {
	issueId?: string;
	identifier?: string;
	title?: string;
	project?: string;
	setAt?: number;
	lastDone?: { identifier: string; title: string; at: number };
	treeCounts?: { done: number; total: number; stuck: number; onyou: number; at: number };
	executingIssue?: NowRef;
	approvedPlan?: { hash: string; at: number };
	obligationHandoff?: { armed: boolean; blockedOnce: boolean };
	obligationReview?: { armed: boolean; blockedOnce: boolean };
	/** Opaque backend carrier (work: candidate ids/shas); persisted verbatim. */
	carrier?: unknown;
}

const TREE_GLYPH: Record<TreeItem["bucket"], string> = { done: "✔", working: "▶", stuck: "✖", onyou: "✋", next: "○" };
const HEALTH_GLYPH: Record<string, string> = { onTrack: "🟢", atRisk: "🟡", offTrack: "🔴" };
const HEALTH_WORDS: Record<string, string> = { onTrack: "on track", atRisk: "at risk", offTrack: "off track" };

function fmtElapsed(ms: number): string {
	const m = Math.floor(ms / 60000);
	if (m < 60) return `${m}m`;
	return `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ""}`;
}

/** Deterministic plain-words render — zero LLM, zero paths/hashes. Identifiers
 *  stay: they are the owner's own handles, visible on his tracker. */
export function renderGoalTree(t: GoalTree): string[] {
	const lines = [`GOAL: ${t.goal}${t.promise ? ` — ${t.promise}` : ""}${t.health ? ` [${HEALTH_WORDS[t.health] ?? t.health}]` : ""}`];
	const by = (b: TreeItem["bucket"]) => t.items.filter(i => i.bucket === b);
	for (const i of by("working")) lines.push(`  ${TREE_GLYPH.working} ${i.key} ${i.title}${i.isNow ? " ← working now" : ""}`);
	for (const i of by("stuck")) lines.push(`  ${TREE_GLYPH.stuck} ${i.key} ${i.title} — stuck: ${i.blocker}`);
	for (const i of by("onyou")) lines.push(`  ${TREE_GLYPH.onyou} ${i.key} ${i.title} — waiting on you`);
	const nowTitle = t.items.find(i => i.isNow)?.title ?? "";
	const parts = [`${t.counts.done} of ${t.counts.total} pieces done.`];
	if (nowTitle) parts.push(`Working on: ${nowTitle}.`);
	if (t.counts.stuck) parts.push(`${t.counts.stuck} stuck.`);
	if (t.counts.onyou) parts.push(`${t.counts.onyou} waiting on you.`);
	const queued = by("next").length;
	if (queued) parts.push(`${queued} queued next.`);
	lines.push(parts.join(" "));
	return lines;
}

function sectionItems(content: string, heading: "Approach" | "Verification"): string[] {
	const lines = content.split("\n");
	const start = lines.findIndex(line => line.trim() === `## ${heading}`);
	if (start < 0) return [];
	const items: string[] = [];
	for (const line of lines.slice(start + 1)) {
		if (/^##\s+/.test(line)) break;
		const match = /^(?:\d+\.|-)\s+(.+)$/.exec(line);
		if (match?.[1]) items.push(match[1].trim());
	}
	return items;
}

/** Deterministic /center prompt — verified snapshot facts + the four-section
 *  contract. The agent writes the orientation; this never does. */
export function renderCenterPrompt(snapshot: CenterSnapshot, scope: string, takenAt: string): string {
	const activity = snapshot.activity;
	return prompt.render(centerPromptTemplate, {
		takenAt,
		scope,
		nowLine: snapshot.now ? `${snapshot.now.project ? `${snapshot.now.project} · ` : ""}${snapshot.now.key} ${snapshot.now.title}` : undefined,
		progressLine: snapshot.progress ? `${snapshot.progress.done} of ${snapshot.progress.total} pieces done · ${snapshot.progress.onyou} waiting on Chris` : undefined,
		readyRows: snapshot.ready.rows,
		readyTotal: snapshot.ready.total,
		readyMore: Math.max(0, snapshot.ready.total - snapshot.ready.rows.length) || undefined,
		waitingRows: snapshot.waiting.rows,
		waitingTotal: snapshot.waiting.total,
		waitingMore: Math.max(0, snapshot.waiting.total - snapshot.waiting.rows.length) || undefined,
		...("unavailable" in activity
			? { activityUnavailable: activity.unavailable }
			: {
					activityRows: activity.rows,
					activityTotal: activity.total,
					activityMore: Math.max(0, activity.total - activity.rows.length) || undefined,
				}),
	});
}

/** Literal prefix of every rendered centering prompt — before_agent_start
 *  recognizes the centering turn by it. Derived from the template so the
 *  header and the renderer can never drift. */
const CENTER_PROMPT_HEADER = centerPromptTemplate.slice(0, centerPromptTemplate.indexOf("{{"));

const DIGEST_LABELS = new Set(["PROBLEM", "MAKEUP", "DECISIONS", "BLOCKERS", "ON-YOU", "EVIDENCE", "STATE", "NEXT", "SUGGEST"]);

/** Reads are free at any depth; every other canonical action is a write. */
const READ_ACTIONS: ReadonlySet<CanonicalAction> = new Set(["get_work", "tree", "waiting", "my_now", "status"]);

/** Tool params — the extension API types zod `parameters` as unknown at execute. */
interface WorkflowToolParams {
	action: string;
	work?: string;
	title?: string;
	description?: string;
	project?: string;
	health?: "onTrack" | "atRisk" | "offTrack";
	body?: string;
	kind?: EvidenceKind;
	queue?: boolean;
	batch?: { title: string; description?: string; blocks?: number[] }[];
	confirm?: boolean;
	confirmation_id?: string;
}

export function createWorkflowHost(cfg: HostConfig) {
	const backend = cfg.backend;
	const CACHE_FILE = join(homedir(), ".omp", "agent", backend.cacheFile);
	const projectFilter = resolveProjectMarker(backend.markerFile);
	const gitRooted = spawnSync("git", ["-C", process.cwd(), "rev-parse", "--show-toplevel"], { encoding: "utf8" }).status === 0;
	const scopeLine = !gitRooted
		? `SCOPE: not a git repo — /now unfiltered, filing lands on ${cfg.teamNoun}; git init + ${backend.markerFile} to scope this directory`
		: projectFilter
			? `SCOPE: "${projectFilter}" (${backend.markerFile}) — /now + filing scoped to this project; NOW itself stays global`
			: `SCOPE: no ${backend.markerFile} marker — /now unfiltered; unscoped writes refused. Day 1: ${backend.scopeFix}`;

	/** Native write gate: null = scoped, string = refusal reason. */
	function unscopedRefusal(target: string | undefined): string | null {
		if (target || !gitRooted) return null;
		return `refused: unscoped git repo (no ${backend.markerFile} marker, no explicit project) — ${backend.scopeFix}`;
	}
	/** Filing target: explicit param wins, then the repo marker. The global NOW's
	 *  project is NOT an implicit target inside a git repo. Outside git repos the
	 *  NOW fallback still applies. */
	function fileTarget(explicit: string | undefined): string | undefined {
		return explicit ?? projectFilter ?? (gitRooted ? undefined : state.project);
	}

	let state: HostNowState = {};
	let digestPending = false;
	let digestInjectedThisSession = false;
	let intakeActive = false;
	let intakeSelected = false;
	let planTarget: NowRef | undefined;
	let summaryAuthorized = false;
	let summaryAttemptFinished = false;
	let preExistingDirtyPaths: string[] = [];
	let models: ExtensionModelQuery | undefined;

	// HOME-114 mechanical lock: flips ONLY on host-observed owner entry of /summary
	// or /done. FAIL CLOSED on unknown depth: subagent sessions never unlock.
	let closeoutAuthorized = false;
	const ownerSession = (ctx: { taskDepth?: number } | undefined): boolean => ctx?.taskDepth === 0;

	// OMP-25 /center: one read-only, tool-less orientation turn at a time.
	// Tool isolation flips INSIDE the centering turn's before_agent_start —
	// sendUserMessage is fire-and-forget (its async rejection never reaches the
	// command), so a failed injection must never have touched the tool set.
	let centerPending = false; // /center sent its prompt; the turn has not started yet
	let centerActive = false; // the centering turn is running with tools disabled
	let centerSavedTools: string[] | undefined;
	/** Restores the exact pre-center tool set — every exit path (agent_end,
	 *  session switch, shutdown) routes through here. */
	async function restoreCenterTools(): Promise<void> {
		centerPending = false;
		if (!centerActive) return;
		const saved = centerSavedTools;
		centerActive = false;
		centerSavedTools = undefined;
		if (!saved) return;
		try {
			await piRef.setActiveTools(saved);
		} catch (error) {
			piRef.logger.warn(`${TOOL_NAME}-now: center tool restore failed`, { error: String(error) });
		}
	}

	const pendingNotices: string[] = [];

	function currentNowRef(): NowRef | undefined {
		if (!state.issueId || !state.identifier) return undefined;
		return { id: state.issueId, key: state.identifier, title: state.title ?? "", ...(state.project ? { project: state.project } : {}) };
	}
	function carrier(): WorkStateCarrier {
		return backend.readCarrier(state.carrier);
	}
	function mergeCarrier(patch: WorkStateCarrier | undefined): void {
		if (!patch) return;
		state.carrier = { ...carrier(), ...patch };
	}
	function hooksFor(ctx: ExtensionContext) {
		return {
			ui: {
				confirm: (title: string, body: string) => ctx.ui.confirm(title, body),
				notify: (msg: string, level?: "info" | "warning" | "error") => ctx.ui.notify(msg, level ?? "info"),
			},
			cwd: process.cwd(),
			preExistingDirtyPaths,
			notices: pendingNotices,
		};
	}

	async function loadCache() {
		try {
			state = JSON.parse(await readFile(CACHE_FILE, "utf8")) as HostNowState;
		} catch {
			state = {};
		}
	}
	let piRef: ExtensionAPI;
	/** Slash-command delivery receipt (HOME-147): tool writes bind op ids to
	 *  their toolResult; command flows (/now, /plan, /summary, /done, /capture)
	 *  have no toolResult, so each persists its newly delivered ops TOGETHER
	 *  with the recoverable outcome — the resumed transcript both proves
	 *  delivery (claim ack at session start) and stays intelligible. */
	const recordedOps = new Set<string>();
	function recordDeliveredOutcome(outcome: string): void {
		const delivered = backend.deliveredOps?.() ?? [];
		const fresh = delivered.filter(id => !recordedOps.has(id));
		if (fresh.length === 0) return;
		for (const id of fresh) recordedOps.add(id);
		try {
			piRef.appendEntry(`${cfg.entryType}-ops`, { ops: fresh, outcome, at: new Date().toISOString() });
		} catch {
			/* receipt lost — claims linger, retry replays stored results */
		}
	}
	/** Durable delivery ack (HOME-147): a pending-operation claim is released
	 *  only when a session RELOADED FROM DISK contains the receipt — the
	 *  toolResult carrying op ids in its details, or a command-outcome entry.
	 *  NEVER scan the live branch mid-session: entries are recorded in memory
	 *  before the file append lands (append failures latch), and extension
	 *  events fire pre-persist — neither proves durability. Mid-session
	 *  duplicates therefore replay stored results (fail closed). Returns the
	 *  recovered outcome lines so the caller can inject them into the resumed
	 *  transcript — appendEntry is extension state, not a transcript message,
	 *  so stored outcomes are surfaced here, not merely stored. */
	async function ackPersistedOps(ctx: ExtensionContext): Promise<string[]> {
		if (!backend.ackOps) return [];
		const ids: string[] = [];
		const outcomes: string[] = [];
		for (const entry of ctx.sessionManager.getBranch()) {
			if (entry.type === "message") {
				// Tool writes: op ids bound to the toolResult's own details — the
				// entry exists on disk iff the result does (no ordering window).
				const message = (entry as { message?: unknown }).message as
					| { role?: string; toolName?: string; details?: unknown }
					| undefined;
				if (!message || message.role !== "toolResult" || message.toolName !== TOOL_NAME) continue;
				const details = message.details;
				if (details && typeof details === "object" && Array.isArray((details as { opIds?: unknown }).opIds)) {
					for (const id of (details as { opIds: unknown[] }).opIds) if (typeof id === "string") ids.push(id);
				}
				continue;
			}
			if (entry.type !== "custom" || !("customType" in entry) || entry.customType !== `${cfg.entryType}-ops`) continue;
			// Slash-command writes: the entry carries the full recoverable outcome
			// alongside the op ids, so the resumed transcript stays intelligible.
			const data = ("data" in entry ? entry.data : undefined) as { ops?: unknown; outcome?: unknown } | undefined;
			if (data && Array.isArray(data.ops)) for (const id of data.ops) if (typeof id === "string") ids.push(id);
			if (data && typeof data.outcome === "string") outcomes.push(data.outcome);
		}
		if (ids.length > 0) await backend.ackOps(ids);
		return outcomes;
	}
	async function saveCache() {
		try {
			await writeFile(CACHE_FILE, JSON.stringify(state, null, 2));
		} catch (e) {
			piRef.logger.warn(`${TOOL_NAME}-now: cache write failed`, { error: String(e) });
		}
	}
	function persistSession() {
		piRef.appendEntry(cfg.entryType, {
			backend: backend.name,
			issueId: state.issueId,
			identifier: state.identifier,
			title: state.title,
			project: state.project,
			setAt: state.setAt,
			executingIssue: state.executingIssue,
			approvedPlan: state.approvedPlan,
			obligationHandoff: state.obligationHandoff,
			obligationReview: state.obligationReview,
			carrier: state.carrier,
		});
	}

	// ---- HOME-122 workflow carrier ----

	function armExecution(issue: NowRef, hash: string) {
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

	function footer(ctx: ExtensionContext) {
		try {
			const theme = ctx.ui.theme;
			const warn = state.obligationHandoff?.armed || state.obligationReview?.armed ? " ⚠" : "";
			ctx.ui.setStatus(
				`${cfg.entryType}-current`,
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
				ctx.ui.setStatus(`${cfg.entryType}`, theme.fg("accent", `◆ ${bits.filter(Boolean).join(" · ")}${warn}`));
			} else if (state.identifier) {
				const elapsed = state.setAt ? ` · ${fmtElapsed(Date.now() - state.setAt)}` : "";
				const proj = state.project ? ` · ${state.project}` : "";
				ctx.ui.setStatus(`${cfg.entryType}`, theme.fg("accent", `◆ NOW${proj}${theme.fg("dim", elapsed)}${warn}`));
			} else if (state.lastDone) {
				ctx.ui.setStatus(
					`${cfg.entryType}`,
					theme.fg("dim", `✓ last: ${state.lastDone.identifier} ${state.lastDone.title} · ${fmtElapsed(Date.now() - state.lastDone.at)} ago — /now to refocus${warn}`),
				);
			} else {
				ctx.ui.setStatus(`${cfg.entryType}`, theme.fg("dim", `◇ no NOW — /now to pick${warn}`));
			}
		} catch {
			/* headless: no footer */
		}
	}

	async function setNow(issue: NowRef, ctx: ExtensionContext) {
		await backend.setNowRemote(issue);
		// the opaque carrier belongs to the previous NOW (Work binds it to a
		// candidate commit) — never let it leak across a switch.
		if (state.issueId !== issue.id) state.carrier = undefined;
		state.issueId = issue.id;
		state.identifier = issue.key;
		state.title = issue.title;
		state.project = issue.project;
		state.setAt = Date.now();
		state.treeCounts = undefined; // previous goal's counts — next goalTree() refreshes
		await saveCache();
		persistSession();
		footer(ctx);
		ctx.ui.notify(`NOW → ${issue.key} ${issue.title}`, "info");
	}

	async function clearNow(ctx: ExtensionContext, markDone: boolean) {
		if (state.issueId) {
			try {
				await backend.clearNowRemote(state.issueId);
			} catch (e) {
				ctx.ui.notify(`${backend.serviceLabel} NOW not cleared remotely (${String(e)}) — cleared locally`, "warning");
			}
		}
		localClear(ctx, markDone);
	}

	/** Local-only clear: the remote side already moved (post-close) or never existed. */
	function localClear(ctx: ExtensionContext, markDone: boolean) {
		if (markDone && state.identifier) {
			state.lastDone = { identifier: state.identifier, title: state.title ?? "", at: Date.now() };
		}
		state.issueId = state.identifier = state.title = state.project = undefined;
		state.setAt = undefined;
		state.carrier = undefined;
		void saveCache();
		persistSession();
		footer(ctx);
	}

	/** Post-close cleanup shared by /done and the work adapter's verdict close. */
	function settleClosedIssue(now: NowRef, ctx: ExtensionContext) {
		if (state.executingIssue?.id === now.id) {
			state.executingIssue = undefined;
			state.approvedPlan = undefined;
			state.obligationHandoff = undefined;
			state.obligationReview = undefined;
			persistSession();
			void saveCache();
		}
		footer(ctx);
	}

	async function buildDigest(): Promise<string> {
		const now = currentNowRef();
		const treeLines = now
			? backend
					.goalTree(now)
					.then(t => {
						if (t) {
							state.treeCounts = { ...t.counts, at: Date.now() };
							void saveCache();
						}
						return t ? renderGoalTree(t) : ["TREE: no goal picked — /now to pick"];
					})
					.catch(e => [`TREE: unavailable (${String(e)}) — session unblocked`])
			: Promise.resolve(["TREE: no goal picked — /now to pick"]);
		const nowLine = state.identifier ? `NOW: ${state.project ? `${state.project} · ` : ""}${state.identifier} ${state.title}` : "NOW: unset — /now to pick";
		const contracts = [WORKFLOW_SEQUENCE, CLOSEOUT_BOUNDARY];
		let extras: string[];
		try {
			extras = await backend.digestExtras();
		} catch (e) {
			// Fail open with what we have: cached NOW + the tree's own honest line.
			return [backend.bookendTitle, nowLine, scopeLine, ...(await treeLines), `[${TOOL_NAME}] queue/in-flight unavailable (${String(e)}) — session unblocked`, ...contracts].join("\n");
		}
		return [backend.bookendTitle, nowLine, scopeLine, ...(await treeLines), ...extras, ...contracts].join("\n");
	}

	async function authorizeSummary(ctx: ExtensionContext): Promise<void> {
		if (summaryAuthorized && !summaryAttemptFinished) return;
		summaryAuthorized = true;
		summaryAttemptFinished = false;
		closeoutAuthorized = true;
		const now = currentNowRef();
		if (!now) {
			ctx.ui.notify("No NOW is selected. Review can run, but /done stays blocked; run /intake first.", "warning");
			return;
		}
		try {
			const gate = await backend.summaryGate(now, carrier(), hooksFor(ctx));
			if (!gate.ok) {
				ctx.ui.notify(gate.reason, "warning");
				return;
			}
			mergeCarrier(gate.carrier);
			if (gate.warning) {
				ctx.ui.notify(gate.warning, "warning");
				persistSession();
				await saveCache();
				// Receipt LAST (HOME-147): claims may be acked from it at startup,
				// so every state mutation + awaited persistence precedes it.
				recordDeliveredOutcome(`summary gate passed for ${now.key} — candidate finalized with warnings, awaiting verdict`);
				footer(ctx);
				return;
			}
			if (!gate.planHash) return;
			state.executingIssue = gate.issue;
			state.approvedPlan = { hash: gate.planHash, at: Date.now() };
			state.obligationReview = { armed: true, blockedOnce: false };
			persistSession();
			await saveCache();
			recordDeliveredOutcome(`summary gate passed for ${now.key} — candidate finalized, awaiting verdict`);
			footer(ctx);
		} catch (error) {
			ctx.ui.notify(`Could not load ${now.key} workflow state (${String(error)}). Review can run, but /done stays blocked.`, "warning");
		}
	}

	// ---- plan stamping ----

	function preparePlanStamp(plan: { planFilePath: string; planContent: string; title: string }): { hash: string; body: string } | { reason: string } {
		const approach = sectionItems(plan.planContent, "Approach");
		const verification = sectionItems(plan.planContent, "Verification");
		if (approach.length === 0 || verification.length === 0) {
			return { reason: "Plan approval requires non-empty ## Approach and ## Verification lists." };
		}
		const hash = Bun.SHA256.hash(plan.planContent, "hex");
		return {
			hash,
			body: [
				"**Plan approved**",
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

	// ---- in-card digest engine (owner ruling R4: auto per highlighted issue) ----

	const detailCache = new Map<string, IssueDetail>();
	const digestCache = new Map<string, { updatedAt: string; lines: string[] }>();
	let digestAuthStorage: Promise<AuthStorage> | undefined;

	function digestApiKey(provider: string): Promise<string | undefined> {
		// Cache the storage DISCOVERY, never the key: getApiKey() refreshes expired
		// OAuth bearers on each call — a session-lifetime key cache serves a dead
		// bearer to every digest after ~1h. A failed discovery evicts itself.
		if (!digestAuthStorage) {
			const p = discoverAuthStorage(getAgentDir());
			digestAuthStorage = p;
			p.catch(() => {
				if (digestAuthStorage === p) digestAuthStorage = undefined;
			});
		}
		return digestAuthStorage.then(storage => storage.getApiKey(provider));
	}

	async function digestFor(issue: BackendIssue, detail: IssueDetail, signal: AbortSignal): Promise<string[]> {
		const cached = digestCache.get(issue.id);
		if (cached && cached.updatedAt === issue.updatedAt) return cached.lines;
		const model = models?.resolve("@smol");
		if (!model) throw new Error("no smol model configured (@smol role) — digests never fall through to the session model");
		const key = await digestApiKey(model.provider);
		if (!key) throw new Error(`no credentials for ${model.provider}`);
		const res = await completeSimple(
			model,
			{ messages: [{ role: "user", content: prompt.render(digestPromptTemplate, { serviceLabel: backend.serviceLabel, waiting: issue.waiting ? "yes" : "no", digestPacket: detail.digestPacket }), timestamp: Date.now() }] },
			{ apiKey: key, disableReasoning: true, signal },
		);
		// pi-ai surfaces provider failures IN-BAND (stopReason "error"/"aborted" +
		// errorMessage, content empty) — completeSimple does not throw.
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
			return m?.[1] && DIGEST_LABELS.has(m[1]) ? m[1] : undefined;
		};
		const first = all.findIndex(l => labelOf(l) !== undefined);
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
		// Contract validation BEFORE caching — a bad reply is never cached.
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
		return (tui: TUI, theme: Theme, _kb: unknown, done: (r: BackendIssue | undefined) => void) => {
			type Row = { kind: "header"; s: MapSurface } | { kind: "issue"; i: BackendIssue };
			const rows: Row[] = [];
			for (const s of map.surfaces) {
				rows.push({ kind: "header", s });
				for (const i of s.issues) rows.push({ kind: "issue", i });
			}
			const issueRows: { rowIdx: number; issue: BackendIssue }[] = [];
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
			let listTop = 0;
			let paneScroll = 0; // →/← page offset INTO oversized pane content; reset on every highlight

			const current = () => issueRows[cursor]!.issue;

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
						det = await backend.issueDetail(issue.key);
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
				clearTimeout(dwell);
				inflight?.abort();
				gen++;
			}

			arm();

			return {
				render(width: number): string[] {
					// R1-v3 split layout (owner ruling 2026-08-11): static one-line list rows
					// LEFT, framed detail pane RIGHT consuming all remaining width.
					const w = Math.max(60, width);
					const listW = Math.min(48, Math.max(24, Math.floor(w * 0.38)));
					const paneW = w - listW - 1;
					const innerW = paneW - 4;
					const vh = Math.max(12, (process.stdout.rows ?? 40) - 6);
					const i = current();

					const cells = rows.map((row, r) => {
						if (row.kind === "header") {
							const s = row.s;
							return truncateToWidth(`${HEALTH_GLYPH[s.health ?? ""] ?? "◇"} ${s.name} · ${s.issues.length} open${s.waiting ? ` · ⏳${s.waiting} on Chris` : ""}`, listW, Ellipsis.Unicode, true);
						}
						const it = row.i;
						const isCur = issueRows[cursor]!.rowIdx === r;
						const cell = truncateToWidth(`  ${isCur ? "❯ " : "  "}${it.isNow ? "◆ " : ""}${it.key} · ${it.title}${it.waiting ? " ⏳" : ""}`, listW, Ellipsis.Unicode, true);
						return isCur ? cell : theme.fg("dim", cell);
					});
					const curRow = issueRows[cursor]!.rowIdx;
					if (curRow < listTop) listTop = Math.max(0, curRow - 1);
					if (curRow >= listTop + vh) listTop = curRow - vh + 1;
					listTop = Math.max(0, Math.min(listTop, Math.max(0, rows.length - vh)));
					const blankCell = " ".repeat(listW);
					const left: string[] = [];
					for (let r = listTop; r < listTop + vh; r++) left.push(cells[r] ?? blankCell);

					const content: string[] = [];
					const push = (line: string) => {
						for (const wl of wrapTextWithAnsi(line, innerW)) content.push(wl);
					};
					push(theme.fg("muted", `${i.state} · updated ${fmtElapsed(Date.now() - Date.parse(i.updatedAt))} ago · ${i.labels.join(",") || "no labels"}`));
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
							if (m?.[1] && DIGEST_LABELS.has(m[1])) push(theme.bold(theme.fg("accent", `${m[1]}:`)) + l.slice(m[0].length));
							else push(l);
						}
					}

					const bd = (s: string) => theme.fg("border", s);
					const bodyH = vh - 2;
					const paneOverflow = Math.max(0, content.length - bodyH);
					paneScroll = Math.min(paneScroll, paneOverflow);
					const head = truncateToWidth(`┌─ ${i.key} · ${i.title} `, paneW - 1, Ellipsis.Unicode);
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

	// ---- the extension ----

	return function workflowHost(pi: ExtensionAPI) {
		piRef = pi;

		pi.on("session_start", async (_e, ctx) => {
			preExistingDirtyPaths = dirtyPaths(process.cwd());
			closeoutAuthorized = false;
			centerPending = false; // fresh session: no centering turn, tool set comes from init
			centerActive = false;
			centerSavedTools = undefined;
			summaryAuthorized = false;
			summaryAttemptFinished = false;
			intakeActive = false;
			intakeSelected = false;
			planTarget = undefined;
			resetConfirmations(); // receipts + audit bridge never cross transcripts
			await loadCache();
			models = ctx.models;
			// session entry wins over cache for NOW restore (survives cache loss)
			try {
				for (const entry of ctx.sessionManager.getBranch()) {
					if (entry.type !== "custom" || !("customType" in entry) || entry.customType !== cfg.entryType) continue;
					if (!("data" in entry) || !entry.data || typeof entry.data !== "object") continue;
					// own persisted shape — written by persistSession above
					const data = entry.data as HostNowState;
					if (cfg.acceptEntry(data as Record<string, unknown>)) Object.assign(state, data);
				}
			} catch {
				/* fresh session */
			}
			// The backend focus is authoritative. A fresh process may have no local
			// cache (or a stale cache from another session), so reconcile before the
			// first tool read instead of reporting a false "NOW unset".
			try {
				const remoteNow = await backend.currentNow();
				if (state.issueId !== remoteNow?.id) {
					state.carrier = undefined;
					state.executingIssue = undefined;
					state.approvedPlan = undefined;
					state.obligationHandoff = undefined;
					state.obligationReview = undefined;
					state.treeCounts = undefined;
					state.setAt = remoteNow ? Date.now() : undefined;
				}
				state.issueId = remoteNow?.id;
				state.identifier = remoteNow?.key;
				state.title = remoteNow?.title;
				state.project = remoteNow?.project;
				await saveCache();
			} catch (error) {
				try {
					ctx.ui.notify(`Could not refresh ${backend.serviceLabel} NOW (${String(error)}) — using the last local pointer`, "warning");
				} catch {
					/* headless */
				}
			}
			// Disk-loaded branch only (the session manager was built from the
			// session file before this event): released claims are proven delivered,
			// and recovered outcomes re-enter the transcript as notices.
			try {
				for (const outcome of await ackPersistedOps(ctx)) {
					pendingNotices.push(`[${TOOL_NAME}] Recovered delivered outcome: ${outcome}`);
				}
			} catch {
				/* ack failure: claims linger, retry stays fail-closed */
			}
			digestPending = true;
			digestInjectedThisSession = false;
			if (gitRooted && !projectFilter) {
				try {
					ctx.ui.notify(`Unscoped git repo: no ${backend.markerFile} — /now unfiltered, unscoped writes refused. ${backend.scopeFix}`, "warning");
				} catch {
					/* headless */
				}
			} else if (projectFilter) {
				try {
					if (!(await backend.projectScopeExists(projectFilter))) {
						ctx.ui.notify(`Project "${projectFilter}" from ${backend.markerFile} does not exist in ${backend.serviceLabel} — create it or fix the marker`, "error");
					}
				} catch (error) {
					try {
						ctx.ui.notify(`Could not verify ${backend.markerFile} "${projectFilter}" (${String(error)}) — session unblocked`, "warning");
					} catch {
						/* headless */
					}
				}
			}
			footer(ctx);
		});

		pi.on("session_switch", async (event, ctx) => {
			preExistingDirtyPaths = dirtyPaths(process.cwd());
			closeoutAuthorized = false; // authorization never crosses transcripts
			summaryAuthorized = false;
			summaryAttemptFinished = false;
			intakeActive = false;
			intakeSelected = false;
			planTarget = undefined;
			resetConfirmations();
			await restoreCenterTools(); // a switch mid-center must not leak the empty tool set
			if (event.reason === "resume" || event.reason === "new") {
				digestPending = true;
				digestInjectedThisSession = false;
			}
			footer(ctx);
		});

		pi.on("input", async (event, ctx) => {
			if (!ownerSession(ctx) || event.source === "extension") return undefined;
			// OMP-25 audit LOW: a lost centering injection must not wedge /center.
			// Fresh owner input proves no centering turn ever started — drop the
			// stale pending flag so the next /center runs instead of refusing.
			if (centerPending && !centerActive) centerPending = false;
			if (/^\s*\/plan\b/.test(event.text)) {
				const now = currentNowRef();
				if (!now) {
					ctx.ui.notify("Run /intake first, or choose an issue with /now.", "warning");
					return { handled: true };
				}
				planTarget = now;
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
		pi.on("before_agent_start", async (event, ctx) => {
			// OMP-25: the centering turn starts here — only NOW do tools flip, so a
			// lost/failed injection never leaves the session tool-less. Any other
			// prompt racing ahead clears the pending flag and keeps its tools.
			if (centerPending || centerActive) {
				const isCenterTurn = !centerActive && event.prompt.startsWith(CENTER_PROMPT_HEADER);
				centerPending = false;
				if (isCenterTurn) {
					const saved = pi.getActiveTools();
					try {
						await pi.setActiveTools([]);
						centerSavedTools = saved;
						centerActive = true;
					} catch (error) {
						// FAIL CLOSED: mechanical read-only is the contract — a turn
						// that cannot drop its tools never runs. Abort bails the
						// pending prompt before it reaches the model.
						try {
							await pi.setActiveTools(saved);
						} catch {
							/* rollback already handled by the tool registry */
						}
						try {
							ctx.abort();
							ctx.ui.notify(`/center failed: tool isolation refused (${String(error)}) — turn aborted, nothing sent`, "error");
						} catch {
							/* headless */
						}
						return;
					}
				}
			}
			const notices = pendingNotices.splice(0).join("\n");
			if (!digestPending || digestInjectedThisSession) {
				return notices ? { message: { customType: `${TOOL_NAME}-notice`, content: notices } } : undefined;
			}
			digestPending = false;
			try {
				const digest = await buildDigest();
				digestInjectedThisSession = true;
				return { message: { customType: `${TOOL_NAME}-digest`, content: notices ? `${digest}\n${notices}` : digest } };
			} catch (e) {
				pi.logger.warn(`${TOOL_NAME}-now: digest failed`, { error: String(e) });
				return { message: { customType: `${TOOL_NAME}-digest`, content: `[${TOOL_NAME}] digest unavailable (${String(e)}) — session unblocked` } };
			}
		});

		pi.on("plan_approved", async (event, ctx) => {
			const target = planTarget ?? currentNowRef();
			if (!target) {
				return { cancel: true, reason: "Run /intake first, or choose an issue with /now." };
			}
			const stamp = preparePlanStamp(event);
			if ("reason" in stamp) return { cancel: true, reason: stamp.reason };
			try {
				const res = await backend.stampPlan(target, { ...stamp, title: target.title, planFilePath: event.planFilePath, approach: sectionItems(event.planContent, "Approach"), verification: sectionItems(event.planContent, "Verification") });
				mergeCarrier(res.plannedCandidateId ? { plannedCandidateId: res.plannedCandidateId } : undefined);
				armExecution(res.issue, stamp.hash);
				planTarget = undefined;
				summaryAuthorized = false;
				summaryAttemptFinished = false;
				closeoutAuthorized = false;
				await saveCache(); // armExecution's own save is fire-and-forget
				// Receipt LAST: startup may ack the stamp claims from it.
				recordDeliveredOutcome(`plan stamped on ${res.issue.key} (hash ${stamp.hash.slice(0, 12)})`);
				footer(ctx);
				return undefined;
			} catch (error) {
				return {
					cancel: true,
					reason: `Could not stamp the approved plan on ${target.key} (${String(error)}). Plan mode remains active.`,
				};
			}
		});

		pi.on("turn_start", async (_e, ctx) => footer(ctx));
		pi.on("turn_end", async (_e, ctx) => footer(ctx));

		// ---- HOME-122 hidden checkpoint continuation ----

		pi.on("session_stop", async event => {
			try {
				// OMP-25: the concise centering orientation is the final turn — no
				// hidden checkpoint continuation rides on it.
				if (centerActive) return;
				if (event.stop_hook_active || !state.executingIssue) return;
				const reviewOwed = state.obligationReview?.armed && !state.obligationReview.blockedOnce;
				const handoffOwed = state.obligationHandoff?.armed && !state.obligationHandoff.blockedOnce;
				const kind = reviewOwed ? backend.reviewKind : handoffOwed ? "handoff" : undefined;
				if (!kind) return;
				if (state.obligationHandoff?.armed) state.obligationHandoff.blockedOnce = true;
				if (state.obligationReview?.armed) state.obligationReview.blockedOnce = true;
				await saveCache();
				return {
					continue: true,
					additionalContext: [
						`Post one silent workflow checkpoint on ${state.executingIssue.key} with the ${TOOL_NAME} tool: action:"append_evidence", work:"${state.executingIssue.key}", kind:"${kind}".`,
						kind === backend.reviewKind ? cfg.reviewCheckpointHint : "The body carries the execution handoff: done, remaining, and exact resume steps.",
						STOP_REMINDER_BOUNDARY,
						"This continuation fires once; later turns show only ⚠ while the checkpoint remains owed.",
					].join(" "),
				};
			} catch (error) {
				pi.logger.warn(`${TOOL_NAME}-now: stop obligation check failed (fail-open)`, { error: String(error) });
				return;
			}
		});

		pi.on("agent_end", async () => {
			// OMP-25: the centering turn settled — restore the exact prior tool set.
			await restoreCenterTools();
		});

		pi.on("session_shutdown", async () => {
			await restoreCenterTools();
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
				return ids.filter(i => i.startsWith(p)).map(i => ({ value: i, label: i }));
			},
			handler: async (args, ctx) => {
				const arg = args.trim();
				try {
					if (arg === "clear") {
						await clearNow(ctx, false);
						await saveCache();
						recordDeliveredOutcome("NOW cleared via /now clear (no close proposed)");
						ctx.ui.notify("NOW cleared (no close proposed)", "info");
						pendingNotices.push(`[${TOOL_NAME}] Owner cleared NOW via /now clear — no close proposed.`);
						return;
					}
					if (arg) {
						const issue = await backend.findIssue(arg);
						await setNow(issue, ctx);
						recordDeliveredOutcome(`NOW → ${issue.key} via /now`);
						pendingNotices.push(`[${TOOL_NAME}] Owner set NOW to ${issue.key} via /now.`);
						return;
					}
					if (projectFilter && !(await backend.projectScopeExists(projectFilter))) {
						ctx.ui.notify(`Project "${projectFilter}" from ${backend.markerFile} does not exist in ${backend.serviceLabel} — create it or fix the marker`, "error");
						return;
					}
					const map = await backend.mapData(state.identifier, projectFilter ?? undefined);
					if (!map.surfaces.length || !map.surfaces.some(s => s.issues.length)) {
						ctx.ui.notify(projectFilter ? `No open issues in project "${projectFilter}" (${backend.markerFile} filter)` : "No open issues found", "warning");
						return;
					}
					// Prefer ctx.ui.custom (TUI overlay) when the host supports it; fall back
					// to a plain select() so non-TUI surfaces can still drive /now.
					let pick: BackendIssue | undefined;
					// ui.custom exists at runtime on TUI hosts but is absent from the published type
					const customUi = ctx.ui as ExtensionContext["ui"] & { custom?: unknown };
					const supportsCustom = typeof customUi.custom === "function";
					if (supportsCustom) {
						try {
							pick = await ctx.ui.custom<BackendIssue | undefined>(nowWindowFactory(map), { overlay: true });
						} catch {
							pick = undefined;
						}
					}
					if (!supportsCustom || pick === undefined) {
						const flat: { label: string; issue: BackendIssue }[] = [];
						for (const s of map.surfaces) {
							for (const i of s.issues) {
								const proj = i.project ? `${i.project} · ` : "";
								const age = fmtElapsed(Date.now() - Date.parse(i.updatedAt));
								const mark = i.isNow ? "● " : "  ";
								flat.push({ label: `${mark}[${s.name}] ${proj}${i.key} ${i.title} · ${age}`.slice(0, 200), issue: i });
							}
						}
						if (!flat.length) {
							ctx.ui.notify("No open issues found", "warning");
							return;
						}
						flat.sort((a, b) => Number(b.issue.isNow) - Number(a.issue.isNow));
						const choice = await ctx.ui.select(`Pick your NOW${map.capped ? " (list capped)" : ""}`, flat.map(f => f.label));
						if (typeof choice !== "string" || !choice) return;
						pick = flat.find(f => f.label === choice)?.issue;
					}
					if (!pick) return;
					const yes = await ctx.ui.confirm(`Make ${pick.key} your NOW?`, `"${pick.title}"\n${pick.project ?? ""}`);
					if (!yes) return;
					await setNow({ id: pick.id, key: pick.key, title: pick.title, ...(pick.project ? { project: pick.project } : {}) }, ctx);
					recordDeliveredOutcome(`NOW → ${pick.key} via /now`);
					pendingNotices.push(`[${TOOL_NAME}] Owner set NOW to ${pick.key} via /now.`);
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
				const now = currentNowRef();
				if (!now) {
					ctx.ui.notify("No NOW set", "warning");
					return;
				}
				try {
					const workflow = await backend.workflowState(now.key);
					if (!workflow.plan) {
						ctx.ui.notify("Run /plan first.", "warning");
						return;
					}
					if (!workflow.review) {
						ctx.ui.notify("Run /summary first.", "warning");
						return;
					}
					const blocker = await backend.closeBlocker(now, carrier());
					if (blocker) {
						ctx.ui.notify(blocker, "warning");
						return;
					}
					const confirmed = await ctx.ui.confirm(
						`This is your verdict — close ${now.key}?`,
						`"${now.title}"\n\nMoves to Done + records the verdict. Not reversible from here.`,
					);
					if (!confirmed) return;
					const line = await backend.closeWithVerdict(now, "done", undefined, carrier(), hooksFor(ctx));
					try {
						await backend.clearNowRemote(now.id);
					} catch (e) {
						ctx.ui.notify(`${backend.serviceLabel} NOW not cleared remotely (${String(e)}) — cleared locally`, "warning");
					}
					localClear(ctx, true);
					settleClosedIssue(now, ctx);
					await saveCache(); // settleClosedIssue's own save is fire-and-forget
					recordDeliveredOutcome(`${now.key} closed (done) via /done: ${line}`);
					ctx.ui.notify(line, "info");
					pendingNotices.push(`[${TOOL_NAME}] Owner verdict via /done: ${now.key} closed (Done); NOW cleared.`);
				} catch (error) {
					ctx.ui.notify(`/done failed: ${String(error)}`, "error");
				}
			},
		});

		pi.registerCommand("capture", {
			description: "Capture a stray thought without losing your thread",
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
					const target = captureProject ? `project "${captureProject}"` : `${cfg.teamNoun} (no project)`;
					const ok = await ctx.ui.confirm("File this capture?", `"${text}"\n→ ${target}`);
					if (!ok) return;
					const created = await backend.createIssue({ title: text, ...(captureProject ? { project: captureProject } : {}) });
					recordDeliveredOutcome(`captured "${text.slice(0, 80)}" as ${created.key}`);
					ctx.ui.notify(`Captured → ${created.key}`, "info");
				} catch (e) {
					ctx.ui.notify(`/capture failed: ${String(e)}`, "error");
				}
			},
		});

		// OMP-25 /center: one fresh, read-only, tool-less orientation turn on demand.
		// Never fires at launch, on a timer, or after another command.
		pi.registerCommand("center", {
			description: "Centering orientation: where you are, what's next, what's stuck, what just moved",
			handler: async (args, ctx) => {
				if (args.trim()) {
					ctx.ui.notify("/center takes no arguments", "warning");
					return;
				}
				if (centerPending || centerActive) {
					ctx.ui.notify("A centering turn is already running — wait for it to finish", "warning");
					return;
				}
				if (!ctx.isIdle()) {
					ctx.ui.notify("The agent is mid-turn — run /center once it finishes", "warning");
					return;
				}
				let snapshot: CenterSnapshot;
				try {
					snapshot = await backend.centerSnapshot(projectFilter ?? undefined);
				} catch (e) {
					// Tree/focus failure: one honest error beats a stale orientation.
					ctx.ui.notify(`/center failed: ${String(e)} — no orientation produced`, "error");
					return;
				}
				// Steer-race guard: the snapshot await spans network reads — a user
				// message submitted meanwhile starts a normal tooled turn, and the
				// centering prompt would steer into it un-isolated. Refuse instead.
				if (!ctx.isIdle()) {
					ctx.ui.notify("A turn started while /center was reading the ledger — run /center again once it finishes", "warning");
					return;
				}
				const scope = projectFilter
					? `project "${projectFilter}" (${backend.markerFile})`
					: `the whole workspace (no ${backend.markerFile} scope here)`;
				// Tools are NOT touched here: the centering turn's own
				// before_agent_start disables them, so a lost injection leaves the
				// session exactly as it was.
				centerPending = true;
				try {
					pi.sendUserMessage(renderCenterPrompt(snapshot, scope, `${new Date().toISOString().slice(0, 19)}Z`));
				} catch (e) {
					centerPending = false;
					ctx.ui.notify(`/center failed: ${String(e)} — no orientation produced`, "error");
				}
			},
		});

		pi.registerCommand(ADMIN_COMMAND, {
			description: `${TOOL_LABEL} weave admin: status | digest | help`,
			getArgumentCompletions: prefix => {
				const opts = ["status", "digest", "help"];
				return opts.filter(o => o.startsWith(prefix.trim())).map(o => ({ value: o, label: o }));
			},
			handler: async (args, ctx) => {
				const sub = args.trim() || "status";
				if (sub === "digest") {
					try {
						pi.sendMessage({ customType: `${TOOL_NAME}-digest`, content: await buildDigest() }, { deliverAs: "nextTurn" });
						ctx.ui.notify("Digest refreshed into context", "info");
					} catch (e) {
						ctx.ui.notify(`digest failed: ${String(e)}`, "error");
					}
					return;
				}
				if (sub === "help") {
					ctx.ui.notify(`/now (window) · /now <issue>|clear · /center (orientation) · /done (reviewed close) · /capture <text> · /${ADMIN_COMMAND} status|digest`, "info");
					return;
				}
				const lines = await backend.statusLines(currentNowRef() ?? null, { ...(projectFilter ? { projectFilter } : {}), digestInjected: digestInjectedThisSession });
				pi.sendMessage({ customType: `${TOOL_NAME}-status`, content: `── /${ADMIN_COMMAND} status @ ${new Date().toISOString().slice(11, 19)}Z ──\n${lines.join("\n")}` }, { deliverAs: "nextTurn" });
				ctx.ui.notify(lines.join(" · "), "info");
			},
		});

		// ---- the bounded tool ----

		const z = pi.zod;
		// z.enum wants a literal tuple; evidenceKinds is the validated readonly list the backend declared.
		const kindValues = backend.evidenceKinds as [EvidenceKind, ...EvidenceKind[]];
		const kindEnum = z.enum(kindValues);
		pi.registerTool({
			name: TOOL_NAME,
			label: TOOL_LABEL,
			description: prompt.render(toolDescriptionTemplate, {
				sequence: WORKFLOW_SEQUENCE,
				kinds: KIND_DESCRIPTION,
				queueNoun: backend.queueNoun,
				markerFile: backend.markerFile,
			}),
			parameters: z.object({
				action: z.enum(ACTION_ENUM),
				work: z.string().optional().describe("Work key (e.g. HOME-31) or ledger work id"),
				title: z.string().optional().describe("Work title (create_work, revise_work)"),
				description: z.string().optional().describe("Work description markdown (create_work, revise_work)"),
				project: z.string().optional().describe("Project name (create_work target, record_health)"),
				health: z.enum(["onTrack", "atRisk", "offTrack"]).optional().describe("Project health (record_health)"),
				body: z.string().optional().describe("Receipt body, close reason, or one-line update"),
				kind: kindEnum.optional().describe(KIND_DESCRIPTION),
				queue: z.boolean().optional().describe(`create_work: also mark ${backend.queueNoun} so the work lands in the owner decision queue`),
				batch: z
					.array(
						z.object({
							title: z.string(),
							description: z.string().optional(),
							blocks: z.array(z.number().int().nonnegative()).optional().describe("zero-based indexes of sibling batch entries THIS entry blocks"),
						}),
					)
					.optional()
					.describe("create_issue: publish these child issues under the parent (title/description) with native parent links and blocks relations, behind ONE preview — one confirm writes the whole set"),
				confirm: z.boolean().optional().describe("Two-phase write continuation: pass true only with the preview's confirmation_id, following the action policy in this tool description"),
				confirmation_id: z.string().optional().describe("The receipt id from this transcript's preview — binds the confirm to the exact shown payload"),
			}),
			// Signature contract is (toolCallId, params, signal, onUpdate, ctx) — the
			// pre-2026-08-10 4-param version received onUpdate as `ctx` (HOME-30).
			async execute(_id, params: WorkflowToolParams, _signal, _onUpdate, ctx) {
				const action = params.action as CanonicalAction;
				// Per-call op attribution: ONE finalizer for success AND failure —
				// any newly delivered op ids are bound to this toolResult's details
				// (atomic with the persisted result) and marked recorded so a later
				// slash-command receipt never re-claims them (HOME-147).
				let opsBaseline = backend.deliveredOps?.().length ?? 0;
				const finalize = (result: { content: { type: "text"; text: string }[]; details: Record<string, unknown> }) => {
					const delivered = backend.deliveredOps?.() ?? [];
					const newOps = delivered.slice(opsBaseline);
					opsBaseline = delivered.length;
					for (const id of newOps) recordedOps.add(id);
					return {
						content: result.content,
						details: { ...result.details, ...(newOps.length > 0 ? { opIds: newOps } : {}) },
					};
				};
				const deny = (msg: string) => finalize({ content: [{ type: "text" as const, text: msg }], details: { success: false } });
				const okText = (text: string, details: Record<string, unknown> = {}) =>
					finalize({ content: [{ type: "text" as const, text }], details: { success: true, ...details } });
				if (!(ACTIONS as readonly string[]).includes(params.action)) return deny(`unknown action "${params.action}"`);
				// OMP-25: a centering turn is mechanically read-only — belt and braces
				// beside the emptied tool set, in case a queued call slips through.
				if (centerActive && !READ_ACTIONS.has(action)) {
					return deny("REFUSED — /center is read-only: no Work Ledger writes during a centering turn.");
				}
				try {
					// HOME-114: wrap-up writes remain host-locked; only /done closes.
					// This refusal names the lock at any depth — checked first.
					if ((action === "record_health" || action === "request_closeout" || action === "cancel_work") && !closeoutAuthorized) {
						return deny(LOCK_REFUSAL);
					}
					// Review provenance refusal names /summary at any depth — checked
					// before the depth guard so a forged subagent summary gets the
					// informative refusal, not a generic one.
					if (action === "append_evidence" && params.kind === backend.reviewKind && !summaryAuthorized) {
						return deny("REFUSED — a closeout review receipt requires Chris to literally enter /summary in this owner session.");
					}
					// Plan §3: depth > 0 receives the candidate projection only — every
					// write is rejected before transport, centrally.
					if (!READ_ACTIONS.has(action) && !ownerSession(ctx)) {
						return deny(`REFUSED — ${TOOL_NAME} writes are owner-session only (task depth 0); a subagent holds no bearer and no confirmation receipt.`);
					}
					switch (action) {
						case "my_now": {
							const now = currentNowRef();
							if (now) {
								try {
									const t = await backend.goalTree(now);
									if (t) {
										state.treeCounts = { ...t.counts, at: Date.now() };
										void saveCache();
										return okText(renderGoalTree(t).join("\n"));
									}
								} catch (e) {
									return okText(`tree unavailable (${String(e)})`);
								}
								// Projectless NOW says it truthfully (by-seeing verdict 3, HOME-109)
								return okText(`NOW: ${now.key} ${now.title} — no goal attached, so no tree`.trim());
							}
							return okText("NOW unset");
						}
						case "waiting": {
							return okText((await backend.waitingLines()).join("\n"));
						}
						case "tree": {
							return okText((await backend.projectTreeLines()).join("\n"));
						}
						case "get_work": {
							if (!params.work) return deny("work key required");
							const i = await backend.issueDetail(params.work);
							return okText(
								[
									`${params.work} ${i.title}`,
									`state: ${i.state} · project: ${i.project ?? "none"} · labels: ${i.labels.join(",") || "none"}`,
									(i.description ?? "").slice(0, 1200),
								].join("\n"),
							);
						}
						case "append_evidence": {
							if (!params.work || !params.body) return deny("work key and body required");
							if (!params.kind) return deny(`kind required — every receipt is typed: ${KIND_DESCRIPTION}`);
							const kind: EvidenceKind = params.kind;
							const workflow = await backend.workflowState(params.work);
							if (!workflow?.plan) {
								return deny(`Run /plan first; ${params.work} has no current approved plan.`);
							}
							// Close-ritual kinds (everything but the execution handoff) require an
							// owner-entered /summary — including audit, whose bridge receipt only
							// ever exists inside a summary flow.
							if (kind !== "handoff" && !summaryAuthorized) {
								return deny(`REFUSED — ${kind} evidence is a close-ritual write; it requires Chris to literally enter /summary in this owner session.`);
							}
							const issue = workflow?.issue ?? (await backend.findIssue(params.work));
							const meta: EvidenceMeta = { ...(workflow?.plan ? { planHash: workflow.plan.hash } : {}) };
							if (kind === "audit") {
								// Bridge-authenticated forward: the persisted bytes are the receipt's
								// captured auditor report, never the model-supplied body.
								const sha = reportSha256(params.body);
								const receipt = claimAuditReceipt(sha);
								if (!receipt) {
									return deny(
										"REFUSED — no fresh auditor receipt matches those bytes. Run the fresh auditor and forward its report VERBATIM as the body; edited or fabricated reports are never recorded.",
									);
								}
								try {
									await backend.appendEvidence(issue, "audit", receipt.report, { ...meta, verdict: receipt.verdict, independent: true });
								} catch (error) {
									releaseAuditReceipt(sha);
									summaryAttemptFinished = true;
									return deny(`${String(error)} on ${issue.key} — audit receipt released, forward stays retryable`);
								}
								commitAuditReceipt(sha);
								summaryAttemptFinished = true;
								// The audit receipt settles NOTHING: only the closeout review
								// (reviewKind) settles the review obligation.
								return okText(`audit receipt recorded on ${issue.key} (verdict ${receipt.verdict})`);
							}
							try {
								await backend.appendEvidence(issue, kind, params.body, meta);
							} catch (error) {
								return deny(`${String(error)} on ${issue.key} — workflow state unchanged`);
							}
							if (kind === "handoff") settleCheckpoint(issue.id, "handoff", ctx);
							else if (kind === backend.reviewKind) settleCheckpoint(issue.id, "review", ctx);
							return okText(`${kind} receipt recorded on ${issue.key}`);
						}
						case "create_work": {
							if (!params.title) return deny("title required");
							const selectsNow = intakeActive && !intakeSelected;
							if (params.batch !== undefined && params.batch.length > 0) {
								const entries = params.batch;
								const n = entries.length;
								// validate BEFORE any gate call: reject at preview time
								for (let k = 0; k < n; k++) {
									for (const j of entries[k]!.blocks ?? []) {
										if (!Number.isInteger(j) || j < 0 || j >= n || j === k) {
											return deny(`batch entry [${k}] "${entries[k]!.title}" has invalid blocks index ${j} (valid: 0–${n - 1}, not itself)`);
										}
									}
								}
								// acyclicity — Kahn's algorithm over edges k→j
								const indeg = new Array<number>(n).fill(0);
								for (let k = 0; k < n; k++) for (const j of entries[k]!.blocks ?? []) indeg[j]!++;
								const ready: number[] = [];
								for (let k = 0; k < n; k++) if (indeg[k] === 0) ready.push(k);
								let seen = 0;
								while (ready.length > 0) {
									const k = ready.shift()!;
									seen++;
									for (const j of entries[k]!.blocks ?? []) if (--indeg[j]! === 0) ready.push(j);
								}
								if (seen < n) {
									const leftover: string[] = [];
									for (let k = 0; k < n; k++) if (indeg[k]! > 0) leftover.push(`[${k}] "${entries[k]!.title}"`);
									return deny(`blocks edges form a cycle: ${leftover.join(" ↔ ")} — publish refused`);
								}
								const target = fileTarget(params.project);
								const batchRefusal = unscopedRefusal(target);
								if (batchRefusal) return deny(batchRefusal);
								const edgeLines: string[] = [];
								for (let k = 0; k < n; k++) for (const j of entries[k]!.blocks ?? []) edgeLines.push(`[${k}] blocks [${j}]`);
								const detail = [
									`PARENT "${params.title}" → ${target ? `project ${target}` : cfg.teamNoun}${params.queue ? `\n→ + ${backend.queueNoun} on parent` : ""}${selectsNow ? "\n→ becomes NOW" : ""}${params.description ? `\n${params.description.slice(0, 400)}` : ""}`,
									...entries.map((e, k) => `[${k}] "${e.title}"${e.description ? `\n${e.description.slice(0, 200)}` : ""}`),
									edgeLines.length > 0 ? `edges:\n${edgeLines.join("\n")}` : "edges: none",
								].join("\n\n");
								const gate = confirmWrite("create_work", `Model wants to publish a BATCH — 1 parent + ${n} children`, detail, params);
								if (!gate.approved) return deny(gate.preview);
								try {
									const outcome = await backend.createBatch({
										parent: { title: params.title, ...(params.description ? { description: params.description } : {}), ...(target ? { project: target } : {}), ...(params.queue ? { queue: true } : {}) },
										entries,
									});
									if (selectsNow) {
										await setNow(outcome.parent, ctx);
										intakeSelected = true;
									}
									return okText(`${outcome.text}${selectsNow ? " + parent is NOW" : ""}`, {
										identifier: outcome.parent.key,
										children: outcome.children.map(c => c.key),
										now: selectsNow,
									});
								} catch (e) {
									if (e instanceof BatchPartialError) {
										// Partial land still delivered ops — finalize binds their ids to
										// THIS failure result so resume can ack exactly what landed.
										return finalize({
											content: [{
												type: "text" as const,
												text: [
													`BATCH PARTIAL FAILURE at ${e.message}`,
													`landed issues: ${e.landed.join(", ") || "none"}`,
													`landed edges: ${e.edgesLanded.join(", ") || "none"}`,
													`NOT created: ${e.notCreated.join(", ") || "none"}`,
													`No rollback exists — surviving issues need an owner verdict to remove.`,
												].join("\n"),
											}],
											details: { success: false, landed: e.landed, edgesLanded: e.edgesLanded },
										});
									}
									throw e;
								}
							}
							const target = fileTarget(params.project);
							const singleRefusal = unscopedRefusal(target);
							if (singleRefusal) return deny(singleRefusal);
							const gate = confirmWrite(
								"create_work",
								"Model wants to file a work item",
								`"${params.title}"\n→ ${target ? `project ${target}` : cfg.teamNoun}${params.queue ? `\n→ + ${backend.queueNoun} (lands in your decision queue)` : ""}${selectsNow ? "\n→ becomes NOW" : ""}\n\n${(params.description ?? "").slice(0, 400)}`,
								params,
							);
							if (!gate.approved) return deny(gate.preview);
							const created = await backend.createIssue({
								title: params.title,
								...(params.description ? { description: params.description } : {}),
								...(target ? { project: target } : {}),
								...(params.queue ? { queue: true } : {}),
							});
							if (selectsNow) {
								await setNow(created, ctx);
								intakeSelected = true;
							}
							return okText(`created ${created.key}${params.queue ? ` + ${backend.queueNoun}` : ""}${selectsNow ? " + NOW" : ""}`, { identifier: created.key, now: selectsNow });
						}
						case "queue_work": {
							if (!params.work) return deny("work key required");
							const issue = await backend.findIssue(params.work);
							const gate = confirmWrite(
								"queue_work",
								"Model wants to add an issue to your decision queue",
								`${issue.key} ${issue.title}\n\nAdds ${backend.queueNoun}. Nothing else changes.`,
								params,
							);
							if (!gate.approved) return deny(gate.preview);
							await backend.queueIssue(issue);
							return okText(`${issue.key} → ${backend.queueNoun}`);
						}
						case "request_closeout": {
							if (!params.work) return deny("work key required");
							const issue = await backend.findIssue(params.work);
							const gate = confirmWrite(
								"request_closeout",
								"Model wants to propose a close",
								`${issue.key} ${issue.title}\nReason: ${params.body ?? "(none given)"}\n\nRecords the proposal + adds ${backend.queueNoun}. Does NOT close.`,
								params,
							);
							if (!gate.approved) return deny(gate.preview);
							await backend.proposeClose(issue, params.body);
							return okText(`close proposed on ${issue.key} — owner verdict closes`);
						}
						case "cancel_work": {
							if (!params.work) return deny("work key required");
							const issue = await backend.findIssue(params.work);
							// Work backend: cancel = owner-verdict canceled close (state + record).
							const gate = confirmWrite(
								"cancel_work",
								"Model wants to CANCEL this work",
								`${issue.key} ${issue.title}\nReason: ${params.body ?? "(none given)"}\n\nRecords a canceled verdict — the work is closed as not-to-do.`,
								params,
							);
							if (!gate.approved) return deny(gate.preview);
							const line = await backend.closeWithVerdict(issue, "canceled", params.body, carrier(), hooksFor(ctx));
							if (issue.id === state.issueId) {
								try {
									await backend.clearNowRemote(issue.id);
								} catch {
									/* adapter already cleared or remote down — local clear proceeds */
								}
								localClear(ctx, false);
							}
							settleClosedIssue(issue, ctx);
							await saveCache();
							recordDeliveredOutcome(`${issue.key} canceled via cancel_work: ${line}`);
							return okText(line);
						}
						case "record_health": {
							if (!params.project || !params.health || !params.body) return deny("project, health, body all required");
							const gate = confirmWrite("record_health", "Model wants to post a project update", `${params.project} → ${params.health}\n"${params.body}"`, params);
							if (!gate.approved) return deny(gate.preview);
							await backend.recordHealth(params.project, params.health, params.body);
							return okText(`update posted: ${params.project} → ${params.health}`);
						}
						case "status": {
							const lines = await backend.statusLines(currentNowRef() ?? null, {
								...(projectFilter ? { projectFilter } : {}),
								digestInjected: digestInjectedThisSession,
							});
							return okText(lines.join("\n"));
						}
						case "revise_work": {
							if (!params.work) return deny("work key required");
							if (!params.title && !params.description) return deny("title and/or description required");
							const issue = await backend.findIssue(params.work);
							const gate = confirmWrite(
								"revise_work",
								"Model wants to revise this work in place",
								`${issue.key} ${issue.title}${params.title ? `\n→ new title: "${params.title}"` : ""}${params.description ? `\n→ new description:\n${params.description.slice(0, 400)}` : ""}`,
								params,
							);
							if (!gate.approved) return deny(gate.preview);
							await backend.reviseWork(issue, {
								...(params.title ? { title: params.title } : {}),
								...(params.description ? { description: params.description } : {}),
							});
							return okText(`${issue.key} revised`);
						}
						case "set_now": {
							if (!params.work) return deny("work key required");
							const issue = await backend.findIssue(params.work);
							const gate = confirmWrite("set_now", "Model wants to set NOW", `${issue.key} ${issue.title}`, params);
							if (!gate.approved) return deny(gate.preview);
							await setNow(issue, ctx);
							return okText(`NOW → ${issue.key}`);
						}
					}
				} catch (e) {
					return deny(`${TOOL_NAME} tool error: ${String(e)}`);
				}
			},
		});
	};
}

/** Repo-root marker lookup — one line, exact project name. */
function resolveProjectMarker(markerFile: string): string | null {
	try {
		const value = readFileSync(join(process.cwd(), markerFile), "utf8").split("\n")[0]?.trim();
		return value || null;
	} catch {
		return null;
	}
}
