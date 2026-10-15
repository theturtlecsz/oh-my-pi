/**
 * Install, update, and remove for the selected ECC packs.
 *
 * The build plans the complete file set for the selected pack(s) first; nothing
 * is written until the plan is complete and verified. Ownership is tracked per
 * file in adapted.lock.json:
 *
 *  - install: write only files whose destination is absent. A destination that
 *    exists and is not owned by this installation is a conflict and aborts the
 *    whole install, so a user file is never overwritten.
 *  - update: re-plan against the current mirror and rewrite files this install
 *    owns. If an owned file's bytes no longer match the lock, it was modified
 *    after install and the update refuses rather than clobbering local edits.
 *  - remove: delete only files recorded as owned, then the now-empty dirs.
 *
 * Every step is idempotent: re-running install writes nothing new, and re-running
 * remove deletes nothing that is already gone.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { isInstallable, readAssetSource, readMirrorFile } from "./catalog";
import { sha256Hex } from "./hash";
import { lockAssetsById, lockFromPlan, readLock, writeLock } from "./lock";
import { loadOverlay } from "./overlay";
import { buildIndexFromManifest, transformAsset } from "./transform";
import type { AdaptedLock, InstallPlan, LockAsset, Manifest, ManifestAsset, PlannedFile } from "./types";

export class InstallError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "InstallError";
	}
}

export interface InstallResult {
	status: "installed" | "updated" | "unchanged" | "removed";
	written: string[];
	removed: string[];
	skipped: string[];
	conflicts: string[];
}

export interface AdapterOptions {
	eccRoot: string;
	/** Project root the `.omp/...` targets resolve under. */
	projectRoot: string;
	adapterVersion?: number;
}

/**
 * Where an installation records the files it owns. This is per-project install
 * state, not the build lock committed beside the mirror: it lives under the
 * installed root so two projects never share one ownership record.
 */
function installLockPath(options: AdapterOptions): string {
	return path.join(options.projectRoot, ".omp", "ecc", "adapted.lock.json");
}

/**
 * Transform every selected, installable asset into an in-memory install plan.
 * Reads the mirror, verifying each asset's upstream sha256 against the manifest
 * so a mirrored edit that nobody re-pinned is caught here.
 */
export async function buildPlan(
	options: AdapterOptions,
	manifest: Manifest,
	assets: ManifestAsset[],
): Promise<InstallPlan> {
	const index = buildIndexFromManifest(manifest.assets);

	const planned: PlannedFile[] = [];
	const lockAssets: LockAsset[] = [];

	for (const asset of assets) {
		if (!isInstallable(asset)) continue;
		const bytes = await readAssetSource(options.eccRoot, asset);
		const actualUpstream = sha256Hex(bytes);
		if (actualUpstream !== asset.upstream.sha256) {
			throw new InstallError(
				`upstream drift for '${asset.id}': manifest pins ${asset.upstream.sha256} but mirror is ${actualUpstream}`,
			);
		}

		const overlay = asset.adaptation.overlay
			? await loadOverlay(asset.adaptation.overlay, options.eccRoot, asset.id, asset.upstream.path)
			: undefined;

		// For a skill, `dependencies` are files relative to the skill directory
		// that must be installed verbatim; for a rule, they are asset ids the
		// link-rewrite index already resolves, so no helper read is needed.
		const helperFiles = new Map<string, Uint8Array>();
		if (asset.adaptation.kind === "skill-namespaced") {
			for (const dep of asset.dependencies) {
				helperFiles.set(dep, await readMirrorFile(options.eccRoot, path.posix.join(asset.upstream.path, dep)));
			}
		}

		const result = transformAsset(asset, bytes, { index, overlay, helperFiles });
		planned.push(...result.files);
		lockAssets.push({
			id: asset.id,
			upstreamPath: asset.upstream.path,
			upstreamSha256: actualUpstream,
			adaptedSha256: result.adaptedSha256,
			adaptationKind: asset.adaptation.kind,
			disposition: asset.disposition,
			files: result.files.map(f => ({ path: f.path, sha256: f.sha256, mode: f.mode })),
		});
	}

	return { files: planned, assets: lockAssets };
}

/** Resolve a planned file's absolute destination under the project root. */
function planDestination(options: AdapterOptions, file: PlannedFile): string {
	return path.join(options.projectRoot, file.path);
}

