/**
 * workflow/backend.ts — the WorkflowBackend contract for the Work Ledger adapter,
 * plus the workflow constants and types. The host (host.ts) owns all session state,
 * obligations, confirmations, footer, NOW window, and digest rendering; a backend
 * supplies storage I/O only.
 */

import type { AuditBinding } from "./audit-bridge";

export const WORKFLOW_SEQUENCE =
	"WORKFLOW SEQUENCE: /intake creates and selects → /plan approves, stamps, and executes → execution handoff → /summary reviews → /done closes. The ledger tracks every stage; Chris never moves state, re-identifies the work, or answers bookkeeping prompts.";

/** HOME-114: closeout is explicit-command only. */
export const CLOSEOUT_BOUNDARY =
	"Closeout is explicit-command only: /summary (questionyourself + whatsmissing + close ritual) and /done run ONLY when Chris literally enters them — never start a summary, close proposal, health update, capture triage, or NOW handoff because work, a todo list, or the session looks finished; a keep-open verdict blocks every closeout action until he enters one.";
export const STOP_REMINDER_BOUNDARY =
	"Post the checkpoint silently. Never narrate bookkeeping or start /summary or /done.";
export const PLAN_APPROVED_PREFIX = "**Plan approved**";
export const EXECUTION_HANDOFF_PREFIX = "**Execution handoff**";
export const SESSION_REVIEW_PREFIX = "**Session review**";
export const CLOSEOUT_LOCK_REFUSAL =
	"REFUSED — closeout lock (HOME-114): no owner-entered /summary or /done this session. record_health, request_closeout, and cancel_work are wrap-up writes; they unlock only when Chris literally enters /summary or /done. If he wants this write, he must enter one of those commands himself — do not retry on your own.";

/** One explicit ceiling on the get_work PLAN PACKET (plan body + acceptance
 *  criteria bytes). Over it, the packet says so and audit spawn is refused —
 *  bytes are never silently omitted (OMP-38). */
export const PLAN_PACKET_MAX_BYTES = 32 * 1024;

/** Thrown by appendEvidence when an audit receipt's expected candidate no
 *  longer matches the ledger's LIVE candidate (OMP-38). The host reacts by
 *  invalidating the receipt and clearing the binding — only a fresh /summary
 *  freeze + audit can retry. */
export class CandidateDriftError extends Error {}

/** Bounded audit-reconstruction packet (OMP-38): everything the /summary
 *  auditor task needs, read from the ledger's newest plan receipt on the
 *  current candidate — never from transcript archaeology. */
export interface PlanPacket {
	candidateId: string;
	candidateSha256: string;
	/** Absent until /summary finalizes the candidate. */
	commitSha?: string;
	/** payload_sha256 of the plan receipt — the value the auditor task must cite. */
	planReceiptSha256: string;
	/** SHA-256 of the approved plan document (stamp hash). */
	planSha256: string;
	/** Exact stored plan body; absent when capped. */
	planBody?: string;
	acceptanceCriteria: string[];
	/** Present ⇒ body + criteria exceeded the ceiling and were withheld. */
	capped?: { bytes: number; max: number };
}

export interface BackendIssue {
	id: string;
	key: string; // HOME-31 / OMP-12
	title: string;
	project?: string; // display name
	state: string; // display state name
	updatedAt: string;
	waiting: boolean;
	isNow: boolean;
	description?: string;
	labels: string[];
}

export interface MapSurface {
	name: string;
	health?: string;
	state: string;
	issues: BackendIssue[];
	waiting: number;
}

export interface IssueDetail {
	title: string;
	state: string;
	project?: string;
	labels: string[];
	description?: string;
	blockedBy: string[];
	blocks: string[];
	related: string[];
	comments: { at: string; author: string; head: string }[];
	commentsTotal: number;
	commentsLast7d: number;
	digestPacket: string;
	/** OMP-38: bounded ledger packet for audit reconstruction (work backend). */
	planPacket?: PlanPacket;
}

export interface TreeItem {
	key: string;
	title: string;
	bucket: "done" | "working" | "stuck" | "onyou" | "next";
	blocker?: string;
	isNow: boolean;
}

export interface GoalTree {
	goal: string;
	health?: string;
	promise?: string;
	items: TreeItem[];
	counts: { done: number; total: number; stuck: number; onyou: number };
}

export interface NowRef {
	id: string;
	key: string;
	title: string;
	project?: string;
}

