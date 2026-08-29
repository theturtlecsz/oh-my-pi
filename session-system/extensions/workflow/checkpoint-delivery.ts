/**
 * workflow/checkpoint-delivery.ts — ONE delivery path for close-attempt
 * checkpoints (OMP-51), shared by the workflow host and model-bookends.
 *
 * Deliver the EXACT server-rendered text through the receipt-backed
 * ExtensionAPI.deliverMessage (resolves only after real injection), then
 * attest the outcome with attest_checkpoint_delivery. A failed delivery is
 * recorded as `failed`, keeps closeout blocked, and names its two recovery
 * paths: retry at next owner session start, or an owner waiver.
 *
 * Tool handlers must never await deliverMessage() directly because deliverMessage
 * only resolves after turn yield (OMP-97). Tool handlers queue delivery and return
 * immediately with the server-rendered event.
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

export interface QueuedDeliveryResult {
	queuedCount: number;
	events: CloseEventView[];
}

// In-flight delivery tracking: eventId -> Promise<"delivered" | "failed">
const inFlightDeliveries = new Map<string, Promise<"delivered" | "failed">>();

function deliverOneEvent(
	pi: ExtensionAPI,
	backend: WorkflowBackend,
	event: CloseEventView,
	onNotice?: (notice: string) => void,
): Promise<"delivered" | "failed"> {
	const existing = inFlightDeliveries.get(event.eventId);
	if (existing) return existing;

	const { promise: slot, resolve: resolveSlot } = Promise.withResolvers<"delivered" | "failed">();
	inFlightDeliveries.set(event.eventId, slot);
	(async () => {
		let status: "delivered" | "failed" = "delivered";
		try {
			await pi.deliverMessage({ customType: "close-attempt-checkpoint", content: event.renderedText });
		} catch {
			status = "failed";
		}
		try {
			const attested = await backend.attestDelivery(event.eventId, pi.getSessionId(), event.renderedSha256, status);
			if (attested.status === "refused" && attested.event.reasonCode !== "delivery_already_resolved") {
				onNotice?.(`checkpoint "${event.eventType}" attestation refused (${attested.event.reasonCode}: ${attested.event.renderedText})`);
			}
		} catch (error) {
			onNotice?.(`checkpoint "${event.eventType}" attestation failed (${String(error)}) — it stays pending and closeout stays blocked`);
		}
		return status;
	})()
		.then(res => {
			if (inFlightDeliveries.get(event.eventId) === slot) {
				inFlightDeliveries.delete(event.eventId);
			}
			resolveSlot(res);
		})
		.catch(() => {
			if (inFlightDeliveries.get(event.eventId) === slot) {
				inFlightDeliveries.delete(event.eventId);
			}
			resolveSlot("failed");
		});

	return slot;
}

/** Queue delivery for one event in the background without awaiting turn yield (for tool handlers). */
export function queueCheckpointDelivery(
	pi: ExtensionAPI,
	backend: WorkflowBackend,
	event: CloseEventView,
	onNotice?: (notice: string) => void,
): void {
	void deliverOneEvent(pi, backend, event, onNotice);
}

/** Queue delivery for all unresolved requires_delivery events for a work item without awaiting turn yield (for tool handlers). */
export async function queuePendingCheckpointDeliveries(
	pi: ExtensionAPI,
	backend: WorkflowBackend,
	key: string,
	onNotice?: (notice: string) => void,
): Promise<QueuedDeliveryResult> {
	const result: QueuedDeliveryResult = { queuedCount: 0, events: [] };
	let pending: CloseEventView[];
	try {
		pending = await backend.pendingDeliveries(key);
	} catch (error) {
		onNotice?.(`checkpoint deliveries unreadable for ${key} (${String(error)}) — closeout stays blocked until they resolve`);
		return result;
	}
	const toDeliver = pending.slice(0, MAX_DELIVERIES_PER_PASS);
	for (const event of toDeliver) {
		queueCheckpointDelivery(pi, backend, event, onNotice);
	}
	result.queuedCount = toDeliver.length;
	result.events = toDeliver;
	if (pending.length > MAX_DELIVERIES_PER_PASS) {
		onNotice?.(`${pending.length - MAX_DELIVERIES_PER_PASS} more checkpoint(s) still pending — they retry at the next owner session start`);
	}
	return result;
}

/** Deliver one event's exact rendered text and attest the outcome. A refused
 *  attestation (other than the idempotent "already resolved") throws with the
 *  service's exact rendered reason — the checkpoint is NOT settled. (For idle lifecycle contexts). */
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

/** Deliver every unresolved requires_delivery event for one work item (bounded). (For idle lifecycle contexts). */
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
			const status = await deliverCheckpoint(pi, backend, event);
			if (status === "delivered") {
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