async function readIfExists(filePath: string): Promise<string | null> {
	try {
		return await Bun.file(filePath).text();
	} catch (err) {
		if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return null;
		throw err;
	}
}

/**
 * Verify a completed plan's files against the destination filesystem without
 * writing, returning the conflicts (existing files that are not owned).
 */
export async function previewPlan(
	options: AdapterOptions,
	plan: InstallPlan,
	lock: AdaptedLock | null,
): Promise<{ willWrite: string[]; conflicts: string[] }> {
	const owned = ownedPathHashes(lock);
	const willWrite: string[] = [];
	const conflicts: string[] = [];
	for (const file of plan.files) {
		const destination = planDestination(options, file);
		const existing = await readIfExists(destination);
		if (existing === null) {
			willWrite.push(file.path);
			continue;
		}
		if (owned.has(file.path)) willWrite.push(file.path);
		else conflicts.push(file.path);
	}
	return { willWrite, conflicts };
}

function ownedPathHashes(lock: AdaptedLock | null): Map<string, string> {
	const map = new Map<string, string>();
	for (const asset of lock?.assets ?? []) {
		for (const file of asset.files) map.set(file.path, file.sha256);
	}
	return map;
}

function decodeBytes(bytes: Uint8Array): string {
	return new TextDecoder("utf-8").decode(bytes);
}

async function writeFileAtomic(destination: string, bytes: Uint8Array): Promise<void> {
	// Bun.write creates parent dirs and truncates; a temp+rename keeps the
	// update atomic so an interrupted run never leaves a half-written file.
	const temp = `${destination}.ecc-tmp-${process.pid}`;
	await Bun.write(temp, bytes);
	await fs.rename(temp, destination);
}

/**
 * Remove directories left empty by deleted files, deepest first. Walks upward
 * from each directory but never removes the project root or `.omp` itself.
 */
async function pruneEmptyDirectories(options: AdapterOptions, directories: Iterable<string>): Promise<void> {
	const omp = path.join(options.projectRoot, ".omp");
	const prunable = (p: string): boolean => {
		const rel = path.relative(options.projectRoot, p);
		return rel !== "" && !rel.startsWith("..") && !path.isAbsolute(rel) && p !== omp;
	};
	for (const dir of [...directories].sort((a, b) => b.length - a.length)) {
		let current = dir;
		while (prunable(current)) {
			const entries = await fs.readdir(current).catch(() => ["x"]);
			if (entries.length > 0) break;
			await fs.rmdir(current).catch(() => undefined);
			current = path.dirname(current);
		}
	}
}

/**
 * Delete files of retired assets (lock assets whose id left the manifest) that
 * are not part of the new plan, but only when the on-disk bytes still match the
 * old lock. Modified or already-missing files are kept and reported as skipped.
 */
async function removeRetiredFiles(
	options: AdapterOptions,
	manifest: Manifest,
	existing: AdaptedLock,
	plan: InstallPlan,
): Promise<{ removed: string[]; skipped: string[] }> {
	const declared = new Set(manifest.assets.map(asset => asset.id));
	const retired = existing.assets.filter(asset => !declared.has(asset.id));
	const protectedPaths = new Set(plan.files.map(file => file.path));
	for (const asset of existing.assets) {
		if (declared.has(asset.id)) for (const file of asset.files) protectedPaths.add(file.path);
	}
	const removed: string[] = [];
	const skipped: string[] = [];
	const directories = new Set<string>();
	for (const asset of retired) {
		for (const file of asset.files) {
			if (protectedPaths.has(file.path)) continue;
			const destination = path.join(options.projectRoot, file.path);
			const onDisk = await readIfExists(destination);
			if (onDisk === null || sha256Hex(onDisk) !== file.sha256) {
				skipped.push(file.path);
				continue;
			}
			await fs.rm(destination);
			removed.push(file.path);
			directories.add(path.dirname(destination));
		}
	}
	await pruneEmptyDirectories(options, directories);
	return { removed, skipped };
}

/** Install the plan, refusing on any unowned destination. */
export function install(
	options: AdapterOptions,
	manifest: Manifest,
	assets: ManifestAsset[],
): Promise<InstallResult> {
	return applyPlan(options, manifest, assets, false);
}

