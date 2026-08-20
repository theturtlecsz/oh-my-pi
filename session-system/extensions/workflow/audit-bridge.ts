/**
 * workflow/audit-bridge.ts — one-time audit receipts shared by model-bookends
 * (registrar) and the workflow host (consumer). Both extensions resolve this
 * module to the same instance in the agent process.
 *
 * HOME-147: on the Work Ledger backend an `append_evidence kind:"audit"` tool
 * call must never carry model-supplied report bytes — the main model could alter
 * or fabricate the auditor's report and self-declare independence. Instead:
 *
 *   1. model-bookends captures the report from the REAL auditor tool_result,
 *      parses the verdict, and registers a receipt keyed by the exact captured
 *      bytes' SHA-256, bound to the current transcript tag.
 *   2. The host's append_evidence kind:"audit" claims the receipt: the model's
 *      body must hash to a registered, unclaimed, same-transcript receipt; the
 *      persisted payload is the receipt's captured bytes and parsed verdict,
 *      marked independent. Anything else is refused.
 *   3. Only a SUCCESSFUL backend append commits the consumption
 *      (commitAuditReceipt). Validation failure or a service error releases the
 *      claim (releaseAuditReceipt) so the forward stays retryable.
 *
 * Receipts are one-time: a second forward of the same report after commit is
 * refused.
 */
import { createHash, randomBytes } from "node:crypto";

export interface AuditReceipt {
	reportSha256: string;
	report: string; // verbatim captured auditor output — the only bytes that may be persisted
	verdict: "PASS" | "NEEDS_FIX" | "BLOCKED";
	transcriptRef: string;
	presentedAt: number;
	claimed: boolean;
	consumed: boolean;
	/** Candidate binding in force when the report was captured (OMP-38). Kept
	 *  for the host to bind expected candidate identity into the ledger write. */
	binding?: AuditBinding;
	/** Bridge binding generation at capture time. Claim requires the CURRENT
	 *  generation: any register/clear after capture — including A→B→A — leaves
	 *  the receipt permanently unclaimable (OMP-38). */
	bindingGeneration: number;
}

/** The finalized candidate a /summary attempt is bound to (OMP-38). Registered
 *  by the workflow host right after a successful summary gate; model-bookends
 *  requires the auditor task to cite commitSha and planReceiptSha256 before it
 *  reserves the one bounded auditor slot. */
export interface AuditBinding {
	candidateId: string;
	candidateSha256: string;
	commitSha: string;
	planReceiptSha256: string;
	/** The ledger plan packet exceeded its byte ceiling — the audit task cannot
	 *  be reconstructed from bounded ledger data, so spawns are refused. */
	capped?: boolean;
}

const RECEIPT_TTL_MS = 30 * 60_000;

/** The omp loader cache-busts each top-level extension's module graph
 *  (`?mtime=` tag), so model-bookends.ts and workflow/host.ts each get their
 *  OWN copy of this module — module-local state here would split the receipt
 *  registry and the forward could never claim what bookends registered.
 *  Process-global storage is the shared runtime. */
interface BridgeStore {
	receipts: Map<string, AuditReceipt>;
	transcriptRef: string;
	binding?: AuditBinding;
	/** Monotonic: bumped by every registerAuditBinding/clearAuditBinding. */
	bindingGeneration: number;
}
const GLOBAL_KEY = "__ompWorkAuditBridge__";
const store = ((globalThis as Record<string, unknown>)[GLOBAL_KEY] as BridgeStore | undefined) ??
	((globalThis as Record<string, unknown>)[GLOBAL_KEY] = { receipts: new Map(), transcriptRef: "", bindingGeneration: 0 } satisfies BridgeStore);
// Pre-OMP-38 processes may hold a store without the generation counter.
if (typeof store.bindingGeneration !== "number") store.bindingGeneration = 0;
const receipts = store.receipts;

/** Per-process transcript tag. Generated lazily; the workflow host resets it on
 *  session start/switch so receipts never cross transcripts. */
export function currentTranscriptRef(): string {
	if (!store.transcriptRef) store.transcriptRef = `t-${randomBytes(8).toString("hex")}`;
	return store.transcriptRef;
}

export function resetTranscriptRef(): string {
	store.transcriptRef = `t-${randomBytes(8).toString("hex")}`;
	for (const [key, receipt] of receipts) if (!receipt.consumed) receipts.delete(key);
	clearAuditBinding(); // a fresh transcript never inherits a candidate binding
	return store.transcriptRef;
}