/** /center snapshot (OMP-25) — one fresh, bounded, read-only orientation.
 *  Rows are preformatted owner-facing lines (key + title, no bodies); each
 *  section carries its full count so truncation stays honest. */
export interface CenterSnapshot {
	/** Global focus — reported honestly even when it belongs to another project. */
	now: NowRef | null;
	/** NOW's own goal progress (counts over its project), when NOW has one. */
	progress?: { done: number; total: number; onyou: number };
	/** Open, non-queued work excluding NOW — scoped by projectFilter when given. */
	ready: { rows: string[]; total: number };
	/** Owner decision queue (TRIAGE) — scoped by projectFilter when given. */
	waiting: { rows: string[]; total: number };
	/** Newest applied receipt/close-proposal/completion metadata, or an honest
	 *  unavailable reason — the other sections survive an activity failure. */
	activity: { rows: string[]; total: number } | { unavailable: string };
}

export interface WorkflowCheckpoint {
	issue: NowRef;
	plan?: { hash: string; at: string };
	handoff?: { at: string };
	review?: { hash: string; at: string };
}

export interface PlanStamp {
	hash: string;
	body?: string;
	title: string;
	planFilePath: string;
	approach: string[];
	verification: string[];
}
export interface BatchEntry {
	title: string;
	description?: string;
	blocks?: number[];
}

export interface CreateBatchInput {
	parent: { title: string; description?: string; project?: string; queue?: boolean };
	entries: BatchEntry[];
}

export interface BatchOutcome {
	parent: NowRef;
	children: NowRef[];
	edges: string[]; // "OMP-2 blocks OMP-3"
	text: string; // result line for the tool response
}

/** Model-facing evidence kinds — every kind exists in work.omp.dev/v1.
 *  There is deliberately no generic "evidence" default: the ledger records
 *  typed receipts only. The session review is `closeout`; the fresh auditor's
 *  report is a separate exact-body `audit` write bound through the receipt bridge. */
export type EvidenceKind = "handoff" | "verification" | "audit" | "closeout";

export interface EvidenceMeta {
	planHash?: string;
	verdict?: "PASS" | "NEEDS_FIX" | "BLOCKED"; // work audit receipts (bridge-supplied only)
	independent?: boolean;
	candidateSha256?: string; // binds verification/audit/closeout to the final candidate
	/** OMP-38: the candidate identity the audit receipt was captured under —
	 *  the backend refuses the append when the LIVE candidate differs. */
	expectedCandidate?: { id: string; sha256: string; commit: string };
}

/** Hooks the host hands a backend for interactive flows (freeze/push verdicts). */
export interface BackendHooks {
	ui: {
		confirm(title: string, body: string): Promise<boolean>;
		notify(msg: string, level?: "info" | "warning" | "error"): void;
	};
	cwd: string;
	preExistingDirtyPaths: readonly string[];
	notices: string[]; // backend pushes one-line notices; host flushes next turn
}

export interface SummaryGateOk {
	ok: true;
	/** The issue the plan/review state actually lives on (workflowState's ref). */
	issue: NowRef;
	/** Present ⇒ arm the review obligation with this hash; absent ⇒ unarmed
	 *  (no plan stamped / nothing frozen — review may run, /done stays blocked). */
	planHash?: string;
	warning?: string;
	/** Work backend: candidate ids allocated during the gate (freeze/finalize) —
	 *  the host merges them into the opaque carrier it persists. */
	carrier?: WorkStateCarrier;
	/** Work backend (OMP-38): the finalized candidate this /summary attempt is
	 *  bound to — present whenever a finalized candidate with a plan receipt
	 *  exists; the host registers it on the audit bridge. */
	binding?: AuditBinding;
}

export interface SummaryGateBlocked {
	ok: false;
	reason: string;
}

export interface WorkStateCarrier {
	revisionId?: string;
	plannedCandidateId?: string;
	candidateId?: string;
	candidateSha?: string;
	commitSha?: string;
	closeoutRequested?: boolean;
}

