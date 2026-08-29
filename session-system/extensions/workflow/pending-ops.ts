/**
 * workflow/pending-ops.ts — durable pending-operation claims (HOME-147, plan §3).
 *
 * Every WorkService mutation persists its exact command envelope BEFORE
 * transport, one file per logical intent, created with O_EXCL ("wx"):
 *
 *   ~/.config/omp-work/pending-operations/<intent>.json
 *
 * Claim states:
 * - pending  { envelope }                — POST attempted or about to be; the
 *   outcome is unknown. Recovery resends the stored bytes verbatim (the
 *   service applies or replays by operation id). NEVER deleted in this state:
 *   a 404 between timeout and server-side commit is not proof of absence.
 * - resolved { envelope, result, … }     — the service returned a terminal
 *   result. Recovery returns the stored result WITHOUT re-sending. Kept until
 *   the host acks delivery (any later tool call or session start proves the
 *   tool result reached the transcript) — an unlink right after execute would
 *   reopen the duplicate window on a crash before delivery.
 * - dropped                              — acked (ackResolvedOps), or released
 *   after a definitive service refusal (status > 0 means the command was seen
 *   and did not apply).
 *
 * The exclusive create arbitrates cross-session races: two concurrent OMP
 *   sessions attempting the same intent produce one winner; the loser reads
 *   the winner's file and continues with ITS envelope — no second operation.
 */
import { chmod, mkdir, open, readdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { payloadHash } from "@oh-my-pi/pi-work-client";

/** Canonical-json sha256 of the intent parts — stable across sessions for the
 *  same logical action regardless of key insertion order (a reordered rebuild
 *  after a code update must still find its persisted claim). */
export function intentFingerprint(...parts: unknown[]): string {
	return payloadHash(parts);
}

/** Claim files hold full mutation payloads/results — 0700 dir, 0600 files. */
const ensuredDirs = new Set<string>();
async function ensureDir(dir: string): Promise<void> {
	if (ensuredDirs.has(dir)) return;
	await mkdir(dir, { recursive: true, mode: 0o700 });
	await chmod(dir, 0o700).catch(() => {}); // pre-existing dir from a looser umask
	ensuredDirs.add(dir);
}

export interface PendingRecord {
	envelope: unknown;
	result?: unknown;
	resolved_at?: string;
}

export interface PendingClaim {
	/** Absolute path of the claim file. */
	path: string;
	/** true when THIS caller created the claim. */
	owner: boolean;
	/** The claim contents — null when the file stays unreadable (caught
	 *  mid-flush or a crash between create and write). Callers fail closed and
	 *  never delete it; repair is manual. */
	record: PendingRecord | null;
}

async function readRecord(path: string): Promise<PendingRecord | null> {
	// The winner writes immediately after creation; a reader can catch the
	// file mid-flush. Retry briefly, then give up (fail closed at the caller).
	for (let attempt = 0; attempt < 10; attempt++) {
		try {
			return JSON.parse(await readFile(path, "utf8")) as PendingRecord;
		} catch {
			await Bun.sleep(50);
		}
	}
	return null;
}

/** Claim an intent, creating the file with `make()`'s envelope when absent. */
export async function claimPendingOp(dir: string, intent: string, make: () => unknown): Promise<PendingClaim> {
	await ensureDir(dir);
	const path = join(dir, `${intent}.json`);
	let handle;
	try {
		handle = await open(path, "wx", 0o600);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
		return { path, owner: false, record: await readRecord(path) };
	}
	try {
		const record: PendingRecord = { envelope: make() };
		await handle.writeFile(JSON.stringify(record));
		return { path, owner: true, record };
	} finally {
		await handle.close();
	}
}

/** Terminal result observed — persist it alongside the envelope (atomic
 *  tmp+rename) so post-crash recovery returns it without re-sending. */
export async function resolvePendingOp(path: string, record: PendingRecord, result: unknown): Promise<void> {
	const resolved: PendingRecord = { ...record, result, resolved_at: new Date().toISOString() };
	const tmp = `${path}.tmp`;
	await writeFile(tmp, JSON.stringify(resolved), { mode: 0o600 });
	await rename(tmp, path);
}

/** Release a claim: delivery acked (resolved) or definitive refusal (pending).
 *  Missing file is fine — a recovering peer may already have consumed it. */
export async function dropPendingOp(path: string): Promise<void> {
	await rm(path, { force: true });
}

/** Resolved claims past this age are housekeeping-swept even without a
 *  recorded delivery — the crash-recovery window is minutes, not days. */
const RESOLVED_TTL_MS = 24 * 60 * 60_000;

/** Delivery ack: housekeep resolved claims. A delivered create/revise/state/evidence
 *  claim remains readable until RESOLVED_TTL_MS expires so an expired confirmation
 *  or lost response reuses the prior operation instead of creating a duplicate.
 *  Resolved record_project_health claims are removed on the next session sweep
 *  because that command is an idempotent current projection and a later
 *  same-status recording must update its timestamp. Pending or unreadable claims
 *  are NEVER swept — only a terminal result or a definitive service refusal
 *  releases one. */
export async function ackOps(dir: string, delivered: ReadonlySet<string>, now = Date.now()): Promise<void> {
	let names: string[];
	try {
		names = await readdir(dir);
	} catch {
		return; // no claims directory yet
	}
	for (const name of names) {
		if (!name.endsWith(".json")) continue;
		const path = join(dir, name);
		const record = await readRecord(path);
		if (!record || record.result === undefined) continue; // pending/unreadable — keep
		const env = record.envelope;
		const commandType =
			env && typeof env === "object" && "command" in env && env.command && typeof env.command === "object" && "type" in env.command
				? env.command.type
				: undefined;
		if (commandType === "record_project_health") {
			await rm(path, { force: true });
			continue;
		}
		const age = now - Date.parse(record.resolved_at ?? "");
		if (Number.isFinite(age) && age > RESOLVED_TTL_MS) await rm(path, { force: true });
	}
}

export async function readPendingClaims(dir: string): Promise<{ records: PendingRecord[]; unreadable: string[] }> {
	await ensureDir(dir);
	const names = await readdir(dir);
	const records: PendingRecord[] = [];
	const unreadable: string[] = [];
	for (const name of names) {
		if (!name.endsWith(".json") || name.endsWith(".tmp")) continue;
		const fullPath = join(dir, name);
		const record = await readRecord(fullPath);
		if (record) {
			records.push(record);
		} else {
			unreadable.push(fullPath);
		}
	}
	return { records, unreadable };
}