async function applyPlan(
	options: AdapterOptions,
	manifest: Manifest,
	assets: ManifestAsset[],
	retire: boolean,
): Promise<InstallResult> {
	const lockPath = installLockPath(options);
	const existing = await readLock(lockPath);
	const plan = await buildPlan(options, manifest, assets);
	const { conflicts } = await previewPlan(options, plan, existing);
	if (conflicts.length > 0) {
		throw new InstallError(`refusing to overwrite unowned files: ${conflicts.join(", ")}`);
	}

	const owned = ownedPathHashes(existing);
	const written: string[] = [];
	const skipped: string[] = [];
	for (const file of plan.files) {
		const destination = planDestination(options, file);
		const existingBytes = await readIfExists(destination);
		// An owned file may only be rewritten when its on-disk bytes still match
		// what this install last wrote. Compare against the lock, not the new
		// plan, so a local edit is refused rather than overwritten.
		if (owned.has(file.path) && existingBytes !== null) {
			if (sha256Hex(existingBytes) !== owned.get(file.path)) {
				throw new InstallError(`refusing to overwrite modified owned file '${file.path}'`);
			}
		}
		if (existingBytes === decodeBytes(file.bytes)) {
			skipped.push(file.path);
			continue;
		}
		await writeFileAtomic(destination, file.bytes);
		written.push(file.path);
	}

	const next = lockFromPlan(plan, manifest.upstream, options.adapterVersion ?? manifest.transformVersion);
	// Selection controls writes, not removal: retain omitted assets and files
	// with their last installed hashes until explicit remove releases ownership.
	const assetsById = existing ? lockAssetsById(existing) : new Map<string, LockAsset>();
	let removed: string[] = [];
	if (retire && existing) {
		// Retired assets left the manifest: release their ownership and delete
		// the files that are still pristine.
		const retired = await removeRetiredFiles(options, manifest, existing, plan);
		removed = retired.removed;
		skipped.push(...retired.skipped);
		const declared = new Set(manifest.assets.map(asset => asset.id));
		for (const id of [...assetsById.keys()]) if (!declared.has(id)) assetsById.delete(id);
	}
	for (const asset of next.assets) {
		const filesByPath = new Map(assetsById.get(asset.id)?.files.map(file => [file.path, file]));
		for (const file of asset.files) filesByPath.set(file.path, file);
		assetsById.set(asset.id, { ...asset, files: [...filesByPath.values()] });
	}
	next.assets = [...assetsById.values()];
	await writeLock(lockPath, next);
	return {
		status: written.length === 0 && removed.length === 0 ? "unchanged" : existing ? "updated" : "installed",
		written,
		removed,
		skipped,
		conflicts: [],
	};
}

/**
 * Update an existing installation like {@link install}, and also retire lock
 * assets that left the manifest. The explicit
 * entry point exists so callers must intend an update, and so a missing lock is
 * reported rather than silently treated as a fresh install.
 */
export async function update(
	options: AdapterOptions,
	manifest: Manifest,
	assets: ManifestAsset[],
): Promise<InstallResult> {
	const lock = await readLock(installLockPath(options));
	if (!lock) throw new InstallError("no adapted.lock.json to update; run install first");
	return applyPlan(options, manifest, assets, true);
}

/** Remove every file recorded in the lock, then prune directories it emptied. */
export async function remove(options: AdapterOptions): Promise<InstallResult> {
	const lockPath = installLockPath(options);
	const lock = await readLock(lockPath);
	if (!lock) return { status: "removed", written: [], removed: [], skipped: [], conflicts: [] };

	const removed: string[] = [];
	const skipped: string[] = [];
	const directories = new Set<string>();
	for (const asset of lock.assets) {
		for (const file of asset.files) {
			const destination = path.join(options.projectRoot, file.path);
			const existing = await readIfExists(destination);
			if (existing === null) {
				skipped.push(file.path);
				continue;
			}
			if (sha256Hex(existing) !== file.sha256) {
				throw new InstallError(`refusing to remove modified file '${file.path}'`);
			}
			await fs.rm(destination);
			removed.push(file.path);
			directories.add(path.dirname(destination));
		}
	}
	await fs.rm(lockPath, { force: true });
	directories.add(path.dirname(lockPath));
	await pruneEmptyDirectories(options, directories);
	return { status: "removed", written: [], removed, skipped, conflicts: [] };
}
