/**
 * adapted.lock.json serialization.
 *
 * The lock records, per installed asset, the upstream sha256 it was built from
 * and the adapted sha256 actually written, plus every owned file. It is the
 * durable record a later update verifies against: if the mirror upstream bytes
 * drifted, the transform's computed upstream hash will differ from the lock and
 * verification fails before any file is overwritten.
 */
import * as path from "node:path";
import type { AdaptedLock, InstallPlan, LockAsset } from "./types";

export type { AdaptedLock } from "./types";

export function lockFromPlan(
	plan: InstallPlan,
	upstream: { commit: string; tree: string },
	transformVersion: number,
): AdaptedLock {
	return {
		schemaVersion: 1,
		transformVersion,
		upstream,
		assets: plan.assets,
	};
}

export function serializeLock(lock: AdaptedLock): string {
	return `${JSON.stringify(lock, null, 2)}\n`;
}

export async function readLock(lockPath: string): Promise<AdaptedLock | null> {
	try {
		return JSON.parse(await Bun.file(lockPath).text()) as AdaptedLock;
	} catch (err) {
		if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return null;
		throw err;
	}
}

export async function writeLock(lockPath: string, lock: AdaptedLock): Promise<void> {
	await Bun.write(lockPath, serializeLock(lock));
}

/** Index a lock's assets by id for verification. */
export function lockAssetsById(lock: AdaptedLock): Map<string, LockAsset> {
	return new Map(lock.assets.map(asset => [asset.id, asset]));
}

export function lockPathFor(eccRoot: string): string {
	return path.join(eccRoot, "adapted.lock.json");
}
