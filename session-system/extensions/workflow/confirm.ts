/**
 * workflow/confirm.ts — transcript-bound confirmation receipts (HOME-147, OMP-168).
 *
 * Replaces bare `confirm:true` replays for model-initiated writes. First call
 * writes NOTHING: the host issues a receipt {id, action, question, detail, payloadSha,
 * transcriptRef, presentedAt} and returns the preview text naming the id. The
 * repeat call must carry confirm:true plus the confirmation_id; the host
 * validates it (exists, unconsumed, fresh, same transcript, identical payload)
 * and consumes it before executing. Anything else is refused and writes nothing.
 *
 * When an unconsumed receipt is foreign to the current transcript or expired,
 * it is retired and a fresh full CONFIRM REQUIRED preview with a new ID is
 * returned in that same tool result (OMP-168).
 *
 * Slash commands keep the ctx.ui.confirm modal — they are owner-entered already.
 */
import { createHash, randomBytes } from "node:crypto";
import { currentTranscriptRef, resetTranscriptRef } from "./transcript";

export interface ConfirmationReceipt {
	id: string;
	action: string;
	question: string; // one-line title shown in the preview
	detail: string;
	payloadSha: string;
	presentedAt: number;
	transcriptRef: string;
	used: boolean;
	isSubagent?: boolean;
}

export const RECEIPT_TTL_MS = 60 * 60_000;
const pending = new Map<string, ConfirmationReceipt>();

function pruneReceipts(now = Date.now()): void {
	for (const [id, receipt] of pending.entries()) {
		if (receipt.used || now - receipt.presentedAt > RECEIPT_TTL_MS) {
			pending.delete(id);
		}
	}
}

/**
 * Confirmation receipts share the transcript tag — owner session start/switch
 * rotates the transcript reference while retaining unconsumed, unexpired receipts
 * until their TTL (OMP-168).
 * OMP-43 / OMP-168: a subagent lifecycle (resetShared:false) clears only its own
 * local receipts and never touches the owner's unconsumed receipts.
 */
export function resetConfirmations(options: { resetShared?: boolean } = {}): void {
	if (options.resetShared !== false) {
		resetTranscriptRef();
		pruneReceipts();
	} else {
		for (const [id, receipt] of pending.entries()) {
			if (receipt.isSubagent) {
				pending.delete(id);
			}
		}
	}
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

function mintPreview(
	action: string,
	question: string,
	detail: string,
	sha: string,
	options: { isSubagent?: boolean } = {},
): Confirmation {
	pruneReceipts();
	const receipt: ConfirmationReceipt = {
		id: `cf-${randomBytes(6).toString("hex")}`,
		action,
		question,
		detail,
		payloadSha: sha,
		presentedAt: Date.now(),
		transcriptRef: currentTranscriptRef(),
		used: false,
		isSubagent: options.isSubagent ?? false,
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

/** Two-phase write gate for model-initiated writes. Depth check happens in the
 *  host before this runs. */
export function confirmWrite(
	action: string,
	question: string,
	detail: string,
	params: ConfirmGateParams,
	options: { isSubagent?: boolean } = {},
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
		if (receipt.payloadSha !== sha || receipt.action !== action) {
			return { approved: false, preview: `REFUSED — payload changed since the preview (confirmation_id "${params.confirmation_id}"). Show Chris the new preview: call again without confirm.` };
		}
		if (receipt.transcriptRef !== currentTranscriptRef()) {
			pending.delete(receipt.id);
			return mintPreview(action, question, detail, sha, options);
		}
		if (Date.now() - receipt.presentedAt > RECEIPT_TTL_MS) {
			pending.delete(receipt.id);
			return mintPreview(action, question, detail, sha, options);
		}
		receipt.used = true;
		return { approved: true };
	}
	return mintPreview(action, question, detail, sha, options);
}