/** Bind this /summary attempt to one finalized candidate (host, after a
 *  successful summary gate). Every call invalidates receipts captured under
 *  any earlier binding — identical fields included. */
export function registerAuditBinding(binding: AuditBinding): void {
	store.binding = { ...binding };
	store.bindingGeneration += 1;
}

export function clearAuditBinding(): void {
	if (store.binding === undefined) return;
	store.binding = undefined;
	store.bindingGeneration += 1;
}

/** The binding for the current transcript, or null when none is registered. */
export function currentAuditBinding(): AuditBinding | null {
	return store.binding ? { ...store.binding } : null;
}

/** Monotonic binding generation — snapshot at auditor spawn, compare at tool
 *  result: a rebind while the auditor runs (A→B, even A→B→A) must refuse the
 *  late report instead of binding it to the new candidate (OMP-38 TOCTOU). */
export function currentBindingGeneration(): number {
	return store.bindingGeneration;
}

/** Hash of the canonical report bytes: CRLF-normalized, outer whitespace
 *  trimmed. Registration (model-bookends passes already-normalized bytes) and
 *  the host's forward-body lookup canonicalize identically, so an honest
 *  forward differing only by outer whitespace still matches its receipt while
 *  any interior edit never does (OMP-38 AC-3: no trim mismatch — live refusal
 *  of a trailing-newline forward observed 2026-08-20). */
export function reportSha256(report: string): string {
	return createHash("sha256").update(report.replace(/\r\n/g, "\n").trim(), "utf8").digest("hex");
}

/** Called by model-bookends when a real auditor tool_result yields a parseable
 *  report while the gate is armed. Returns the receipt (existing one if the same
 *  bytes were already registered and not yet consumed). */
export function registerAuditReceipt(report: string, verdict: AuditReceipt["verdict"]): AuditReceipt {
	const sha = reportSha256(report);
	const existing = receipts.get(sha);
	if (
		existing &&
		!existing.consumed &&
		existing.transcriptRef === currentTranscriptRef() &&
		existing.bindingGeneration === store.bindingGeneration
	) {
		return existing;
	}
	const receipt: AuditReceipt = {
		reportSha256: sha,
		report,
		verdict,
		transcriptRef: currentTranscriptRef(),
		presentedAt: Date.now(),
		claimed: false,
		consumed: false,
		...(store.binding ? { binding: { ...store.binding } } : {}),
		bindingGeneration: store.bindingGeneration,
	};
	receipts.set(sha, receipt);
	return receipt;
}

function claimable(sha: string): AuditReceipt | null {
	const receipt = receipts.get(sha);
	if (!receipt || receipt.claimed || receipt.consumed) return null;
	if (receipt.transcriptRef !== currentTranscriptRef()) return null;
	if (receipt.bindingGeneration !== store.bindingGeneration) return null;
	if (Date.now() - receipt.presentedAt > RECEIPT_TTL_MS) {
		receipts.delete(sha);
		return null;
	}
	return receipt;
}

/** Host pre-write step. Marks the receipt in-flight; the caller MUST persist
 *  receipt.report (never the model-supplied bytes) and then settle with
 *  commitAuditReceipt (write landed) or releaseAuditReceipt (write failed). */
export function claimAuditReceipt(sha: string): AuditReceipt | null {
	const receipt = claimable(sha);
	if (receipt) receipt.claimed = true;
	return receipt;
}

export function commitAuditReceipt(sha: string): void {
	const receipt = receipts.get(sha);
	if (receipt?.claimed) receipt.consumed = true;
}

export function releaseAuditReceipt(sha: string): void {
	const receipt = receipts.get(sha);
	if (receipt && !receipt.consumed) receipt.claimed = false;
}

/** Live-candidate drift (OMP-38): the ledger's current candidate no longer
 *  matches the receipt's bound identity. The receipt is deleted — not merely
 *  released — so the forward is retryable only after a fresh /summary
 *  freeze + audit; the stale binding is cleared with it. */
export function invalidateAuditReceipt(sha: string): void {
	receipts.delete(sha);
	clearAuditBinding();
}

/** model-bookends' forwarding probe: the audit tool call's body hashed to a
 *  receipt the host committed. */
export function auditReceiptConsumed(sha: string): boolean {
	return receipts.get(sha)?.consumed === true;
}
