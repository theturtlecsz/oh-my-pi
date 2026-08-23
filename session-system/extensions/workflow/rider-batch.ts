/**
 * OMP-93 rider batch staging (owner ruling 2026-08-22, decision 0006)
 * and OMP-111 cancellation batch staging (owner ruling 2026-08-23).
 *
 * A staged batch is untrusted input to a close-authority path, so every read
 * is descriptor-bound: O_NOFOLLOW refuses symlinks at open, fstat validates
 * the exact opened inode, and consumption re-verifies that same dev/ino
 * identity before renaming — a pathname swap between approval and consume is
 * refused, never silently consumed.
 */
import { createHash } from "node:crypto";
import { closeSync, constants, fstatSync, openSync, readFileSync, realpathSync, renameSync, type Stats } from "node:fs";
import { join } from "node:path";

export const RIDER_BATCH_MAX_BYTES = 256 * 1024;
export const CANCEL_BATCH_MAX_BYTES = 256 * 1024;

export type RiderBatchEntry = { key: string; evidence: string };
export type CancelBatchEntry = { key: string; reason: string };

export type StagedRiderBatch = {
	path: string;
	entries: RiderBatchEntry[];
	/** SHA-256 of the exact staged bytes — what the owner confirmation binds. */
	digest: string;
	/** Inode identity of the validated file; consumption requires it unchanged. */
	dev: number;
	ino: number;
};

export type StagedCancelBatch = {
	path: string;
	entries: CancelBatchEntry[];
	/** SHA-256 of the exact staged bytes — what the owner confirmation binds. */
	digest: string;
	/** Inode identity of the validated file; consumption requires it unchanged. */
	dev: number;
	ino: number;
};

/** Thrown for every unsafe or malformed batch; ENOENT is NOT an error — read returns null. */
export class RiderBatchError extends Error {}
export class CancelBatchError extends Error {}

export function readStagedRiderBatch(path: string): StagedRiderBatch | null {
	let fd: number;
	try {
		// O_NOFOLLOW: a symlink at the final component fails the open itself
		// (ELOOP) — fstat on a followed target would validate the wrong file.
		fd = openBatch(path);
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		if (code === "ENOENT") return null; // no staged batch — the normal case
		if (code === "ELOOP") throw new RiderBatchError("staged batch is a symlink — it must be a regular file");
		throw new RiderBatchError(`staged batch unreadable (${String(error)})`);
	}
	let raw: string;
	let dev: number;
	let ino: number;
	try {
		const stat = fstatSync(fd);
		if (!stat.isFile()) throw new RiderBatchError("staged batch must be a regular file");
		if (process.getuid && stat.uid !== process.getuid()) throw new RiderBatchError("staged batch is not owned by the current user");
		if ((stat.mode & 0o777) !== 0o600) throw new RiderBatchError("staged batch must be mode 0600 exactly");
		if (stat.size > RIDER_BATCH_MAX_BYTES) throw new RiderBatchError(`staged batch exceeds ${RIDER_BATCH_MAX_BYTES} bytes`);
		dev = stat.dev;
		ino = stat.ino;
		raw = readFileSync(fd, "utf8");
	} finally {
		closeSync(fd);
	}
	let entries: unknown;
	try {
		entries = JSON.parse(raw);
	} catch {
		throw new RiderBatchError("staged batch is not valid JSON");
	}
	if (!Array.isArray(entries) || entries.length === 0) throw new RiderBatchError("batch must be a non-empty array of {key, evidence}");
	for (const entry of entries as RiderBatchEntry[]) {
		if (typeof entry?.key !== "string" || !entry.key.trim() || typeof entry.evidence !== "string" || !entry.evidence.trim()) {
			throw new RiderBatchError(`batch entry for ${(entry as RiderBatchEntry)?.key ?? "(missing key)"} needs a key and non-empty evidence`);
		}
	}
	return {
		path,
		entries: entries as RiderBatchEntry[],
		digest: createHash("sha256").update(raw).digest("hex"),
		dev,
		ino,
	};
}

/**
 * One-shot archive of the exact approved bytes. The rename happens first (a
 * pathname pre-check would just reopen the swap window); the ARCHIVED file is
 * then verified by descriptor — same dev/ino as approved AND the same
 * SHA-256 — so a swap or same-inode rewrite between approval and archive is
 * detected, never reported as success. Throws RiderBatchError on any
 * mismatch or rename failure — callers decide whether that is fail-closed
 * (decline) or a non-rollback warning (the service already applied the
 * attempt; what was SEALED came from the approved in-memory bytes either way).
 */