export interface WorkflowBackend {
	readonly name: "work";
	readonly serviceLabel: string; // "Work Ledger"
	readonly markerFile: string; // committed project-scope marker
	readonly scopeFix: string; // one-line instructions for an unscoped repo
	readonly cacheFile: string; // work-now.json (basename under ~/.omp/agent)
	readonly queueNoun: string; // "TRIAGE" — for preview text
	/** Kind that settles the review obligation — the typed session review. */
	readonly reviewKind: "closeout";
	/** Evidence kinds this backend accepts. */
	readonly evidenceKinds: readonly EvidenceKind[];
	readonly bookendTitle: string; // "── Work Ledger bookend (work.omp.dev/v1) ──"
	/** Adapter-side mutable carrier: the host stores it opaquely in NowState.work. */
	readCarrier(raw: unknown): WorkStateCarrier;
	healthProbe(): Promise<void>; // throws on unreachable
	projectScopeExists(project: string): Promise<boolean>;
	mapData(nowKey?: string, projectFilter?: string): Promise<{ surfaces: MapSurface[]; capped: boolean }>;
	issueDetail(key: string): Promise<IssueDetail>;
	findIssue(key: string): Promise<NowRef>; // throws when absent
	currentNow(): Promise<NowRef | null>;
	goalTree(now: NowRef): Promise<GoalTree | null>;
	/** Backend-only digest lines after the tree: IN FLIGHT / NEEDS CHRIS / DRAIN RULE.
	 *  Throws on failure — the host degrades to one honest line, never blocks. */
	digestExtras(): Promise<string[]>;
	/** Bounded /work status rows (service probe, focus holder, drift). */
	statusLines(now: NowRef | null, ctx: { projectFilter?: string; digestInjected: boolean }): Promise<string[]>;
	workflowState(key: string): Promise<WorkflowCheckpoint>;
	/** The `waiting` tool read (owner decision queue). */
	waitingLines(): Promise<string[]>;
	/** The `tree` tool read (surface/milestone overview). */
	projectTreeLines(): Promise<string[]>;
	/** One fresh /center orientation read (OMP-25). Throws on tree/focus
	 *  failure — the host shows one honest error instead of a stale
	 *  orientation; only the activity section degrades internally. */
	centerSnapshot(projectFilter?: string): Promise<CenterSnapshot>;

	setNowRemote(issue: NowRef): Promise<void>;
	clearNowRemote(issueId: string | undefined): Promise<void>; // throws → host warns, clears locally
	/** Returns the issue the plan state lives on; work backend also the planned candidate id. */
	stampPlan(target: NowRef, stamp: PlanStamp): Promise<{ issue: NowRef; plannedCandidateId?: string }>;
	appendEvidence(issue: NowRef, kind: EvidenceKind, body: string, meta: EvidenceMeta): Promise<void>;
	createIssue(input: { title: string; description?: string; project?: string; queue?: boolean }): Promise<NowRef>;
	createBatch(input: CreateBatchInput): Promise<BatchOutcome>;
	queueIssue(issue: NowRef): Promise<void>;
	proposeClose(issue: NowRef, reason: string | undefined): Promise<void>;
	reviseWork(issue: NowRef, fields: { title?: string; description?: string }): Promise<void>;
	recordHealth(project: string, health: "onTrack" | "atRisk" | "offTrack", body: string): Promise<void>;

	/** null = clear to close; string = the exact refusal the owner sees. */
	closeBlocker(now: NowRef, carrier: WorkStateCarrier): Promise<string | null>;
	/** Full close: push + push receipt + complete_work + clear focus. Returns the
	 *  one-line result; throws on failure (state left recoverable). */
	closeWithVerdict(now: NowRef, outcome: "done" | "canceled", reason: string | undefined, carrier: WorkStateCarrier, hooks: BackendHooks): Promise<string>;
	/** Durable pending-operation journal:
	 *  deliveredOps() lists operation ids whose results have been handed to the
	 *  host since the last ack; the host passes them to ackOps() at
	 *  before_provider_request (proof the results entered conversation history).
	 *  ackOps drops exactly those claims plus TTL-expired resolved ones. */
	deliveredOps?(): string[];
	ackOps?(delivered: string[]): Promise<void>;

	/** /summary authorization: freeze the candidate and finalize it.
	 *  planHash arms the review obligation. */
	summaryGate(now: NowRef, carrier: WorkStateCarrier, hooks: BackendHooks): Promise<SummaryGateOk | SummaryGateBlocked>;
}

/** Thrown by createBatch after a partial publish — the host formats the exact
 *  landed inventory; a generic catch must never flatten it. */
export class BatchPartialError extends Error {
	constructor(
		cause: unknown,
		readonly landed: string[],
		readonly edgesLanded: string[],
		readonly notCreated: string[],
	) {
		super(String(cause));
	}
}
