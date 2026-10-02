/**
 * Build-lock construction and verification.
 *
 * The committed adapted.lock.json pins, besides the per-asset hashes, the
 * sha256 of the manifest.json bytes it was built from and — when the update was
 * owner-reviewed — the review file's path and content hash. Verification
 * recomputes the manifest pin and re-reads the review, so a hand edit to either
 * the manifest or the reviewed record fails before the change can be trusted.
 */
import * as path from "node:path";
import { buildPlan } from "./apply";
import { ECC_ROOT, installablePackedAssets, loadManifest } from "./catalog";
import { sha256Hex } from "./hash";
import { lockFromPlan, lockPathFor, serializeLock } from "./lock";
import { validateReview } from "./review";
import type { BuildLock, UpdateReview } from "./types";

export type { BuildLock } from "./types";

export class PinError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "PinError";
	}
}

/** Manifest path and bytes as the lock pins them. */
async function readManifestBytes(eccRoot: string): Promise<Uint8Array> {
	return new Uint8Array(await Bun.file(path.join(eccRoot, "manifest.json")).arrayBuffer());
}

/**
 * Build the lock from the pinned mirror: the adapted plan, the manifest-bytes
 * pin, and the review pin (null when the update carries no owner review).
 */
export async function buildLock(eccRoot: string, review: { path: string; sha256: string } | null): Promise<BuildLock> {
	const manifest = await loadManifest(eccRoot);
	const assets = installablePackedAssets(manifest);
	const plan = await buildPlan({ eccRoot, projectRoot: eccRoot }, manifest, assets);
	const base = lockFromPlan(plan, manifest.upstream, manifest.transformVersion);
	return {
		...base,
		manifestSha256: sha256Hex(await readManifestBytes(eccRoot)),
		review,
	};
}

export async function writeBuildLock(
	eccRoot: string,
	review: { path: string; sha256: string } | null,
): Promise<BuildLock> {
	const lock = await buildLock(eccRoot, review);
	await Bun.write(lockPathFor(eccRoot), serializeLock(lock));
	return lock;
}

/** Read and parse the committed build lock; PinError when absent or malformed. */
async function readBuildLock(eccRoot: string): Promise<BuildLock> {
	const lockPath = lockPathFor(eccRoot);
	let parsed: unknown;
	try {
		parsed = JSON.parse(await Bun.file(lockPath).text());
	} catch (err) {
		throw new PinError(`cannot read build lock at ${lockPath}: ${err instanceof Error ? err.message : String(err)}`);
	}
	return parsed as BuildLock;
}

/** Resolve a review pin path inside the ECC root, refusing an escape. */
function resolveReviewPath(eccRoot: string, reviewPath: string): string {
	if (path.isAbsolute(reviewPath)) {
		throw new PinError(`review path must be relative to the ECC root, got '${reviewPath}'`);
	}
	const resolved = path.resolve(eccRoot, reviewPath);
	const relative = path.relative(eccRoot, resolved);
	if (relative.startsWith("..") || path.isAbsolute(relative)) {
		throw new PinError(`review path '${reviewPath}' escapes the ECC root`);
	}
	return resolved;
}

/**
 * Verify the committed lock against the committed manifest and, when attached,
 * the reviewed record. Every failure is a PinError naming what drifted.
 */
export async function verifyBuildLock(eccRoot: string = ECC_ROOT): Promise<void> {
	const manifestBytes = await readManifestBytes(eccRoot);
	const expectedManifestSha = sha256Hex(manifestBytes);
	const lock = await readBuildLock(eccRoot);

	if (lock.manifestSha256 !== expectedManifestSha) {
		throw new PinError(
			`manifest sha256 mismatch: lock pins ${lock.manifestSha256} but manifest.json hashes to ${expectedManifestSha}`,
		);
	}

	const manifest = await loadManifest(eccRoot);
	if (lock.upstream.commit !== manifest.upstream.commit) {
		throw new PinError(
			`upstream commit mismatch: lock pins ${lock.upstream.commit} but manifest.json pins ${manifest.upstream.commit}`,
		);
	}

	if (lock.review === null) return;

	const reviewPath = resolveReviewPath(eccRoot, lock.review.path);
	let bytes: Uint8Array;
	try {
		bytes = new Uint8Array(await Bun.file(reviewPath).arrayBuffer());
	} catch (err) {
		if ((err as NodeJS.ErrnoException)?.code === "ENOENT") {
			throw new PinError(`review file '${lock.review.path}' is missing`);
		}
		throw err;
	}
	const actualReviewSha = sha256Hex(bytes);
	if (actualReviewSha !== lock.review.sha256) {
		throw new PinError(
			`review sha256 mismatch for '${lock.review.path}': lock pins ${lock.review.sha256} but the file hashes to ${actualReviewSha}`,
		);
	}

	let review: UpdateReview;
	try {
		review = validateReview(JSON.parse(new TextDecoder().decode(bytes)));
	} catch (err) {
		throw new PinError(
			`review '${lock.review.path}' is invalid: ${err instanceof Error ? err.message : String(err)}`,
		);
	}
	if (review.decision !== "accept") {
		throw new PinError(`review '${lock.review.path}' is not accepted (decision '${review.decision}')`);
	}
	if (review.candidate.commit !== lock.upstream.commit) {
		throw new PinError(
			`review '${lock.review.path}' names candidate ${review.candidate.commit} but the lock pins ${lock.upstream.commit}`,
		);
	}
}