export function consumeStagedRiderBatch(batch: StagedRiderBatch, suffix: "consumed" | "declined"): string {
	const target = `${batch.path}.${suffix}-${Date.now()}`;
	try {
		renameSync(batch.path, target);
	} catch (error) {
		throw new RiderBatchError(`staged batch vanished before archive (${String(error)})`);
	}
	let fd: number;
	try {
		fd = openBatch(target);
	} catch (error) {
		throw new RiderBatchError(`archived batch unreadable after rename (${String(error)})`);
	}
	let stat: Stats;
	let raw: string;
	try {
		stat = fstatSync(fd);
		raw = readFileSync(fd, "utf8");
	} finally {
		closeSync(fd);
	}
	if (stat.dev !== batch.dev || stat.ino !== batch.ino || createHash("sha256").update(raw).digest("hex") !== batch.digest) {
		throw new RiderBatchError(`archived file at ${target} is NOT the approved batch (swapped or rewritten after approval) — inspect it before the next /summary`);
	}
	return target;
}

export function readStagedCancelBatch(path: string): StagedCancelBatch | null {
	let fd: number;
	try {
		fd = openBatch(path);
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		if (code === "ENOENT") return null;
		if (code === "ELOOP") throw new CancelBatchError("staged cancel batch is a symlink — it must be a regular file");
		throw new CancelBatchError(`staged cancel batch unreadable (${String(error)})`);
	}
	let raw: string;
	let dev: number;
	let ino: number;
	try {
		const stat = fstatSync(fd);
		if (!stat.isFile()) throw new CancelBatchError("staged cancel batch must be a regular file");
		if (process.getuid && stat.uid !== process.getuid()) throw new CancelBatchError("staged cancel batch is not owned by the current user");
		if ((stat.mode & 0o777) !== 0o600) throw new CancelBatchError("staged cancel batch must be mode 0600 exactly");
		if (stat.size > CANCEL_BATCH_MAX_BYTES) throw new CancelBatchError(`staged cancel batch exceeds ${CANCEL_BATCH_MAX_BYTES} bytes`);
		dev = stat.dev;
		ino = stat.ino;
		raw = readFileSync(fd, "utf8");
	} finally {
		closeSync(fd);
	}
	let entries: unknown;
	try {
		entries = JSON.parse(raw);
	} catch {
		throw new CancelBatchError("staged cancel batch is not valid JSON");
	}
	if (!Array.isArray(entries) || entries.length === 0) throw new CancelBatchError("cancel batch must be a non-empty array of {key, reason}");
	if (entries.length > 128) throw new CancelBatchError("cancel batch exceeds maximum 128 entries");
	const seenKeys = new Set<string>();
	for (const entry of entries as CancelBatchEntry[]) {
		if (typeof entry?.key !== "string" || !entry.key.trim() || typeof entry?.reason !== "string" || !entry.reason.trim()) {
			throw new CancelBatchError(`cancel batch entry for ${(entry as CancelBatchEntry)?.key ?? "(missing key)"} needs a key and non-empty reason`);
		}
		if (new TextEncoder().encode(entry.reason).length > 4096) {
			throw new CancelBatchError(`cancel batch entry for ${entry.key} reason exceeds 4096 UTF-8 bytes`);
		}
		if (entry.reason.includes("\0")) {
			throw new CancelBatchError(`cancel batch entry for ${entry.key} reason contains NUL`);
		}
		if (seenKeys.has(entry.key)) {
			throw new CancelBatchError(`duplicate key "${entry.key}" in staged cancel batch`);
		}
		seenKeys.add(entry.key);
	}
	return {
		path,
		entries: entries as CancelBatchEntry[],
		digest: createHash("sha256").update(raw).digest("hex"),
		dev,
		ino,
	};
}

export function consumeStagedCancelBatch(batch: StagedCancelBatch, suffix: "consumed" | "declined"): string {
	const target = `${batch.path}.${suffix}-${Date.now()}`;
	try {
		renameSync(batch.path, target);
	} catch (error) {
		throw new CancelBatchError(`staged cancel batch vanished before archive (${String(error)})`);
	}
	let fd: number;
	try {
		fd = openBatch(target);
	} catch (error) {
		throw new CancelBatchError(`archived cancel batch unreadable after rename (${String(error)})`);
	}
	let stat: Stats;
	let raw: string;
	try {
		stat = fstatSync(fd);
		raw = readFileSync(fd, "utf8");
	} finally {
		closeSync(fd);
	}
	if (stat.dev !== batch.dev || stat.ino !== batch.ino || createHash("sha256").update(raw).digest("hex") !== batch.digest) {
		throw new CancelBatchError(`archived file at ${target} is NOT the approved cancel batch (swapped or rewritten after approval) — inspect it before the next /done`);
	}
	return target;
}

export function cancelBatchPath(agentDir: string, cwd: string): string {
	const canonical = realpathSync(cwd);
	const cwdHash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
	return join(agentDir, "work-cancel-batches", `${cwdHash}.json`);
}

export function riderBatchPath(agentDir: string, cwd: string): string {
	const canonical = realpathSync(cwd);
	const cwdHash = createHash("sha256").update(canonical).digest("hex").slice(0, 16);
	return join(agentDir, "work-rider-batches", `${cwdHash}.json`);
}

function openBatch(path: string): number {
	return openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
}
