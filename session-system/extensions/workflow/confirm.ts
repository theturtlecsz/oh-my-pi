/**
 * workflow/confirm.ts — transcript-bound confirmation receipts (HOME-147).
 *
 * Replaces bare `confirm:true` replays for model-initiated writes. First call
 * writes NOTHING: the host issues a receipt {id, action, question, payloadSha,
 * transcriptRef, presentedAt} and returns the preview text naming the id. The
 * repeat call must carry confirm:true plus the confirmation_id; the host
 * validates it (exists, unconsumed, fresh, same transcript, identical payload)
 * and consumes it before executing. Anything else is refused and writes nothing.
 *
 * Slash commands keep the ctx.ui.confirm modal — they are owner-entered already.
 */
import { createHash, randomBytes } from "node:crypto";
import { currentTranscriptRef, resetTranscriptRef } from "./transcript";

export interface ConfirmationReceipt {
	id: string;
	action: string;
	question: string; // one-line title shown in the preview
	payloadSha: string;
	presentedAt: number;
	transcriptRef: string;
	used: boolean;
}

const RECEIPT_TTL_MS = 10 * 60_000;
const pending = new Map<string, ConfirmationReceipt>();

/** Confirmation receipts share the audit bridge's transcript tag — a session
 *  start/switch invalidates every unconsumed receipt of both kinds at once.
 *  OMP-43: the transcript/binding store is process-GLOBAL while `pending` is
 *  per module copy — a subagent lifecycle (resetShared:false) clears only its
 *  own local receipts and must never touch the owner's shared bridge. */
export function resetConfirmations(options: { resetShared?: boolean } = {}): void {
	if (options.resetShared !== false) resetTranscriptRef();
	pending.clear();
}

/** Stable stringify: object keys sorted recursively — the payload hash must not
 *  depend on the model's key ordering. confirm/confirmation_id are excluded. */
function stableStringify(value: unknown): string {
	if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "undefined";
	if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
	const entries = Object.entries(value as Record<string, unknown>)
		.filter(([, v]) => v !== undefined)
		.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
		.map(([k, v]) => `${JSON.stringify(k)}:${stableStringify(v)}`);
	return `{${entries.join(",")}}`;
}

export function payloadSha256(action: string, params: ConfirmGateParams): string {
	const { confirm: _c, confirmation_id: _i, ...rest } = params;
	return createHash("sha256").update(stableStringify({ action, params: rest }), "utf8").digest("hex");
}

/** What the two-phase gate reads out of a tool call — everything else is hashed through. */
export type ConfirmGateParams = { confirm?: unknown; confirmation_id?: unknown };

export type Confirmation =
	| { approved: true }
	| { approved: false; preview: string };

/** Two-phase write gate for model-initiated writes. Depth check happens in the
 *  host before this runs. */
export function confirmWrite(
	action: string,
	question: string,
	detail: string,
	params: ConfirmGateParams,
): Confirmation {
	const sha = payloadSha256(action, params);
	if (params.confirm === true && typeof params.confirmation_id === "string") {
		const receipt = pending.get(params.confirmation_id);
		if (!receipt) {
			return { approved: false, preview: `REFUSED — unknown or already-used confirmation_id "${params.confirmation_id}". Call without confirm to get a fresh preview.` };
		}
		if (receipt.used) {
			return { approved: false, preview: `REFUSED — confirmation_id "${params.confirmation_id}" was already consumed. Call without confirm for a fresh preview.` };
		}
		if (receipt.transcriptRef !== currentTranscriptRef()) {
			return { approved: false, preview: `REFUSED — confirmation_id "${params.confirmation_id}" belongs to another transcript. Call without confirm for a fresh preview.` };
		}
		if (Date.now() - receipt.presentedAt > RECEIPT_TTL_MS) {
			pending.delete(receipt.id);
			return { approved: false, preview: `REFUSED — confirmation_id "${params.confirmation_id}" expired. Call without confirm for a fresh preview.` };
		}
		if (receipt.payloadSha !== sha || receipt.action !== action) {
			return { approved: false, preview: `REFUSED — payload changed since the preview (confirmation_id "${params.confirmation_id}"). Show Chris the new preview: call again without confirm.` };
		}
		receipt.used = true;
		return { approved: true };
	}
	const receipt: ConfirmationReceipt = {
		id: `cf-${randomBytes(6).toString("hex")}`,
		action,
		question,
		payloadSha: sha,
		presentedAt: Date.now(),
		transcriptRef: currentTranscriptRef(),
		used: false,
	};
	pending.set(receipt.id, receipt);
	return {
		approved: false,
		preview: [
			"CONFIRM REQUIRED — nothing written.",
			"",
			question,
			detail,
			"",
			`confirmation_id: ${receipt.id}`,
			"",
			"Show this to Chris verbatim. If he says yes, call again with the same arguments plus confirm:true and this confirmation_id.",
		].join("\n"),
	};
}
