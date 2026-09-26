/**
 * Content hashing shared by the adapter. One implementation so the upstream and
 * adapted digests recorded in the manifest and lock cannot diverge between the
 * transform and the overlay drift check.
 */
import * as crypto from "node:crypto";

export function sha256Hex(data: Uint8Array | string): string {
	const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data;
	return crypto.createHash("sha256").update(bytes).digest("hex");
}
