/**
 * workflow/host.ts — the owner-workflow host (HOME-147, HOME-149).
 *
 * Session state, the NOW pointer + footer, the NOW window, the in-card
 * digest engine, the obligation/closeout locks, owner commands (/now /done
 * /capture /work), the bounded tool, transcript-bound confirmation receipts,
 * and the audit-receipt bridge consumer. A WorkflowBackend supplies storage.
 */
import { createHash, randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { type Dirent, readFileSync, realpathSync } from "node:fs";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { type AuthStorage, completeSimple } from "@oh-my-pi/pi-ai";
import {
	Container,
	discoverAuthStorage,
	getAgentDir,
	getMarkdownTheme,
	Markdown,
	Spacer,
	Text,
	type ExtensionAPI,
	type ExtensionContext,
	type ExtensionModelQuery,
	type Theme,
} from "@oh-my-pi/pi-coding-agent";
import { resolveLocalUrlToPath } from "@oh-my-pi/pi-coding-agent/internal-urls";
import { Ellipsis, matchesKey, truncateToWidth, type TUI, visibleWidth, wrapTextWithAnsi } from "@oh-my-pi/pi-tui";
import { prompt } from "@oh-my-pi/pi-utils";
import digestPromptTemplate from "./digest-prompt.md" with { type: "text" };
import kindDescriptionText from "./kind-description.md" with { type: "text" };
import lockRefusalText from "./lock-refusal.md" with { type: "text" };
import sequenceText from "./sequence.md" with { type: "text" };
import toolDescriptionTemplate from "./tool-description.md" with { type: "text" };
import {
	type BackendIssue,
	BatchPartialError,
	type CancellationProof,
	CENTER_READOUT_TYPE,
	type CenterSnapshot,
	CLOSEOUT_BOUNDARY,
	type CloseAttemptOutcome,
	type CloseAttemptSession,
	type CloseAttemptSnapshot,
	type EvidenceKind,
	type EvidenceMeta,
	type GoalTree,
	type IssueDetail,
	type MapSurface,
	type NowRef,
	nowRefusal,
	sameSessionSections,
	type PlanPacket,
	type RiderProof,
	type SealedAuditTask,
	STOP_REMINDER_BOUNDARY,
	type TreeItem,
	type WorkflowBackend,
	type WorkflowCheckpoint,
	type WorkStateCarrier,
	renderCenterReadout,
} from "./backend";
import { deliverCheckpoint, deliverPendingCheckpoints, queueCheckpointDelivery, queuePendingCheckpointDeliveries } from "./checkpoint-delivery";
import { confirmWrite, resetConfirmations } from "./confirm";
import { dirtyPaths, headCommit, parentCommit, rangeDiffSha256 } from "./git";
import {
	cancelBatchPath,
	consumeStagedCancelBatch,
	consumeStagedRiderBatch,
	readStagedCancelBatch,
	readStagedRiderBatch,
	riderBatchPath,
	type StagedCancelBatch,
	type StagedRiderBatch,
} from "./rider-batch";
import { registerSessionLedger } from "./session-ledger";
import { prepareNativeAuditRunner, type NativeAuditRunner, type NativeAuditRunResult } from "./auditor-runner";

/** Tool actions — the canonical action set for the `work` tool. */
export type CanonicalAction =
	| "get_work"
	| "tree"
	| "waiting"
	| "my_now"
	| "status"
	| "list_work"
	| "append_evidence"
	| "run_audit"
	| "create_work"
	| "queue_work"
	| "revise_work"
	| "set_now"
	| "record_health"
	| "waive_delivery"
	| "cancel_work";

const ACTIONS = [
	"get_work", "tree", "waiting", "my_now", "status", "list_work",
	"append_evidence", "run_audit", "create_work", "queue_work", "revise_work",
	"set_now", "record_health", "waive_delivery", "cancel_work",
] as const;
const ACTION_ENUM: [string, ...string[]] = [...ACTIONS];
const LIVE_ATTEMPT_STATES: Record<string, true> = {
	active: true,
	audit_ready: true,
	auditor_in_flight: true,
	audited: true,
	closeout_requested: true,
};
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
	/** OMP-137: the last /summary refusal, durable across sessions. Recorded on
	 *  every gate/warning/begin block with the complete typed reason; surfaced
	 *  as one pending notice at the next owner session_start while it still
	 *  targets the current NOW; cleared only by a successful begin/resume or a
	 *  NOW switch/clear. */
	summaryRefusal?: { issueId: string; key: string; reason: string; at: number };
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

/** OMP-38 get_work tail: the bounded audit-reconstruction packet. Deterministic
 *  render; capped packets say so and never leak partial bytes. */
export function renderPlanPacket(packet: PlanPacket | undefined): string[] {
	if (!packet) return ["PLAN PACKET: none — no plan receipt on a current candidate; run /plan (then /summary to finalize)"];
	const head = [
		"PLAN PACKET (build the auditor task from these exact ledger values — no transcript reads)",
		`candidate: ${packet.candidateId}`,
		`candidate sha256: ${packet.candidateSha256}`,
		`final commit: ${packet.commitSha ?? "unfinalized — run /summary"}`,
		`plan receipt sha256: ${packet.planReceiptSha256}`,
		`approved plan sha256: ${packet.planSha256}`,
	];
	if (packet.capped) {
		return [
			...head,
			`PLAN PACKET CAPPED: plan body + acceptance criteria total ${packet.capped.bytes} bytes, over the ${packet.capped.max}-byte ceiling — audit spawn is refused; restamp a smaller plan with /plan.`,
		];
	}
	return [
		...head,
		"acceptance criteria:",
		...(packet.acceptanceCriteria.length ? packet.acceptanceCriteria.map(criterion => `- ${criterion}`) : ["(none recorded)"]),
		"plan body (exact stored bytes):",
		packet.planBody ?? "(no stored plan body)",
	];
}

/** Render the deterministic three-line next-action banner for live close attempts (OMP-168). */
export function renderNextActionBanner(
	workKey: string,
	snapshot: CloseAttemptSnapshot | undefined,
	summaryAuthorized: boolean,
): string[] {
	if (!snapshot) return [];
	switch (snapshot.state) {
		case "active":
			return [
				"STATUS: CLOSE ATTEMPT active",
				`NEXT REQUIRED ACTION: work action:"append_evidence", work:"${workKey}", kind:"verification"`,
				`BLOCKED ACTIONS: run_audit, append_evidence kind:"closeout", /done`,
			];
		case "audit_ready":
			return [
				"STATUS: CLOSE ATTEMPT audit_ready",
				`NEXT REQUIRED ACTION: work action:"run_audit", work:"${workKey}"`,
				`BLOCKED ACTIONS: append_evidence kind:"closeout", /done`,
			];
		case "auditor_in_flight":
			return [
				"STATUS: CLOSE ATTEMPT auditor_in_flight",
				"NEXT REQUIRED ACTION: wait for the current native run to settle and use get_work only for recovery",
				"BLOCKED ACTIONS: run_audit, append_evidence, /done",
			];
		case "audited":
			if (!summaryAuthorized) {
				return [
					"STATUS: CLOSE ATTEMPT audited",
					"NEXT REQUIRED ACTION: owner /summary must be entered in this session to authorize closeout review",
					`BLOCKED ACTIONS: append_evidence kind:"closeout", /done`,
				];
			}
			return [
				"STATUS: CLOSE ATTEMPT audited",
				`NEXT REQUIRED ACTION: work action:"append_evidence", work:"${workKey}", kind:"closeout"`,
				"BLOCKED ACTIONS: run_audit, /done",
			];
		case "closeout_requested":
			return [
				"STATUS: CLOSE ATTEMPT closeout_requested",
				"NEXT REQUIRED ACTION: owner /done closes this work",
				"BLOCKED ACTIONS: run_audit, append_evidence",
			];
		default:
			return [];
	}
}

/** Compact re-entry digest when a live close attempt already existed before /summary (OMP-168). */
export function renderSummaryResumeDigest(
	workKey: string,
	snapshot: CloseAttemptSnapshot | undefined,
): string {
	const banner = renderNextActionBanner(workKey, snapshot, true);
	const bannerText = banner.length > 0 ? `${banner.join("\n")}\n\n` : "";
	return [
		`${bannerText}# SUMMARY RESUME (${workKey})`,
		"",
		"A live close attempt is already in progress. Satisfied steps must NOT be repeated.",
		`Call \`work action:"get_work", work:"${workKey}"\` and execute the single NEXT REQUIRED ACTION.`,
		"",
		'When appending closeout evidence (kind: "closeout"), format the review body with these sections:',
		'1. Verbatim `work action:"my_now"` completion tree',
		"2. MOVED — plain-language decisions and household-visible results",
		"3. PROOF — durable issue evidence and exact verification commands/counts",
		'4. UNVERIFIED / BLOCKED — real gaps or explicit "none"',
		"5. NEXT SESSION — standing goal, starting state, queue sources in priority order, first action, gates, and stop conditions (loop charter)",
		"",
		"Do not post second handoff files or separate prompt files; the loop charter lives in the closeout receipt.",
	].join("\n");
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

const DIGEST_LABELS = new Set(["PROBLEM", "MAKEUP", "DECISIONS", "BLOCKERS", "ON-YOU", "EVIDENCE", "STATE", "NEXT", "SUGGEST"]);

/** Reads are free at any depth; every other canonical action is a write. */
const READ_ACTIONS: ReadonlySet<CanonicalAction> = new Set(["get_work", "tree", "waiting", "my_now", "status", "list_work"]);

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
	question?: string;
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
	let intakeScanRequired = false;
	let intakeScanDelivered = false;
	let intakeSelected = false;
	let planTarget: NowRef | undefined;
	let summaryAuthorized = false;
	let summaryBlockReason: string | undefined;
	let preExistingDirtyPaths: string[] = [];
	// OMP-47: owner-session identity for close attempts. The start commit is
	// captured at session start; the authorization references are host-minted at
	// the LITERAL owner commands — models can never fabricate them.
	let sessionStartCommit: string | null = null;
	let sessionStartedAt: string = new Date().toISOString();
	let summaryAuthorizationRef: string | undefined;
	let models: ExtensionModelQuery | undefined;

	// HOME-114 mechanical lock: flips ONLY on host-observed owner entry of /summary
	// or /done. FAIL CLOSED on unknown depth: subagent sessions never unlock.
	let closeoutAuthorized = false;
	const ownerSession = (ctx: { taskDepth?: number } | undefined): boolean => ctx?.taskDepth === 0;

	function hasIntakeScanHeadings(text: string): boolean {
		const headingRegex = (title: string) =>
			new RegExp(`(?:^|\\n)\\s*(?:#{1,6}\\s+|\\*\\*|\\*|[-*]\\s+\\*\\*)?\\s*(?:\\d+\\.\\s+)?${title}\\s*(?:\\*\\*|\\*|:)?\\s*(?:\\n|$)`, "i");
		const m1 = headingRegex("Figured out myself").exec(text);
		if (!m1) return false;
		const rest1 = text.slice(m1.index + m1[0].length);
		const m2 = headingRegex("Asking you").exec(rest1);
		if (!m2) return false;
		const rest2 = rest1.slice(m2.index + m2[0].length);
		const m3 = headingRegex("Leaving for later").exec(rest2);
		return m3 !== null;
	}

	function resetIntakeState(): void {
		intakeActive = false;
		intakeScanRequired = false;
		intakeScanDelivered = false;
		intakeSelected = false;
	}

	async function readLatestIntakeBlueprint(ctx: ExtensionContext): Promise<{ url: string; content: string } | null> {
		if (!ctx.localProtocolOptions) return null;
		let localRoot: string;
		try {
			localRoot = resolveLocalUrlToPath("local://", ctx.localProtocolOptions);
		} catch {
			return null;
		}
		let dirents: Dirent[];
		try {
			dirents = await readdir(localRoot, { withFileTypes: true });
		} catch {
			return null;
		}
		const pattern = /^intake-[a-z0-9][a-z0-9_-]*\.md$/i;
		const candidates: Array<{ name: string; fullPath: string; mtimeMs: number }> = [];
		for (const dirent of dirents) {
			if (!dirent.isFile() && !dirent.isSymbolicLink()) continue;
			if (!pattern.test(dirent.name)) continue;
			const fullPath = join(localRoot, dirent.name);
			try {
				const st = await stat(fullPath);
				if (st.isFile()) {
					candidates.push({ name: dirent.name, fullPath, mtimeMs: st.mtimeMs });
				}
			} catch {
				// ignore unreadable file
			}
		}
		if (candidates.length === 0) return null;
		candidates.sort((a, b) => {
			if (b.mtimeMs !== a.mtimeMs) {
				return b.mtimeMs - a.mtimeMs;
			}
			return a.name.localeCompare(b.name);
		});
		const top = candidates[0]!;
		try {
			const content = await readFile(top.fullPath, "utf-8");
			if (content.length === 0) return null;
			return { url: `local://${top.name}`, content };
		} catch {
			return null;
		}
	}

	function hasExplicitMultiDeliverableDeclaration(text: string): boolean {
		const countPattern =
			/\b(?:[2-9]|two|three|four|five|six|seven|eight|nine)\s+(?:(?:independent|independently\s+verifiable)\s+)?(?:slices?|deliverables?|workstreams?)\b/i;
		const qualifiedPluralPattern =
			/\b(?:independent|independently\s+verifiable)\s+(?:changes|slices|deliverables|workstreams)\b/i;
		return countPattern.test(text) || qualifiedPluralPattern.test(text);
	}
	let activeCenterRunId: number | null = null;
	let nextCenterRunId = 1;
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
		await backend.ackOps(ids);
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
			summaryRefusal: state.summaryRefusal,
			carrier: state.carrier,
		});
	}

	/** OMP-137: durably record a /summary refusal against the NOW it targeted.
	 *  Persistence is awaited so the reason survives an immediate process exit. */
	async function recordSummaryRefusal(now: NowRef, reason: string): Promise<void> {
		state.summaryRefusal = { issueId: now.id, key: now.key, reason, at: Date.now() };
		persistSession();
		await saveCache();
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
		// Closed work never becomes NOW (owner ruling 2026-08-25). Backend-looked-up
		// refs carry state; the /now picker and fresh creates are open by construction.
		const refusal = nowRefusal(issue);
		if (refusal) throw new Error(refusal);
		await backend.setNowRemote(issue);
		// the opaque carrier belongs to the previous NOW (Work binds it to a
		// candidate commit) — never let it leak across a switch. The captured
		// plan approval target is likewise bound to the NOW at plan entry
		// (OMP-124): a switch discards it, and approval re-resolves the new NOW.
		if (state.issueId !== issue.id) {
			state.carrier = undefined;
			planTarget = undefined;
			state.summaryRefusal = undefined; // OMP-137: refusal is bound to the previous NOW
		}
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
		planTarget = undefined; // no latent approval target survives a NOW clear (OMP-124)
		state.summaryRefusal = undefined; // OMP-137: a NOW clear resolves the refusal notice
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

	/** OMP-93 rider staging: host-owned path (never repo-controlled), one file
	 *  per canonical working directory. Consumption requires an owner confirm
	 *  bound to the exact keys and file digest, then a one-shot rename. */
	async function stageRiders(ctx: ExtensionContext): Promise<{ riders: RiderProof[]; batch: StagedRiderBatch } | { refusal: string } | undefined> {
		const path = riderBatchPath(getAgentDir(), process.cwd());
		let batch: StagedRiderBatch | null;
		try {
			batch = readStagedRiderBatch(path);
		} catch (error) {
			ctx.ui.notify(`close attempt not begun — staged rider batch rejected (${String(error)}); fix or remove ${path}`, "warning");
			return { refusal: `staged rider batch rejected (${String(error)}) — fix or remove ${path}` }; // fail closed: only a provably absent batch is skippable
		}
		if (batch === null) return undefined; // no staged batch — the normal case
		try {
			const riders = await backend.resolveRiders(batch.entries);
			const keys = batch.entries.map(entry => entry.key).join(", ");
			const yes = await ctx.ui.confirm(
				`Seal ${riders.length} rider(s) into this close attempt?`,
				`${keys}\n\nbatch sha256 ${batch.digest} — each rider closes with this item's /done, exactly as staged. Declining seals none.`,
			);
			if (!yes) {
				consumeStagedRiderBatch(batch, "declined");
				ctx.ui.notify("rider batch declined — archived, no riders sealed", "info");
				// Fail closed: a canceled batch aborts the begin so summary writes
				// are not armed on a riderless attempt the owner did not approve.
				return { refusal: "the owner declined the staged rider batch — archived, no riders sealed" };
			}
			// NOT consumed yet: the file is archived only after the service applies
			// the begin — a refusal must leave the approved batch staged.
			return { riders, batch };
		} catch (error) {
			ctx.ui.notify(`close attempt not begun — staged rider batch is invalid (${String(error)}); fix or remove ${path}`, "warning");
			return { refusal: `staged rider batch is invalid (${String(error)}) — fix or remove ${path}` }; // fail closed: never begin an attempt that silently drops a staged batch
		}
	}

	async function stageCancelBatch(nowKey: string, ctx: ExtensionContext): Promise<{ cancellations: CancellationProof[]; batch: StagedCancelBatch } | undefined | null> {
		const path = cancelBatchPath(getAgentDir(), process.cwd());
		let batch: StagedCancelBatch | null;
		try {
			batch = readStagedCancelBatch(path);
		} catch (error) {
			ctx.ui.notify(`/done refused — staged cancel batch rejected (${String(error)}); fix or remove ${path}`, "warning");
			return null;
		}
		if (batch === null) return undefined;
		try {
			const cancellations = await backend.resolveCancellations(batch.entries, nowKey);
			return { cancellations, batch };
		} catch (error) {
			ctx.ui.notify(`/done refused — staged cancel batch is invalid (${String(error)}); fix or remove ${path}`, "warning");
			return null;
		}
	}


	/** OMP-47: bind this literal /summary to a ledger-owned close attempt. Every
	 *  identity field is host-computed; the service refuses with a typed event. */
	async function beginAttempt(now: NowRef, ctx: ExtensionContext, auditBaseCommit?: string, auditBaseDirtyPaths?: readonly string[]): Promise<boolean> {
		const commitSha = carrier().commitSha;
		if (!commitSha) {
			summaryBlockReason = "the candidate was not finalized";
			await recordSummaryRefusal(now, "the candidate was not finalized");
			return false;
		}
		const legacyPlan = auditBaseCommit === undefined;
		const startCommit = auditBaseCommit ?? parentCommit(process.cwd(), commitSha);
		if (!startCommit) {
			summaryBlockReason = "no audit base commit was available";
			await recordSummaryRefusal(now, "no audit base commit was available (outside a git repo?)");
			ctx.ui.notify("close attempt not begun — no audit base commit (outside a git repo?); /done will refuse", "warning");
			return false;
		}
		if (auditBaseDirtyPaths === undefined && !legacyPlan) {
			summaryBlockReason = "the approved plan has no audit-base dirty-path snapshot";
			await recordSummaryRefusal(now, "the approved plan has no audit-base dirty-path snapshot; restamp with /plan");
			ctx.ui.notify("close attempt not begun — no audit-base dirty-path snapshot; restamp with /plan", "warning");
			return false;
		}
		const diffSha256 = rangeDiffSha256(process.cwd(), startCommit, commitSha);
		if (!diffSha256) {
			summaryBlockReason = `the ${startCommit.slice(0, 12)}..${commitSha.slice(0, 12)} audit diff could not be hashed`;
			await recordSummaryRefusal(now, `the ${startCommit.slice(0, 12)}..${commitSha.slice(0, 12)} audit diff could not be hashed`);
			ctx.ui.notify(`close attempt not begun — could not hash the ${startCommit.slice(0, 12)}..${commitSha.slice(0, 12)} diff; /done will refuse`, "warning");
			return false;
		}
		summaryAuthorizationRef ??= `summary:${randomUUID()}`;
		const staged = await stageRiders(ctx);
		if (staged && "refusal" in staged) {
			summaryBlockReason = staged.refusal;
			await recordSummaryRefusal(now, staged.refusal);
			return false;
		}
		const session: CloseAttemptSession = {
			authorizationRef: summaryAuthorizationRef,
			sessionId: piRef.getSessionId(),
			startedAt: sessionStartedAt,
			startCommit,
			repository: process.cwd(),
			diffSha256,
			dirtyPaths: [...(auditBaseDirtyPaths ?? [])],
			...(staged?.riders.length ? { riders: staged.riders } : {}),
		};
		const outcome = await backend.beginCloseAttempt(now, session);
		if (staged) {
			if (outcome.status === "applied") {
				try {
					consumeStagedRiderBatch(staged.batch, "consumed");
				} catch (error) {
					// Non-rollback: the service already sealed the riders. The file is
					// now stale authority residue — the owner must remove it by hand.
					pendingNotices.push(`[${TOOL_NAME}] riders ARE sealed, but archiving the batch file failed (${String(error)}) — remove ${staged.batch.path} manually before the next /summary`);
				}
			} else {
				ctx.ui.notify("rider batch NOT consumed — the attempt was refused; it stays staged for the next /summary", "warning");
			}
		}
		if (outcome.status === "refused") {
			summaryBlockReason = `the ledger refused the close attempt (${outcome.event.reasonCode})`;
			// OMP-137: persist the COMPLETE service-rendered event text, not a summary.
			await recordSummaryRefusal(now, outcome.event.renderedText);
			ctx.ui.notify(`close attempt refused: ${outcome.event.reasonCode}`, "warning");
		}
		if (outcome.event.requiresDelivery) {
			try {
				await deliverCheckpoint(piRef, backend, outcome.event);
			} catch (error) {
				pendingNotices.push(`[${TOOL_NAME}] close-attempt checkpoint delivery failed (${String(error)}) — it retries at the next owner session start`);
			}
		}
		return outcome.status === "applied";
	}

	async function authorizeSummary(ctx: ExtensionContext): Promise<{ authorized: boolean; reason?: string }> {
		// Review writes stay locked until a close attempt is actually live: a
		// refused begin must not arm verification evidence (OMP-127).
		summaryBlockReason = undefined;
		summaryAuthorized = false;
		closeoutAuthorized = true;
		// A literal /summary mints a fresh authorization: the service
		// atomically begins, resumes, or refuses under it (OMP-140).
		summaryAuthorizationRef = `summary:${randomUUID()}`;
		const now = currentNowRef();
		if (!now) {
			summaryBlockReason = "no NOW item is selected";
			ctx.ui.notify("No NOW is selected. Review can run, but /done stays blocked; run /intake first.", "warning");
			return { authorized: false, reason: summaryBlockReason };
		}
		try {
			const gate = await backend.summaryGate(now, carrier(), hooksFor(ctx));
			if (!gate.ok) {
				summaryBlockReason = gate.reason;
				await recordSummaryRefusal(now, gate.reason);
				ctx.ui.notify(gate.reason, "warning");
				return { authorized: false, reason: summaryBlockReason };
			}
			mergeCarrier(gate.carrier);
			if (gate.warning) {
				summaryBlockReason = gate.warning;
				await recordSummaryRefusal(now, gate.warning);
				ctx.ui.notify(gate.warning, "warning");
				persistSession();
				await saveCache();
				// Receipt LAST (HOME-147): claims may be acked from it at startup,
				// so every state mutation + awaited persistence precedes it.
				recordDeliveredOutcome(`summary gate passed for ${now.key} — candidate finalized with warnings, awaiting verdict`);
				footer(ctx);
				return { authorized: false, reason: summaryBlockReason };
			}
			if (!gate.planHash) {
				summaryBlockReason = "the approved plan hash is missing";
				await recordSummaryRefusal(now, "the approved plan hash is missing");
				return { authorized: false, reason: summaryBlockReason };
			}
			if (!(await beginAttempt(now, ctx, gate.auditBaseCommit, gate.auditBaseDirtyPaths))) {
				return { authorized: false, reason: summaryBlockReason ?? "close attempt could not be begun" };
			}
			summaryAuthorized = true;
			state.summaryRefusal = undefined; // OMP-137: a successful begin/resume resolves the refusal
			state.executingIssue = gate.issue;
			state.approvedPlan = { hash: gate.planHash, at: Date.now() };
			state.obligationReview = { armed: true, blockedOnce: false };
			persistSession();
			await saveCache();
			recordDeliveredOutcome(`summary gate passed for ${now.key} — candidate finalized, awaiting verdict`);
			footer(ctx);
			return { authorized: true };
		} catch (error) {
			summaryBlockReason = `workflow state could not be loaded (${String(error)})`;
			await recordSummaryRefusal(now, `workflow state could not be loaded (${String(error)})`);
			ctx.ui.notify(`Could not load ${now.key} workflow state (${String(error)}). Review can run, but /done stays blocked.`, "warning");
			return { authorized: false, reason: summaryBlockReason };
		}
	}

	// ---- plan stamping ----

	function preparePlanStamp(plan: { planFilePath: string; planContent: string; title: string }): { hash: string; body: string } | { reason: string } {
		const approach = sectionItems(plan.planContent, "Approach");
		const verification = sectionItems(plan.planContent, "Verification");
		if (approach.length === 0 || verification.length === 0) {
			return { reason: "Plan approval requires non-empty ## Approach and ## Verification lists." };
		}
		// OMP-155: the receipt body IS the exact approved plan — the sha-sealed
		// local:// file dies with its authoring session; the ledger copy is the
		// durable source the stored SHA-256 recovers against.
		return { hash: Bun.SHA256.hash(plan.planContent, "hex"), body: plan.planContent };
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
		// OMP-69: the Session Ledger note — owner-turn reconstruction over the
		// shared backend and the same credential path the digest engine uses.
		registerSessionLedger(pi, { backend, getApiKey: digestApiKey });

		pi.registerMessageRenderer(CENTER_READOUT_TYPE, (message, _options, theme) => {
			const container = new Container();
			container.addChild(new Text(theme.fg("accent", "Centering Orientation"), 1, 0));
			const details = message.details;
			const displayReadout =
				details && typeof details === "object" && "readout" in details && typeof details.readout === "string"
					? details.readout
					: typeof message.content === "string"
						? message.content
						: "";
			container.addChild(
				new Markdown(displayReadout, 1, 0, getMarkdownTheme(), {
					color: (text: string) => theme.fg("customMessageText", text),
				}),
			);
			container.addChild(new Spacer(1));
			return container;
		});

		pi.on("session_start", async (_e, ctx) => {
			preExistingDirtyPaths = dirtyPaths(process.cwd());
			closeoutAuthorized = false;
			sessionStartCommit = headCommit(process.cwd());
			sessionStartedAt = new Date().toISOString();
			summaryAuthorizationRef = undefined;
			summaryAuthorized = false;
			summaryBlockReason = undefined;
			resetIntakeState();
			planTarget = undefined;
			// OMP-43: only an OWNER lifecycle clears the process-global audit bridge
			// (binding, receipts, transcript ref) — a subagent's session_start (the
			// auditor itself) runs its own module copy and must leave the owner's
			// in-flight binding intact. Local flags above are per-copy and stay reset.
			resetConfirmations({ resetShared: ownerSession(ctx) });
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
					state.summaryRefusal = undefined; // OMP-137: refusal was bound to the previous NOW
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
			// OMP-51: retry pending close-attempt checkpoint deliveries for the
			// current NOW BEFORE any close action can run this session.
			if (ownerSession(ctx) && state.identifier) {
				try {
					const pass = await deliverPendingCheckpoints(pi, backend, state.identifier);
					pendingNotices.push(...pass.notices.map(notice => `[${TOOL_NAME}] ${notice}`));
					if (pass.delivered > 0) pendingNotices.push(`[${TOOL_NAME}] Recovered ${pass.delivered} pending close-attempt checkpoint(s).`);
				} catch (error) {
					pendingNotices.push(`[${TOOL_NAME}] checkpoint recovery failed (${String(error)}) — deliveries stay pending and closeout stays blocked`);
				}
			}
			// OMP-137: a durable /summary refusal that still targets the current NOW
			// re-enters model context as ONE pending notice with its exact reason.
			if (ownerSession(ctx) && state.summaryRefusal && state.issueId && state.summaryRefusal.issueId === state.issueId) {
				pendingNotices.push(
					`[${TOOL_NAME}] The last /summary on ${state.summaryRefusal.key} was refused and is still unresolved: ${state.summaryRefusal.reason}`,
				);
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
			summaryBlockReason = undefined;
			sessionStartCommit = headCommit(process.cwd());
			sessionStartedAt = new Date().toISOString();
			summaryAuthorizationRef = undefined;
			resetIntakeState();
			planTarget = undefined;
			resetConfirmations({ resetShared: ownerSession(ctx) }); // OMP-43: owner transcripts only — see session_start
			if (event.reason === "resume" || event.reason === "new") {
				digestPending = true;
				digestInjectedThisSession = false;
			}
			footer(ctx);
		});

		pi.on("input", async (event, ctx) => {
			if (!ownerSession(ctx) || event.source === "extension") return undefined;
			if (/^\s*\/plan\b/.test(event.originalText)) {
				const now = currentNowRef();
				if (!now) {
					ctx.ui.notify("Run /intake first, or choose an issue with /now.", "warning");
					return { handled: true };
				}
				planTarget = now;
			}
			// Exact normalized owner /summary and /skill:summary commands re-authorize
			// before prompt expansion or streaming (OMP-47, OMP-168).
			if (event.originalText === "/summary" || event.originalText === "/skill:summary") {
				const now = currentNowRef();
				let preSnapshot: CloseAttemptSnapshot | undefined;
				if (now) {
					try {
						const preDetail = await backend.issueDetail(now.key);
						preSnapshot = preDetail.attemptSnapshot;
					} catch (error) {
						return {
							text: `SUMMARY REFUSED: workflow state could not be read (${String(error)}). Call get_work and resolve that blocker; do not run review, health, triage, verification, or closeout writes.`,
						};
					}
				}
				const hadLiveAttempt = Boolean(preSnapshot && LIVE_ATTEMPT_STATES[preSnapshot.state] === true);
				const auth = await authorizeSummary(ctx);
				if (!auth.authorized) {
					return {
						text: `SUMMARY REFUSED: ${auth.reason ?? "authorization failed"}. Call get_work and resolve that blocker; do not run review, health, triage, verification, or closeout writes.`,
					};
				}
				if (hadLiveAttempt && now) {
					try {
						const postDetail = await backend.issueDetail(now.key);
						return {
							text: renderSummaryResumeDigest(now.key, postDetail.attemptSnapshot),
						};
					} catch (error) {
						return {
							text: `SUMMARY RESUME (${now.key}): live attempt state could not be refreshed (${String(error)}). Call get_work and resolve that blocker; do not repeat satisfied steps.`,
						};
					}
				}
			} else if (event.originalText === "/done") {
				closeoutAuthorized = true;
			}
			return undefined;
		});
		pi.on("message_start", async (event, ctx) => {
			if (!ownerSession(ctx)) return;
			const m = event.message as { role?: string; customType?: string; attribution?: string; details?: { name?: string; args?: string; path?: string } };
			if (m.role !== "custom" || m.customType !== "skill-prompt" || m.attribution !== "user") return;
			if (m.details?.name === "intake") {
				intakeActive = true;
				intakeSelected = false;
				const isPublish =
					typeof m.details?.args === "string" && /(?:^|\s)--publish\b/.test(m.details.args);
				intakeScanRequired = !isPublish;
				intakeScanDelivered = false;
				return;
			}
			resetIntakeState();
		});
		pi.on("message_end", async (event, ctx) => {
			if (!ownerSession(ctx)) return;
			if (event.message?.role !== "assistant") return;
			if (!intakeActive || !intakeScanRequired || intakeScanDelivered) return;
			const msg = event.message;
			if (!msg || typeof msg !== "object" || !("content" in msg) || !Array.isArray(msg.content)) return;
			const hasToolCalls = msg.content.some((b: unknown) => b && typeof b === "object" && "type" in b && b.type === "toolCall");
			if (hasToolCalls) return;
			const textBlocks: string[] = [];
			for (const b of msg.content) {
				if (b && typeof b === "object" && "type" in b && b.type === "text" && "text" in b && typeof b.text === "string") {
					textBlocks.push(b.text);
				}
			}
			const fullText = textBlocks.join("\n");
			if (hasIntakeScanHeadings(fullText)) {
				intakeScanDelivered = true;
			}
		});

		pi.on("tool_call", async (event, ctx) => {
			if (!ownerSession(ctx)) return undefined;
			if (event.toolName === "ask" && intakeActive && intakeScanRequired) {
				if (!intakeScanDelivered) {
					return {
						block: true,
						reason: "Intake visible scan required before questions: deliver a standalone tool-free assistant message containing 'Figured out myself', 'Asking you', and 'Leaving for later' headings first.",
					};
				}
				const input = event.input;
				const questions = input && typeof input === "object" && "questions" in input && Array.isArray(input.questions) ? input.questions : undefined;
				if (!questions || questions.length !== 1) {
					return {
						block: true,
						reason: "Intake questions must contain exactly one decision per dialog.",
					};
				}
			}
			return undefined;
		});
		pi.on("before_agent_start", async (event, ctx) => {
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
				const res = await backend.stampPlan(target, {
					...stamp,
					title: target.title,
					planFilePath: event.planFilePath,
					approach: sectionItems(event.planContent, "Approach"),
					verification: sectionItems(event.planContent, "Verification"),
					baseCommit: headCommit(process.cwd()) ?? undefined,
					baseDirtyPaths: dirtyPaths(process.cwd()),
				});
				mergeCarrier(res.plannedCandidateId ? { plannedCandidateId: res.plannedCandidateId } : undefined);
				armExecution(res.issue, stamp.hash);
				planTarget = undefined;
				summaryAuthorized = false;
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
				if (event.stop_hook_active || !state.executingIssue) return;
				const reviewOwed = state.obligationReview?.armed && !state.obligationReview.blockedOnce;
				const handoffOwed = state.obligationHandoff?.armed && !state.obligationHandoff.blockedOnce;
				if (!reviewOwed && !handoffOwed) return;
				let orientation = "The body carries the execution handoff: done, remaining, and exact resume steps.";
				if (reviewOwed) {
					// OMP-134/OMP-137: the closeout checkpoint is emitted only from LIVE
					// ledger state. Anything but an audited attempt (active, audit_ready,
					// auditor_in_flight, closeout_requested, terminal, missing) or an
					// unreadable workflow emits nothing and leaves blockedOnce false, so a
					// later valid audited state can still trigger exactly once.
					let workflow: WorkflowCheckpoint;
					try {
						workflow = await backend.workflowState(state.executingIssue.key);
					} catch {
						return;
					}
					if (workflow.closeAttempt?.state !== "audited") return;
					orientation = workflow.closeAttempt.latestEventText
						? `The body carries the closeout review. Ledger state: ${workflow.closeAttempt.latestEventText}`
						: "The body carries the closeout review.";
				}
				const kind = reviewOwed ? backend.reviewKind : "handoff";
				if (state.obligationHandoff?.armed) state.obligationHandoff.blockedOnce = true;
				if (state.obligationReview?.armed) state.obligationReview.blockedOnce = true;
				await saveCache();
				return {
					continue: true,
					additionalContext: [
						`Post one silent workflow checkpoint on ${state.executingIssue.key} with the ${TOOL_NAME} tool: action:"append_evidence", work:"${state.executingIssue.key}", kind:"${kind}".`,
						orientation,
						STOP_REMINDER_BOUNDARY,
						"This continuation fires once; later turns show only ⚠ while the checkpoint remains owed.",
					].join(" "),
				};
			} catch (error) {
				pi.logger.warn(`${TOOL_NAME}-now: stop obligation check failed (fail-open)`, { error: String(error) });
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
					const stagedCancel = await stageCancelBatch(now.key, ctx);
					if (stagedCancel === null) return;

					let confirmed: boolean;
					if (stagedCancel) {
						const cancelLines = stagedCancel.batch.entries.map(e => `  ${e.key} — "${e.reason}"`).join("\n");
						confirmed = await ctx.ui.confirm(
							`This is your verdict — close ${now.key} and cancel ${stagedCancel.cancellations.length} item(s)?`,
							`"${now.title}"\n\nAlso cancel:\n${cancelLines}\n\nCancellation batch SHA-256: ${stagedCancel.batch.digest}\n\nMoves primary to Done and targets to Canceled atomically. Not reversible from here.`,
						);
					} else {
						confirmed = await ctx.ui.confirm(
							`This is your verdict — close ${now.key}?`,
							`"${now.title}"\n\nMoves to Done + records the verdict. Not reversible from here.`,
						);
					}
					if (!confirmed) return;
					// OMP-47: the LITERAL /done mints a fresh single-use authorization —
					// the service refuses reuse and refuses the /summary reference.
					const line = await backend.closeWithVerdict(now, "done", undefined, carrier(), hooksFor(ctx), `done:${randomUUID()}`, stagedCancel?.cancellations);
					if (stagedCancel) {
						try {
							consumeStagedCancelBatch(stagedCancel.batch, "consumed");
						} catch (error) {
							pendingNotices.push(`[${TOOL_NAME}] cancellations WERE applied, but archiving the cancel batch file failed (${String(error)}) — remove ${stagedCancel.batch.path} manually before the next /done`);
						}
					}
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

		// OMP-25 / OMP-107 /center: one fresh, read-only deterministic orientation readout.
		// Never fires at launch, on a timer, or after another command.
		pi.registerCommand("center", {
			description: "Centering orientation: where you are, what's next, what's stuck, what just moved",
			handler: async (args, ctx) => {
				if (args.trim()) {
					ctx.ui.notify("/center takes no arguments", "warning");
					return;
				}
				if (activeCenterRunId !== null) {
					ctx.ui.notify("A centering turn is already running — wait for it to finish", "warning");
					return;
				}
				if (!ctx.isIdle()) {
					ctx.ui.notify("The agent is mid-turn — run /center once it finishes", "warning");
					return;
				}
				const runId = nextCenterRunId++;
				activeCenterRunId = runId;
				const capturedSessionId = pi.getSessionId();
				try {
					let snapshot: CenterSnapshot;
					try {
						snapshot = await backend.centerSnapshot(projectFilter ?? undefined);
					} catch (e) {
						if (pi.getSessionId() !== capturedSessionId) return;
						// Tree/focus failure: one honest error beats a stale orientation.
						ctx.ui.notify(`/center failed: ${String(e)} — no orientation produced`, "error");
						return;
					}
					if (pi.getSessionId() !== capturedSessionId) return;
					if (!ctx.isIdle()) {
						ctx.ui.notify("A turn started while /center was reading the ledger — run /center again once it finishes", "warning");
						return;
					}
					const readout = renderCenterReadout(snapshot);
					try {
						await pi.deliverMessage({
							customType: CENTER_READOUT_TYPE,
							content: "Owner requested a read-only centering view; no action requested.",
							details: { readout },
							display: true,
							triggerTurn: false,
						});
					} catch (e) {
						if (pi.getSessionId() !== capturedSessionId) return;
						ctx.ui.notify(`/center failed: ${String(e)} — no orientation produced`, "error");
					}
				} finally {
					if (activeCenterRunId === runId) {
						activeCenterRunId = null;
					}
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
				project: z.string().optional().describe("Project name (create_work target, record_health, list_work filter)"),
				health: z.enum(["onTrack", "atRisk", "offTrack"]).optional().describe("Project health (record_health)"),
				body: z.string().optional().describe("Receipt body or close reason; record_health is status-only and refuses body"),
				kind: kindEnum.optional().describe(KIND_DESCRIPTION),
				queue: z.boolean().optional().describe(`create_work: also mark ${backend.queueNoun} so the work lands in the owner decision queue`),
				question: z.string().optional().describe("Owner decision question (max 240 chars, single line) — required when creating a TRIAGE issue (queue:true) or queueing an issue (queue_work)"),
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
				const deny = async (msg: string) => {
					let text = msg;
					if (!msg.startsWith("CONFIRM REQUIRED")) {
						const workTarget = params.work || currentNowRef()?.key;
						if (workTarget) {
							try {
								const detail = await backend.issueDetail(workTarget);
								const banner = renderNextActionBanner(workTarget, detail.attemptSnapshot, summaryAuthorized);
								if (banner.length > 0) {
									text = `${banner.join("\n")}\n\n${msg}`;
								}
							} catch {
								// degrade to original refusal
							}
						}
					}
					return finalize({ content: [{ type: "text" as const, text }], details: { success: false } });
				};
				const okText = (text: string, details: Record<string, unknown> = {}) =>
					finalize({ content: [{ type: "text" as const, text }], details: { success: true, ...details } });
				if (!(ACTIONS as readonly string[]).includes(params.action)) return deny(`unknown action "${params.action}"`);
				try {
					// HOME-114: wrap-up writes remain host-locked; only /done closes.
					// This refusal names the lock at any depth — checked first.
					if ((action === "record_health" || action === "waive_delivery" || action === "cancel_work") && !closeoutAuthorized) {
						return deny(LOCK_REFUSAL);
					}
					// Review provenance refusal names /summary at any depth — checked
					// before the depth guard so a forged subagent summary gets the
					// informative refusal, not a generic one. An OWNER session with no
					// approved plan falls through to the case body's "Run /plan first"
					// (OMP-127: a failed gate leaves summaryAuthorized false).
					if (action === "append_evidence" && params.kind === backend.reviewKind && !summaryAuthorized) {
						// Owner + no approved plan → fall through to "Run /plan first".
						// A failed lookup stays false: fail closed with the refusal.
						let planGuidance = false;
						if (ownerSession(ctx) && params.work) {
							try {
								planGuidance = !((await backend.workflowState(params.work))?.plan);
							} catch {
								// fail closed below
							}
						}
						if (!planGuidance) {
							return deny("REFUSED — a closeout review receipt requires Chris to literally enter /summary in this owner session.");
						}
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
						case "list_work": {
							if (!params.project) return deny("project required");
							const project = params.project;
							if (!(await backend.projectScopeExists(project))) {
								return deny(`project "${project}" does not exist`);
							}
							const { surfaces } = await backend.mapData(undefined, project);
							const issues = surfaces[0]?.issues ?? [];
							const lines = [...issues]
								.sort((left, right) => left.key.localeCompare(right.key))
								.map(issue => `${issue.key} | ${issue.state} | ${issue.title}`);
							return okText(lines.length > 0 ? lines.join("\n") : `Project "${project}" — no open items`);
						}
						case "get_work": {
							if (!params.work) return deny("work key required");
							const i = await backend.issueDetail(params.work);
							const banner = renderNextActionBanner(params.work, i.attemptSnapshot, summaryAuthorized);
							return okText(
								[
									...banner,
									`${params.work} ${i.title}`,
									`state: ${i.state} · project: ${i.project ?? "none"} · labels: ${i.labels.join(",") || "none"}`,
									i.description ?? "",
									"RECEIPTS:",
									i.digestPacket,
									...(i.attemptSnapshot
										? [`CLOSE ATTEMPT: state ${i.attemptSnapshot.state} · candidate ${i.attemptSnapshot.candidateId ?? "none"} · commit ${i.attemptSnapshot.candidateCommit ?? "none"} · launches left ${i.attemptSnapshot.remainingLaunches} · reports left ${i.attemptSnapshot.remainingReports}`]
										: []),
									...renderPlanPacket(i.planPacket),
								].join("\n"),
							);
						}
						case "run_audit": {
							if (!params.work) return deny("work key required");
							if (!ownerSession(ctx)) {
								return deny(`REFUSED — ${TOOL_NAME} writes are owner-session only (task depth 0); a subagent holds no bearer.`);
							}
							if (!summaryAuthorized) {
								return deny("REFUSED — a closeout audit requires Chris to literally enter /summary in this owner session.");
							}
							const now = currentNowRef();
							if (!now || now.key !== params.work) {
								return deny(`REFUSED — named work item "${params.work}" must be current NOW ("${now?.key ?? "none"}").`);
							}
							const issue = await backend.issueDetail(params.work);
							const snapshot = issue.attemptSnapshot;
							if (!snapshot || snapshot.state !== "audit_ready") {
								return deny(`REFUSED — ${params.work} close attempt state is "${snapshot?.state ?? "none"}" (expected "audit_ready").`);
							}
							if (!issue.auditTask || issue.auditTask.attemptId !== snapshot.attemptId) {
								return deny(`REFUSED — ${params.work} has no sealed audit task matching live attempt ${snapshot.attemptId}.`);
							}
							let runner: NativeAuditRunner;
							try {
								runner = await prepareNativeAuditRunner(ctx);
							} catch (error) {
								return deny(`REFUSED — auditor runner preparation failed: ${error instanceof Error ? error.message : String(error)}`);
							}

							const reservation = await backend.reserveAuditorLaunch(params.work, issue.auditTask.taskSha256, _id);
							if (reservation.status === "refused" || !reservation.launchId) {
								if (reservation.event?.requiresDelivery) {
									queueCheckpointDelivery(pi, backend, reservation.event, notice => {
										pendingNotices.push(`[${TOOL_NAME}] reserve checkpoint delivery failed (${notice})`);
									});
								}
								return deny(`Audit launch refused by the ledger:\n${reservation.event?.renderedText ?? "unknown reservation refusal"}`);
							}

							let auditRun: NativeAuditRunResult;
							try {
								auditRun = await runner(issue.auditTask.taskBody, snapshot.attemptId, _signal);
							} catch (error) {
								auditRun = { started: false, error: String(error) };
							}

							if (!auditRun.started) {
								try {
									const cancelOutcome = await backend.cancelAuditorLaunch(params.work, reservation.launchId);
									if (cancelOutcome.event?.requiresDelivery) {
										queueCheckpointDelivery(pi, backend, cancelOutcome.event, notice => {
											pendingNotices.push(`[${TOOL_NAME}] cancel checkpoint delivery failed (${notice})`);
										});
									}
								} catch (error) {
									// cancel failed
								}
								return deny(`Auditor launch failed before start: ${auditRun.error ?? "runner cancelled before dispatch"}`);
							}

							const transportPayload = auditRun.payload && auditRun.payload.trim().length > 0 ? { payload: auditRun.payload } : { failed: true };
							const settleOutcome = await backend.settleAuditorLaunch(params.work, reservation.launchId, transportPayload);
							if (settleOutcome.event?.requiresDelivery) {
								queueCheckpointDelivery(pi, backend, settleOutcome.event, notice => {
									pendingNotices.push(`[${TOOL_NAME}] settle checkpoint delivery failed (${notice})`);
								});
							}

							if (settleOutcome.status === "refused") {
								return deny(`Audit launch settlement refused by ledger:\n${settleOutcome.event.renderedText}`);
							}

							if (settleOutcome.verdict === "NEEDS_FIX" || settleOutcome.verdict === "BLOCKED") {
								const reportSuffix = auditRun.payload ? `\n\n## Auditor Report\n${auditRun.payload}` : "";
								return okText(`${settleOutcome.event.renderedText}${reportSuffix}`);
							}

							return okText(settleOutcome.event.renderedText);
						}
						case "append_evidence": {
							if (!params.work || !params.body) return deny("work key and body required");
							if (!params.kind) return deny(`kind required — every receipt is typed: ${KIND_DESCRIPTION}`);
							const kind: EvidenceKind = params.kind;
							const workflow = await backend.workflowState(params.work);
							if (!workflow?.plan) {
								return deny(`Run /plan first; ${params.work} has no current approved plan.`);
							}
							// Close-ritual kinds (everything but the execution handoff and a
							// same-session child receipt) require a completed owner /summary.
							if (kind !== "handoff" && kind !== "same_session_found_fixed" && !summaryAuthorized) {
								const reason = summaryBlockReason
									? `/summary was recognized but did not complete: ${summaryBlockReason}`
									: "Chris must literally enter /summary in this owner session";
								return deny(`REFUSED — ${kind} evidence is a close-ritual write; ${reason}.`);
							}
							const issue = workflow?.issue ?? (await backend.findIssue(params.work));
							if (kind === backend.reviewKind && ownerSession(ctx)) {
								// OMP-152: self-heal stale requires_delivery checkpoints BEFORE the
								// closeout append — a superseded attempt's undelivered event must not
								// strand an otherwise-complete close behind a session restart. The
								// helper queues fire-and-forget (OMP-97: never await deliverMessage);
								// an unreadable preflight read surfaces via its notice and refuses.
								const preflightNotices: string[] = [];
								const preflight = await queuePendingCheckpointDeliveries(pi, backend, issue.key, notice => preflightNotices.push(notice));
								if (preflight.events.length > 0) {
									return deny(
										`closeout receipt not recorded yet — ${preflight.events.length} pending checkpoint(s) queued. Yield the turn now, then retry this exact closeout write.\n\n`
										+ preflight.events.map(event => event.renderedText).join("\n\n"),
									);
								}
								if (preflightNotices.length > 0) {
									return deny(preflightNotices.join("; "));
								}
							}
							const meta: EvidenceMeta = { ...(workflow?.plan ? { planHash: workflow.plan.hash } : {}) };
							try {
								await backend.appendEvidence(issue, kind, params.body, meta, summaryAuthorizationRef);
							} catch (error) {
								return deny(`${String(error)} on ${issue.key} — workflow state unchanged`);
							}
							if (kind === "handoff") settleCheckpoint(issue.id, "handoff", ctx);
							else if (kind === backend.reviewKind) settleCheckpoint(issue.id, "review", ctx);
							if (kind === "verification" && summaryAuthorized && ownerSession(ctx)) {
								// OMP-50: the service seals the audit manifest from the exact
								// verification receipt — get_work then renders the sealed task.
								try {
									const sealed = await backend.sealAuditManifest(issue);
									if (sealed.event.requiresDelivery) {
										queueCheckpointDelivery(pi, backend, sealed.event, notice => {
											pendingNotices.push(`[${TOOL_NAME}] seal checkpoint delivery failed (${notice}) — it retries at the next owner session start`);
										});
									}
									if (sealed.status === "applied") {
										const eventText = sealed.event?.renderedText ? `\n\n${sealed.event.renderedText}` : "";
										return okText(`${kind} receipt recorded on ${issue.key}; audit manifest sealed — run native audit with work action:"run_audit", work:"${issue.key}".${eventText}`);
									}
									return okText(`${kind} receipt recorded on ${issue.key}; manifest not sealed — ${sealed.event.reasonCode}: ${sealed.event.renderedText}`);
								} catch (error) {
									return okText(`${kind} receipt recorded on ${issue.key}; manifest not sealed (${String(error)}) — begin the close attempt with /summary, then append verification again`);
								}
							}
							if (kind === backend.reviewKind && ownerSession(ctx)) {
								// OMP-51/OMP-97: the review checkpoint the service minted must reach the
								// owner and be attested before /done can succeed. Queued delivery
								// avoids turn-yield deadlock in tool handlers.
								const queued = await queuePendingCheckpointDeliveries(pi, backend, issue.key, notice => {
									pendingNotices.push(`[${TOOL_NAME}] ${notice}`);
								});
								const yieldNote = queued.events.length > 0 ? " Yield the turn now before the next close step." : "";
								const eventText = queued.events.length > 0 ? `\n\n${queued.events.map(e => e.renderedText).join("\n\n")}` : "";
								return okText(`${kind} receipt recorded on ${issue.key}.${yieldNote}${eventText}`);
							}
							return okText(`${kind} receipt recorded on ${issue.key}`);
						}
						case "create_work": {
							if (intakeActive && intakeScanRequired && !intakeScanDelivered) {
								return deny(
									"Intake visible scan required before publication: deliver a standalone tool-free assistant message containing 'Figured out myself', 'Asking you', and 'Leaving for later' headings first.",
								);
							}
							// OMP-139 atomic same-session filing: selected ONLY by the exact tuple
							// work:<existing parent> + kind:"same_session_found_fixed" + title +
							// body (## Finding / ## Verification). Partial tuples and unsupported
							// authority fields are refused BEFORE any preview.
							if (params.kind !== undefined || params.work !== undefined) {
								if (params.kind !== "same_session_found_fixed") {
									return deny('create_work accepts kind/work only as the atomic same-session form: kind:"same_session_found_fixed", work:<parent key>, title, and a body with `## Finding` and `## Verification`');
								}
								if (!params.work) return deny("the atomic same-session filing needs work:<existing parent key>");
								if (!params.title) return deny("title required");
								if (params.batch !== undefined) return deny("the atomic same-session filing rejects batch — it files exactly one child");
								if (params.queue !== undefined) return deny("the atomic same-session filing rejects queue — the child closes with the parent's /done");
								if (params.question !== undefined) return deny("the atomic same-session filing rejects question — no owner decision is queued");
								if (params.project !== undefined) return deny("the atomic same-session filing rejects project — the child inherits the parent's project");
								const sections = params.body ? sameSessionSections(params.body) : null;
								if (!sections) {
									return deny("the atomic same-session filing needs a body with non-empty `## Finding` and `## Verification` sections");
								}
								const parent = await backend.findIssue(params.work);
								const gate = confirmWrite(
									"create_work",
									"Model wants to file a same-session found-and-fixed child",
									`parent: ${parent.key} ${parent.title}\nchild: "${params.title}"\n\n## Finding\n${sections.finding}\n\n## Verification\n${sections.verification}\n\nFiles atomically against the parent's live /summary attempt; the child closes with the parent's /done.`,
									params,
								);
								if (!gate.approved) return deny(gate.preview);
								const created = await backend.createSameSessionChild({
									parentKey: parent.key,
									ownerSessionId: piRef.getSessionId(),
									title: params.title,
									...(params.description ? { description: params.description } : {}),
									finding: sections.finding,
									verification: sections.verification,
								});
								return okText(`created ${created.key} as a same-session child of ${parent.key} — it closes with the parent's /done`, { identifier: created.key, parent: parent.key });
							}
							if (params.queue) {
								if (!params.question || typeof params.question !== "string" || !params.question.trim()) {
									return deny("question required when creating a TRIAGE issue (queue:true)");
								}
								const q = params.question.trim();
								if (q.includes("\n") || q.length > 240) {
									return deny("question must be a single line of at most 240 characters");
								}
							}
							if (!params.title) return deny("title required");
							if (intakeActive) {
								const blueprint = await readLatestIntakeBlueprint(ctx);
								if (!blueprint) {
									return deny("intake_blueprint_missing: save and lint local://intake-{slug}.md before publishing");
								}
								if (params.description === undefined || params.description !== blueprint.content) {
									return deny(`intake_blueprint_mismatch: description must exactly match ${blueprint.url}; save the changed bytes and re-run intake lint`);
								}
								if ((params.batch === undefined || params.batch.length === 0) && hasExplicitMultiDeliverableDeclaration(blueprint.content)) {
									return deny("intake_decomposition_required: blueprint declares multiple deliverables without native blocking relations; save one local://intake-{slug}.md per independent complaint, or publish one linked batch when the slices truly block each other");
								}
							}
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
								if (intakeActive) {
									for (let k = 0; k < n; k++) {
										const outgoing = (entries[k]!.blocks ?? []).length;
										let incoming = 0;
										for (let j = 0; j < n; j++) {
											if (j !== k && (entries[j]!.blocks ?? []).includes(k)) {
												incoming++;
											}
										}
										if (outgoing + incoming === 0) {
											return deny(`intake_decomposition_required: batch entry [${k}] "${entries[k]!.title}" has no blocking relations; publish as a separate single-issue blueprint`);
										}
									}
								}
								const target = fileTarget(params.project);
								const batchRefusal = unscopedRefusal(target);
								if (batchRefusal) return deny(batchRefusal);
								const edgeLines: string[] = [];
								for (let k = 0; k < n; k++) for (const j of entries[k]!.blocks ?? []) edgeLines.push(`[${k}] blocks [${j}]`);
								const queueText = params.queue ? `\n→ + ${backend.queueNoun} on parent\n\nOwner question:\n${params.question?.trim()}` : "";
								const detail = [
									`PARENT "${params.title}" → ${target ? `project ${target}` : cfg.teamNoun}${queueText}${selectsNow ? "\n→ becomes NOW" : ""}${params.description ? `\n${params.description}` : ""}`,
									...entries.map((e, k) => `[${k}] "${e.title}"${e.description ? `\n${e.description}` : ""}`),
									edgeLines.length > 0 ? `edges:\n${edgeLines.join("\n")}` : "edges: none",
								].join("\n\n");
								const gate = confirmWrite("create_work", `Model wants to publish a BATCH — 1 parent + ${n} children`, detail, params);
								if (!gate.approved) return deny(gate.preview);
								try {
									const outcome = await backend.createBatch({
										parent: { title: params.title, ...(params.description ? { description: params.description } : {}), ...(target ? { project: target } : {}), ...(params.queue ? { queue: true } : {}), ...(params.question ? { question: params.question.trim() } : {}) },
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
							const queueText = params.queue ? `\n→ + ${backend.queueNoun} (lands in your decision queue)\n\nOwner question:\n${params.question?.trim()}` : "";
							const detail = `"${params.title}"\n→ ${target ? `project ${target}` : cfg.teamNoun}${queueText}${selectsNow ? "\n→ becomes NOW" : ""}\n\n${params.description ?? ""}`;
							const gate = confirmWrite(
								"create_work",
								"Model wants to file a work item",
								detail,
								params,
							);
							if (!gate.approved) return deny(gate.preview);
							const created = await backend.createIssue({
								title: params.title,
								...(params.description ? { description: params.description } : {}),
								...(target ? { project: target } : {}),
								...(params.queue ? { queue: true } : {}),
								...(params.question ? { question: params.question.trim() } : {}),
							});
							if (selectsNow) {
								await setNow(created, ctx);
								intakeSelected = true;
							}
							return okText(`created ${created.key}${params.queue ? ` + ${backend.queueNoun}` : ""}${selectsNow ? " + NOW" : ""}`, { identifier: created.key, now: selectsNow });
						}
						case "queue_work": {
							if (!params.work) return deny("work key required");
							if (!params.question || typeof params.question !== "string" || !params.question.trim()) {
								return deny("question required for queue_work (single line, max 240 chars)");
							}
							const question = params.question.trim();
							if (question.includes("\n") || question.length > 240) {
								return deny("question must be a single line of at most 240 characters");
							}
							const issue = await backend.findIssue(params.work);
							const gate = confirmWrite(
								"queue_work",
								"Model wants to add an issue to your decision queue",
								`${issue.key} ${issue.title}\n\nOwner question:\n${question}\n\nAdds ${backend.queueNoun}. Nothing else changes.`,
								params,
							);
							if (!gate.approved) return deny(gate.preview);
							await backend.queueIssue(issue, question);
							return okText(`${issue.key} → ${backend.queueNoun}`);
						}
						case "waive_delivery": {
							// OMP-51: an owner-visible two-phase waiver for ONE failed pending
							// checkpoint delivery. Only a failed latest delivery can be waived;
							// the service enforces that — this gate makes the choice visible.
							if (!params.work) return deny("work key required");
							const issue = await backend.findIssue(params.work);
							const pending = await backend.pendingDeliveries(issue.key);
							const target = params.body ? pending.find(event => event.eventId === params.body) : pending.length === 1 ? pending[0] : undefined;
							if (!target) {
								return deny(
									pending.length === 0
										? `nothing to waive — ${issue.key} has no pending checkpoint deliveries`
										: `name the event to waive in body — pending: ${pending.map(event => `${event.eventId} (${event.eventType})`).join("; ")}`,
								);
							}
							const gate = confirmWrite(
								"waive_delivery",
								"Model wants to WAIVE a failed checkpoint delivery",
								`${issue.key} ${issue.title}\nevent: ${target.eventType} (${target.eventId})\n\n${target.renderedText}\n\nWaiving records that Chris accepts this checkpoint as handled without a delivered message.`,
								params,
							);
							if (!gate.approved) return deny(gate.preview);
							const outcome = await backend.attestDelivery(target.eventId, pi.getSessionId(), target.renderedSha256, "waived", `waiver:${params.confirmation_id}`);
							if (outcome.status === "refused") return deny(outcome.event.renderedText);
							return okText(`delivery waived for event ${target.eventId} on ${issue.key}`);
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
							if (params.body !== undefined) return deny("REFUSED — record_health stores only project health and updated_at; omit body.");
							if (!params.project || !params.health) return deny("project and health required");
							const gate = confirmWrite("record_health", "Model wants to post a project update", `${params.project} → ${params.health}`, params);
							if (!gate.approved) return deny(gate.preview);
							await backend.recordHealth(params.project, params.health);
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
								`${issue.key} ${issue.title}${params.title ? `\n→ new title: "${params.title}"` : ""}${params.description ? `\n→ new description:\n${params.description}` : ""}`,
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
							const refusal = nowRefusal(issue);
							if (refusal) return deny(refusal);
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
