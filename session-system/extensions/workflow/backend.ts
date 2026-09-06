/**
 * workflow/backend.ts — the WorkflowBackend contract for the Work Ledger adapter,
 * plus the workflow constants and types. The host (host.ts) owns all session state,
 * obligations, confirmations, footer, NOW window, and digest rendering; a backend
 * supplies storage I/O only.
 */
import type {
	Candidate,
	Command,
	CommandResult,
	EvidenceReceipt,
	ExecutionGrantItemClaim,
	ExecutionGrantItemView,
	ExecutionGrantView,
	ExecutionJudgeManifest,
	ExecutionMode,
	ExecutionProvenanceEnvelope,
	WorkClient,
} from "@oh-my-pi/pi-work-client";

export interface ExecutionSnapshot {
	grant: ExecutionGrantView;
	items: ExecutionGrantItemView[];
	activeItem: ExecutionGrantItemView | null;
}



export const WORKFLOW_SEQUENCE =
	"WORKFLOW SEQUENCE: /intake creates and selects → /plan approves, stamps, and executes → execution handoff → /summary reviews → /done closes. The ledger tracks every stage; Chris never moves state, re-identifies the work, or answers bookkeeping prompts.";

/** HOME-114: closeout is explicit-command only. */
export const CLOSEOUT_BOUNDARY =
	"Closeout is explicit-command only: /summary and /done run ONLY when Chris literally enters them — never start a summary, close proposal, health update, capture triage, or NOW handoff because work, a todo list, or the session looks finished; a keep-open verdict blocks every closeout action until he enters one.";
export const STOP_REMINDER_BOUNDARY =
	"Post the checkpoint silently. Never narrate bookkeeping or start /summary or /done.";
export const CLOSEOUT_LOCK_REFUSAL =
	"REFUSED — closeout lock (HOME-114): no owner-entered /summary or /done this session. record_health and cancel_work are wrap-up writes; they unlock only when Chris literally enters /summary or /done. If he wants this write, he must enter one of those commands himself — do not retry on your own.";

/** One explicit ceiling on the get_work PLAN PACKET (plan body + acceptance
 *  criteria bytes). Over it, the packet says so and audit spawn is refused —
 *  bytes are never silently omitted (OMP-38). */
export const PLAN_PACKET_MAX_BYTES = 32 * 1024;

/** Thrown by appendEvidence when the service refuses the write against the
 *  LIVE candidate (OMP-38/OMP-47). */
export class CandidateDriftError extends Error {}

// ---- OMP-47 close attempts (service-owned authority) ----

/** One historical work item riding this /summary's close attempt (OMP-93):
 *  batch-owned proof text sealed into the audited task. */
export interface RiderProof {
	work_id: string;
	revision_id: string;
	evidence: string;
}

/** OMP-111: one historical work item canceled atomically with /done. */
export interface CancellationProof {
	work_id: string;
	revision_id: string;
	reason: string;
}
export interface CloseAttemptSession {
	authorizationRef: string;
	sessionId: string;
	startedAt: string;
	startCommit: string;
	repository: string;
	diffSha256: string;
	dirtyPaths: string[];
	/** Sealed rider batch from the owner-staged .work-riders.json, if any. */
	riders?: RiderProof[];
	authorization_kind?: "summary" | "execution";
	execution_grant_id?: string;
	candidate_tree_sha?: string;
	original_request_sha256?: string;
	criteria_sha256?: string;
	plan_stamp_sha256?: string;
	judge_sha256?: string;
}

/** One typed service event, exactly as the ledger rendered it. */
export interface CloseEventView {
	eventId: string;
	eventType: string;
	reasonCode: string;
	renderedText: string;
	renderedSha256: string;
	requiresDelivery: boolean;
	requiresFreshAuthorization: boolean;
}

/** Typed outcome of a close-attempt command: refusals carry the event, never throw. */
export interface CloseAttemptOutcome {
	status: "applied" | "refused";
	attemptId?: string;
	attemptState?: string;
	verdict?: "PASS" | "NEEDS_FIX" | "BLOCKED";
	launchId?: string;
	event: CloseEventView;
}

/** OMP-50 / OMP-168: sealed task bytes are internal input to run_audit only, never model-facing. */
export interface SealedAuditTask {
	attemptId: string;
	attemptState: string;
	taskBody: string;
	taskSha256: string;
}

/** Bounded audit-reconstruction packet (OMP-38): everything the /summary
 *  auditor task needs, read from the ledger's newest plan receipt on the
 *  current candidate — never from transcript archaeology. */
