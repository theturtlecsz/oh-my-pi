/**
 * workflow/checkpoint-delivery.ts — ONE delivery path for close-attempt
 * checkpoints (OMP-51), shared by the workflow host and model-bookends.
 *
 * Deliver the EXACT server-rendered text through the receipt-backed
 * ExtensionAPI.deliverMessage (resolves only after real injection), then
 * attest the outcome with attest_checkpoint_delivery. A failed delivery is
 * recorded as `failed`, keeps closeout blocked, and names its two recovery
 * paths: retry at next owner session start, or an owner waiver.
 */
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import type { CloseEventView, WorkflowBackend } from "./backend";

/** Bound per call: session-start recovery must never wedge on a runaway backlog. */
const MAX_DELIVERIES_PER_PASS = 12;

export interface DeliveryPassResult {
	delivered: number;
	failed: number;
	notices: string[];
}

/** Deliver one event's exact rendered text and attest the outcome. A refused
 *  attestation (other than the idempotent "already resolved") throws with the
 *  service's exact rendered reason — the checkpoint is NOT settled. */
export async function deliverCheckpoint(pi: ExtensionAPI, backend: WorkflowBackend, event: CloseEventView): Promise<"delivered" | "failed"> {
	let status: "delivered" | "failed" = "delivered";
	try {
		await pi.deliverMessage({ customType: "close-attempt-checkpoint", content: event.renderedText });
	} catch {
		status = "failed";
	}
	const attested = await backend.attestDelivery(event.eventId, pi.getSessionId(), event.renderedSha256, status);
	if (attested.status === "refused" && attested.event.reasonCode !== "delivery_already_resolved") {
		throw new Error(attested.event.renderedText);
	}
	return status;
}

/** Deliver every unresolved requires_delivery event for one work item (bounded). */
export async function deliverPendingCheckpoints(pi: ExtensionAPI, backend: WorkflowBackend, key: string): Promise<DeliveryPassResult> {
	const result: DeliveryPassResult = { delivered: 0, failed: 0, notices: [] };
	let pending: CloseEventView[];
	try {
		pending = await backend.pendingDeliveries(key);
	} catch (error) {
		result.notices.push(`checkpoint deliveries unreadable for ${key} (${String(error)}) — closeout stays blocked until they resolve`);
		return result;
	}
	for (const event of pending.slice(0, MAX_DELIVERIES_PER_PASS)) {
		try {
			if ((await deliverCheckpoint(pi, backend, event)) === "delivered") {
				result.delivered += 1;
			} else {
				result.failed += 1;
				result.notices.push(`checkpoint "${event.eventType}" could not be delivered — recorded failed; retry at next owner session start, or owner waiver via work action:"waive_delivery"`);
			}
		} catch (error) {
			result.failed += 1;
			result.notices.push(`checkpoint "${event.eventType}" attestation failed (${String(error)}) — it stays pending and closeout stays blocked`);
		}
	}
	if (pending.length > MAX_DELIVERIES_PER_PASS) {
		result.notices.push(`${pending.length - MAX_DELIVERIES_PER_PASS} more checkpoint(s) still pending — they retry at the next owner session start`);
	}
	return result;
}
