import type { CustomMessage } from "./messages";

/** Yield-queue kind for receipt-backed extension deliveries (OMP-51). */
export const EXTENSION_DELIVERY_MESSAGE_TYPE = "extension-delivery";

/** One extension-authored message awaiting receipt-backed injection. */
export interface ExtensionDeliveryEntry {
	customType: string;
	content: string;
	display?: boolean;
	details?: unknown;
	/** Session id stamped at enqueue — a flush into any other session is stale. */
	owner?: string;
}
export function buildExtensionDeliveryBatchMessage(entries: ExtensionDeliveryEntry[]): CustomMessage {
	return {
		role: "custom",
		customType: EXTENSION_DELIVERY_MESSAGE_TYPE,
		content: entries.map(entry => entry.content).join("\n\n"),
		display: entries.some(entry => entry.display !== false),
		attribution: "agent",
		details: { deliveries: entries.map(entry => ({ customType: entry.customType, details: entry.details })) },
		timestamp: Date.now(),
	};
}
