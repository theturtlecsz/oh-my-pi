import { type UUID, payloadHash } from "@oh-my-pi/pi-work-client";

/** RFC-4122-shaped deterministic id over the canonical payload hash: the same
 *  logical content mints the same id regardless of key insertion order. */
export function stableId(...parts: unknown[]): UUID {
	const hex = payloadHash(parts);
	const variant = ((parseInt(hex[16]!, 16) & 0x3) | 0x8).toString(16);
	return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

/** Stable identity for one exact planned work revision and plan body. */
export function plannedCandidateId(workId: string, revisionId: string, planSha256: string): UUID {
	return stableId("planned-candidate", workId, revisionId, planSha256);
}