export interface PlanPacket {
	candidateId: string;
	candidateSha256: string;
	/** Absent until /summary finalizes the candidate. */
	commitSha?: string;
	/** Commit at plan approval; anchors the audited implementation range. */
	baseCommit?: string;
	/** Dirty paths present at plan approval; excluded from implementation claims. */
	baseDirtyPaths?: string[];
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

export interface CloseAttemptSnapshot {
	attemptId: string;
	state: string;
	candidateId?: string;
	candidateSha?: string;
	candidateCommit?: string;
	remainingLaunches: number;
	remainingReports: number;
	hasManifest: boolean;
	isLaunchable: boolean;
	nextAction: string;
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
	/** OMP-50 / OMP-168: the sealed auditor task — internal input to run_audit only, never model-facing. */
	auditTask?: SealedAuditTask;
	/** OMP-140: attempt state snapshot from WorkService workflow data. */
	attemptSnapshot?: CloseAttemptSnapshot;
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
	/** Ledger state at lookup time (backend-filled refs only; host-built refs omit it). */
	state?: string;
	archived?: boolean;
}

/** Owner ruling 2026-08-25: closed work never becomes or stays NOW.
 *  Plain-words refusal for terminal refs; null = fine to focus.
 *  Refs without state (picker rows, fresh creates) are open by construction. */
export function nowRefusal(ref: NowRef): string | null {
	if (ref.archived) return `${ref.key} is archived — closed work can't be NOW`;
	const state = ref.state?.toUpperCase();
	if (state === "DONE" || state === "CANCELED" || state === "CANCELLED") {
		return `${ref.key} is ${state.toLowerCase()} — closed work can't be NOW (/now lists only open work)`;
	}
	return null;
}

export interface CenterCommandRecommendation {
	command: string;
	reason: string;
}

export interface CenterWaitingRow {
	key: string;
	question: string;
}

export interface CenterHiddenRow {
	key: string;
	reason: string;
}

/** /center snapshot (OMP-25 / OMP-107) — presentation-ready deterministic readout. */
export interface CenterSnapshot {
	/** Global focus — reported honestly even when it belongs to another project. */
	now: NowRef | null;
	/** Scope description for reporting. */
	scope?: string;
	/** NOW's own goal progress (counts over its project), when NOW has one. */
	progress?: { done: number; total: number; onyou: number };
	/** 1–3 command recommendations with reasons. */
	recommendations: CenterCommandRecommendation[];
	/** Genuine owner-decision rows (TRIAGE with stored question) and total. */
	waiting: { rows: CenterWaitingRow[]; total: number };
	/** Hidden rows (blocked work or legacy TRIAGE without question) and total. */
	hidden: { rows: CenterHiddenRow[]; total: number };
	/** Newest activity line and total, or an explicit unavailable marker. */
	activity: { rows: string[]; total: number } | { unavailable: string };
}
export const CENTER_READOUT_TYPE = "center-readout";

export const OWNER_QUESTION_HEADING = "## Owner question";

export function extractOwnerQuestion(description?: string): string | undefined {
	if (!description) return undefined;
	const match = /(?:^|\n)##\s+Owner question\s*\n([^\n]+)(?:\n|$)/.exec(description);
	if (!match) return undefined;
	const line = match[1]?.trim();
	if (!line || line.startsWith("#") || line.length > 240) return undefined;
	const afterIdx = match.index + match[0].length;
	const rest = description.slice(afterIdx);
	const nextLine = rest.split("\n")[0]?.trim();
	if (nextLine && !nextLine.startsWith("#")) return undefined;
	return line;
}

export function setOwnerQuestion(description: string | undefined, question: string): string {
	const trimmedQuestion = question.trim();
	const cleanDesc = (description ?? "").trim();
	if (!cleanDesc) {
		return `${OWNER_QUESTION_HEADING}\n${trimmedQuestion}`;
	}
	const idx = cleanDesc.indexOf(OWNER_QUESTION_HEADING);
	if (idx === -1) {
		return `${cleanDesc}\n\n${OWNER_QUESTION_HEADING}\n${trimmedQuestion}`;
	}
	const before = cleanDesc.slice(0, idx).trimEnd();
	const after = cleanDesc.slice(idx + OWNER_QUESTION_HEADING.length);
	const nextHeadingMatch = after.search(/\n(?=#{1,6}\s)/);
	const rest = nextHeadingMatch !== -1 ? after.slice(nextHeadingMatch).trimStart() : "";
	const parts = [before, `${OWNER_QUESTION_HEADING}\n${trimmedQuestion}`];
	if (rest) parts.push(rest);
	return parts.filter(Boolean).join("\n\n");
}

export function escapeMarkdown(text: string): string {
	if (!text) return "";
	const singleLine = text.replace(/[\r\n\t]+/g, " ").trim();
	return singleLine
		.replace(/([\\`*_{}[\]<|~#])/g, "\\$1")
		.replace(/^([-+>])/g, "\\$1")
		.replace(/^(\d+)\./g, "$1\\.");
}

export function renderCenterReadout(snapshot: CenterSnapshot): string {
	const sections: string[] = [];

	// 1. FOCUS
	const focusLines: string[] = ["# FOCUS"];
	if (snapshot.now) {
		const projectPrefix = snapshot.now.project ? `${escapeMarkdown(snapshot.now.project)} · ` : "";
		focusLines.push(`${projectPrefix}${escapeMarkdown(snapshot.now.key)} ${escapeMarkdown(snapshot.now.title)}`);
		if (snapshot.progress) {
			focusLines.push(`${snapshot.progress.done} of ${snapshot.progress.total} pieces done (${snapshot.progress.onyou} on you)`);
		}
	} else {
		focusLines.push("NOW unset — no work item selected");
	}
	sections.push(focusLines.join("\n"));

	// 2. DO NEXT
	const doNextLines: string[] = ["# DO NEXT"];
	if (snapshot.recommendations.length > 0) {
		for (const r of snapshot.recommendations) {
			const sanitizedCmd = r.command.replace(/[`\r\n]+/g, "").trim();
			doNextLines.push(`- \`${sanitizedCmd}\` — ${escapeMarkdown(r.reason)}`);
		}
	} else {
		doNextLines.push("(none)");
	}
	sections.push(doNextLines.join("\n"));

	// 3. WAITING ON YOU
	const waitingLines: string[] = ["# WAITING ON YOU"];
	if (snapshot.waiting.rows.length > 0) {
		for (const r of snapshot.waiting.rows) {
			waitingLines.push(`- ${escapeMarkdown(r.key)} — ${escapeMarkdown(r.question)}`);
		}
	} else {
		waitingLines.push("(none)");
	}
	sections.push(waitingLines.join("\n"));

	// 4. HIDDEN (N)
	const hiddenLines: string[] = [`# HIDDEN (${snapshot.hidden.total})`];
	if (snapshot.hidden.rows.length > 0) {
		for (const r of snapshot.hidden.rows) {
			hiddenLines.push(`- ${escapeMarkdown(r.key)} — ${escapeMarkdown(r.reason)}`);
		}
	} else {
		hiddenLines.push("(none)");
	}
	sections.push(hiddenLines.join("\n"));

	// 5. MOVED (N)
	if ("unavailable" in snapshot.activity) {
		sections.push(`# MOVED\nactivity unavailable (${escapeMarkdown(snapshot.activity.unavailable)})`);
	} else {
		const movedLines: string[] = [`# MOVED (${snapshot.activity.total})`];
		if (snapshot.activity.rows.length > 0) {
			for (const r of snapshot.activity.rows) {
				movedLines.push(escapeMarkdown(r));
			}
		} else {
			movedLines.push("(none)");
		}
		sections.push(movedLines.join("\n"));
	}

	return sections.join("\n\n");
}

export interface WorkflowCheckpoint {
	issue: NowRef;
	plan?: { hash: string; at: string };
	handoff?: { at: string };
	review?: { hash: string; at: string };
	/** OMP-134/OMP-137: live close-attempt state plus the newest
	 *  service-rendered event text for that attempt — the host emits closeout
	 *  continuations from THIS, never from a hardcoded receipt claim. */
	closeAttempt?: { state: string; latestEventText?: string };
}

export interface PlanStamp {
	hash: string;
	/** Exact approved plan text — the durable copy of the bytes `hash` seals
	 *  (OMP-155); never a reconstructed summary. */
	body: string;
	title: string;
	planFilePath: string;
	approach: string[];
	verification: string[];
	/** Current HEAD at plan approval; absent outside git. */
	baseCommit?: string;
	/** Dirty paths present at plan approval. */
	baseDirtyPaths?: string[];
}
export interface BatchEntry {
	title: string;
	description?: string;
	blocks?: number[];
}

export interface CreateBatchInput {
	parent: { title: string; description?: string; project?: string; queue?: boolean; question?: string };
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
 *  typed receipts only. The session review is `closeout`; audit receipts are
 *  minted ONLY by the service's settle transaction (OMP-47) and never appended
 *  through this surface. */
export type EvidenceKind = "handoff" | "verification" | "closeout" | "same_session_found_fixed" | "push" | "plan";
/** Split a same-session receipt body into its finding and verification texts
 *  (OMP-52): the model writes `## Finding` and `## Verification` sections. */
export function sameSessionSections(body: string): { finding: string; verification: string } | null {
	// `\Z` is not a JavaScript end anchor (it is a literal "z" under /i);
	// `(?![\s\S])` asserts true end-of-input.
	const finding = /^##\s+Finding\s*$([\s\S]*?)(?=^##\s|(?![\s\S]))/im.exec(body);
	const verification = /^##\s+Verification\s*$([\s\S]*?)(?=^##\s|(?![\s\S]))/im.exec(body);
	if (!finding?.[1]?.trim() || !verification?.[1]?.trim()) return null;
	return { finding: finding[1].trim(), verification: verification[1].trim() };
}

export interface EvidenceMeta {
	planHash?: string;
	candidateSha256?: string; // binds verification/closeout to the final candidate
	candidateCommit?: string;
	remoteRef?: string;
	remoteCommit?: string;
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
	/** Commit anchoring the implementation range sealed for audit. */
	auditBaseCommit?: string;
	/** Dirty paths present at the audit base. */
	auditBaseDirtyPaths?: string[];
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
	readonly workspaceId: string;
	workClient?: WorkClient;
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
	digestExtras(cwd: string): Promise<string[]>;
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
	appendEvidence(issue: NowRef, kind: EvidenceKind, body: string, meta: EvidenceMeta, authorizationRef?: string): Promise<CloseAttemptOutcome | EvidenceReceipt | void>;
	createIssue(input: { title: string; description?: string; project?: string; queue?: boolean; question?: string }): Promise<NowRef>;
	createBatch(input: CreateBatchInput): Promise<BatchOutcome>;
	/** OMP-139: one atomic same-session found-and-fixed filing — the BACKLOG
	 *  child (inheriting the parent's project), its child→parent edge, and the
	 *  typed same_session_found_fixed receipt bound to the parent's LIVE attempt
	 *  land in ONE service transaction or not at all. ownerSessionId must be the
	 *  current owner session; the service refuses a mismatch. */
	createSameSessionChild(input: {
		parentKey: string;
		ownerSessionId: string;
		title: string;
		description?: string;
		finding: string;
		verification: string;
	}): Promise<NowRef>;
	queueIssue(issue: NowRef, question?: string): Promise<void>;
	reviseWork(
		issue: NowRef,
		fields: {
			title?: string;
			description?: string;
			scope?: string;
			acceptance_criteria?: string[];
			expected_revision_id?: string;
		},
	): Promise<void>;
	recordHealth(project: string, health: "onTrack" | "atRisk" | "offTrack"): Promise<void>;

	/** null = clear to close; string = the exact refusal the owner sees. */
	closeBlocker(now: NowRef, carrier: WorkStateCarrier): Promise<string | null>;
	/** Full close: push + push receipt + complete_work + clear focus. Returns the
	 *  one-line result; throws on failure (state left recoverable). */
	closeWithVerdict(
		now: NowRef,
		outcome: "done" | "canceled",
		reason: string | undefined,
		carrier: WorkStateCarrier,
		hooks: BackendHooks,
		doneAuthorizationRef?: string,
		cancellations?: CancellationProof[],
	): Promise<string>;
	/** Resolve an owner-staged cancel batch ({key, reason} entries) to exact
	 *  CancellationProof bindings against each item's CURRENT revision. */
	resolveCancellations(entries: { key: string; reason: string }[], nowKey: string): Promise<CancellationProof[]>;
	/** Durable pending-operation journal:
	 *  deliveredOps() lists operation ids whose results have been handed to the
	 *  host since the last ack; the host passes them to ackOps() at
	 *  session start and before_provider_request.
	 *  ackOps retains delivered non-health claims until TTL, and sweeps resolved health claims. */
	deliveredOps?(): string[];
	ackOps?(delivered: string[]): Promise<void>;

	/** /summary authorization: freeze the candidate and finalize it.
	 *  planHash arms the review obligation. */
	summaryGate(now: NowRef, carrier: WorkStateCarrier, hooks: BackendHooks): Promise<SummaryGateOk | SummaryGateBlocked>;

	// ---- OMP-47 close attempts: the service owns every gate; these are transport ----

	/** Bind this literal /summary to one ledger-owned close attempt. */
	beginCloseAttempt(now: NowRef, session: CloseAttemptSession): Promise<CloseAttemptOutcome>;
	/** Resolve an owner-staged rider batch ({key, evidence} entries) to exact
	 *  RiderProof bindings against each item's CURRENT revision. Throws on any
	 *  unknown key, DONE item, or empty evidence — a staged batch never
	 *  silently shrinks. */
	resolveRiders(entries: { key: string; evidence: string }[]): Promise<RiderProof[]>;
	/** Seal the audit manifest from the newest verification receipt on the live attempt. */
	sealAuditManifest(now: NowRef): Promise<CloseAttemptOutcome>;
	/** The sealed task for the live attempt, or null before seal. */
	sealedAuditTask(key: string): Promise<SealedAuditTask | null>;
	/** Reserve one bounded auditor launch against the sealed task hash. */
	reserveAuditorLaunch(key: string, taskSha256: string, toolCallId: string): Promise<CloseAttemptOutcome>;
	/** Cancel a reservation when the host cannot start its auditor task. */
	cancelAuditorLaunch(key: string, launchId: string): Promise<CloseAttemptOutcome>;
	/** Settle the reserved launch with the UNTOUCHED transport payload. */
	settleAuditorLaunch(key: string, launchId: string, transport: { payload?: unknown; failed?: boolean }): Promise<CloseAttemptOutcome>;
	/** Unresolved requires_delivery events for this work item (any attempt). */
	pendingDeliveries(key: string): Promise<CloseEventView[]>;
	snapshotQueue(projectFilter?: string, currentKey?: string, cwd?: string): Promise<ExecutionGrantItemClaim[]>;
	attestDelivery(eventId: string, ownerSessionId: string, renderedSha256: string, status: "delivered" | "failed" | "waived", authorizationRef?: string): Promise<CloseAttemptOutcome>;
	getFocusVersion(): Promise<number>;

	// ---- OMP-180 execution cycle authority ----
	getExecution(key?: string): Promise<ExecutionSnapshot | null>;
	finalizeExecutionCandidate(key: string, plannedCandidateId: string, freeze: { commitSha: string; candidateSha256: string; paths: string[] }): Promise<Candidate>;
	snapshotQueue(projectFilter?: string, currentKey?: string): Promise<ExecutionGrantItemClaim[]>;
	beginExecution(input: {
		provenance: ExecutionProvenanceEnvelope;
		remoteRef: string;
		mode: ExecutionMode;
		items: ExecutionGrantItemClaim[];
		expectedFocusVersion: number;
		judgeSha256: string;
		judgeManifest: ExecutionJudgeManifest;
	}): Promise<ExecutionSnapshot>;
	activateExecutionItem(input: {
		grantId: string;
		expectedGrantVersion: number;
		position: number;
		workId: string;
		expectedRevisionId: string;
		gitBaseline: string;
		judgeSha256: string;
		expectedFocusVersion: number;
		expectedProjectId?: string;
		expectedBlockerIds?: string[];
	}): Promise<ExecutionSnapshot>;
	sealExecutionCriteria(input: {
		grantId: string;
		expectedGrantVersion: number;
		workId: string;
		expectedRevisionId: string;
		criteria: string[];
		descriptionSha256: string;
		judgeSha256: string;
	}): Promise<ExecutionSnapshot & { sealedCriteria: string[] }>;
	stampExecutionPlan(input: {
		grantId: string;
		expectedGrantVersion: number;
		workId: string;
		revisionId: string;
		candidateId: string;
		planFile: string;
		planBody: string;
		planSha256: string;
		approach: string[];
		verification: string[];
		paths: string[];
		candidateSha256: string;
		judgeSha256: string;
	}): Promise<ExecutionSnapshot>;
	setExecutionState(input: {
		grantId: string;
		expectedGrantVersion: number;
		targetState: "active" | "paused" | "stopped" | "canceled";
		reason?: string | null;
		judgeSha256: string;
	}): Promise<ExecutionSnapshot>;
	completeExecutionItem(input: {
		grantId: string;
		expectedGrantVersion: number;
		workId: string;
		attemptId: string;
		pushReceiptId: string;
		judgeSha256: string;
	}): Promise<ExecutionSnapshot>;
	getPendingExecutionClaims?(): Promise<Array<{ command: Command; result?: CommandResult }>>;
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
