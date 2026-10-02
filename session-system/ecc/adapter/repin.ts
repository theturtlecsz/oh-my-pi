/**
 * Reviewed re-pin of the pinned ECC mirror to an accepted upstream candidate.
 *
 * A re-pin is the only path that moves the pinned upstream commit. It applies a
 * candidate the owner already accepted in an UpdateReview, but trusts nothing in
 * that record: it re-detects the change from the candidate checkout, requires
 * the review to agree with that detection, and refuses to touch the pinned ECC
 * root unless the resulting manifest, mirror, review copy, and build lock all
 * verify together.
 *
 * The apply is staged: the whole new state is assembled in a temp copy of the
 * ECC root, the build lock is rebuilt and verified there, and only then are the
 * staged bytes copied back. A refusal leaves the committed ECC root byte for
 * byte unchanged.
 *
 * Usage:
 *   bun session-system/ecc/adapter/repin.ts --candidate-root <dir> --review <file> [--ecc-root <dir>]
 */
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ECC_ROOT, installablePackedAssets, loadManifest, primaryUpstreamPath } from "./catalog";
import { sha256Hex } from "./hash";
import { lockPathFor } from "./lock";
import { verifyBuildLock, writeBuildLock } from "./pin";
import { validateReview } from "./review";
import type { UpdateReview } from "./types";
import { type UpstreamCheckResult, checkUpstream } from "./upstream-check";

export class RepinError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "RepinError";
	}
}

export interface RepinOptions {
	eccRoot?: string;
	candidateRoot: string;
	/** Path to the owner's accepted UpdateReview JSON. */
	review: string;
}

export interface RepinResult {
	fromManifestSha256: string;
	toManifestSha256: string;
	candidateCommit: string;
	changedFiles: string[];
}

interface ChangedRow {
	assetId: string;
	path: string;
	fromSha256: string;
	toSha256: string;
}

function messageOf(err: unknown): string {
	return err instanceof Error ? err.message : String(err);
}

async function readBytes(filePath: string): Promise<Uint8Array> {
	return new Uint8Array(await Bun.file(filePath).arrayBuffer());
}

async function copyBytes(source: string, destination: string): Promise<void> {
	await fs.mkdir(path.dirname(destination), { recursive: true });
	await Bun.write(destination, await readBytes(source));
}

/** The changed primary-file rows, keyed by their manifest primary upstream path. */
function changedRowsOf(report: UpstreamCheckResult): ChangedRow[] {
	return report.files
		.filter(file => file.status === "changed")
		.map(file => {
			if (file.toSha256 === null) {
				throw new RepinError(`changed row '${file.path}' has no candidate hash`);
			}
			return {
				assetId: file.assetId,
				path: file.path,
				fromSha256: file.fromSha256,
				toSha256: file.toSha256,
			};
		});
}

function canonicalChanges(changes: readonly ChangedRow[]): string {
	return [...changes]
		.sort((a, b) => a.path.localeCompare(b.path))
		.map(change => `${change.assetId}\0${change.path}\0${change.fromSha256}\0${change.toSha256}`)
		.join("\n");
}

/**
 * Refuse unless the review agrees with the detected upstream change: accepted,
 * based on the currently pinned commit, and enumerating exactly the changed
 * files, no missing consumed file, no overlay conflict, and the detected new
 * files.
 */
function assertReviewAgrees(review: UpdateReview, manifestCommit: string, report: UpstreamCheckResult): ChangedRow[] {
	if (review.decision !== "accept") {
		throw new RepinError(`review decision is '${review.decision}', expected 'accept'`);
	}
	if (review.baseCommit !== manifestCommit) {
		throw new RepinError(
			`review baseCommit ${review.baseCommit} does not match the pinned manifest upstream.commit ${manifestCommit}`,
		);
	}
	if (!report.changed) {
		throw new RepinError(
			`candidate ${report.candidateCommit} has no changes against the pinned mirror; nothing to re-pin`,
		);
	}

	const missing = report.files.filter(file => file.status === "missing");
	if (missing.length > 0) {
		throw new RepinError(`candidate is missing consumed file(s): ${missing.map(f => f.path).join(", ")}`);
	}
	if (report.overlayConflicts.length > 0) {
		throw new RepinError(`candidate edits overlay-backed asset(s): ${report.overlayConflicts.join(", ")}`);
	}

	const changedRows = changedRowsOf(report);
	if (canonicalChanges(review.changes) !== canonicalChanges(changedRows)) {
		throw new RepinError("review.changes does not match the detected upstream changes");
	}
	if (canonicalList(review.newFiles) !== canonicalList(report.newFiles)) {
		throw new RepinError("review.newFiles does not match the detected new files");
	}
	return changedRows;
}

function canonicalList(values: readonly string[]): string {
	return [...values].sort((a, b) => a.localeCompare(b)).join("\n");
}

/**
 * Apply an accepted review to a copy of the ECC root, verify the rebuilt lock,
 * then copy the staged bytes back. Refuses before any write on mismatch.
 */
export async function repin(options: RepinOptions): Promise<RepinResult> {
	const eccRoot = path.resolve(options.eccRoot ?? ECC_ROOT);
	const candidateRoot = path.resolve(options.candidateRoot);
	const reviewPath = path.resolve(options.review);

	let review: UpdateReview;
	try {
		review = validateReview(JSON.parse(new TextDecoder().decode(await readBytes(reviewPath))));
	} catch (err) {
		throw new RepinError(`review is invalid: ${messageOf(err)}`);
	}

	const manifest = await loadManifest(eccRoot);
	const candidateCommit = review.candidate.commit;
	const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit });
	const changedRows = assertReviewAgrees(review, manifest.upstream.commit, report);

	const fromManifestSha256 = sha256Hex(await readBytes(path.join(eccRoot, "manifest.json")));

	const stagingParent = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-repin-"));
	const staging = path.join(stagingParent, "ecc");
	try {
		await fs.cp(eccRoot, staging, { recursive: true });

		for (const row of changedRows) {
			await copyBytes(path.join(candidateRoot, row.path), path.join(staging, "mirror", row.path));
		}

		const stagedManifest = await loadManifest(staging);
		stagedManifest.upstream = {
			repository: stagedManifest.upstream.repository,
			commit: candidateCommit,
			tree: review.candidate.tree,
			version: review.candidate.version,
			describe: review.candidate.describe,
		};
		const changedByPrimaryPath = new Map(changedRows.map(row => [row.path, row]));
		for (const asset of installablePackedAssets(stagedManifest)) {
			asset.upstream.commit = candidateCommit;
			const row = changedByPrimaryPath.get(primaryUpstreamPath(asset));
			if (row) asset.upstream.sha256 = row.toSha256;
		}
		await Bun.write(path.join(staging, "manifest.json"), `${JSON.stringify(stagedManifest, null, 2)}\n`);

		await copyBytes(reviewPath, path.join(staging, "reviews", `${candidateCommit}.json`));
		const reviewPin = {
			path: `reviews/${candidateCommit}.json`,
			sha256: sha256Hex(await readBytes(path.join(staging, "reviews", `${candidateCommit}.json`))),
		};
		await writeBuildLock(staging, reviewPin);
		await verifyBuildLock(staging);

		const toManifestSha256 = sha256Hex(await readBytes(path.join(staging, "manifest.json")));

		for (const row of changedRows) {
			await copyBytes(path.join(staging, "mirror", row.path), path.join(eccRoot, "mirror", row.path));
		}
		await copyBytes(path.join(staging, "manifest.json"), path.join(eccRoot, "manifest.json"));
		await copyBytes(
			path.join(staging, "reviews", `${candidateCommit}.json`),
			path.join(eccRoot, "reviews", `${candidateCommit}.json`),
		);
		await copyBytes(lockPathFor(staging), lockPathFor(eccRoot));

		return {
			fromManifestSha256,
			toManifestSha256,
			candidateCommit,
			changedFiles: changedRows.map(row => row.path).sort((a, b) => a.localeCompare(b)),
		};
	} finally {
		await fs.rm(stagingParent, { recursive: true, force: true });
	}
}

export interface RepinCliArgs {
	candidateRoot: string;
	review: string;
	eccRoot: string;
}

function takeOption(argv: string[], index: number, name: string): { value: string; next: number } {
	const arg = argv[index] ?? "";
	const prefix = `${name}=`;
	if (arg.startsWith(prefix)) {
		const value = arg.slice(prefix.length);
		if (value.length === 0) throw new Error(`${name} requires a value`);
		return { value, next: index };
	}
	const value = argv[index + 1];
	if (!value || value.startsWith("-")) throw new Error(`${name} requires a value`);
	return { value, next: index + 1 };
}

export function parseRepinCliArgs(argv: string[]): RepinCliArgs {
	let candidateRoot: string | undefined;
	let review: string | undefined;
	let eccRoot = ECC_ROOT;

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i] ?? "";
		if (arg === "--candidate-root" || arg.startsWith("--candidate-root=")) {
			const taken = takeOption(argv, i, "--candidate-root");
			candidateRoot = taken.value;
			i = taken.next;
			continue;
		}
		if (arg === "--review" || arg.startsWith("--review=")) {
			const taken = takeOption(argv, i, "--review");
			review = taken.value;
			i = taken.next;
			continue;
		}
		if (arg === "--ecc-root" || arg.startsWith("--ecc-root=")) {
			const taken = takeOption(argv, i, "--ecc-root");
			eccRoot = taken.value;
			i = taken.next;
			continue;
		}
		if (arg.startsWith("-")) throw new Error(`unknown argument ${arg}`);
		throw new Error(`unexpected argument ${arg}`);
	}

	if (!candidateRoot) throw new Error("--candidate-root requires a directory");
	if (!review) throw new Error("--review requires a file");
	return {
		candidateRoot: path.resolve(candidateRoot),
		review: path.resolve(review),
		eccRoot: path.resolve(eccRoot),
	};
}

async function main(): Promise<void> {
	try {
		process.stdout.write(`${JSON.stringify(await repin(parseRepinCliArgs(process.argv.slice(2))))}\n`);
	} catch (err) {
		process.stdout.write(`${JSON.stringify({ error: messageOf(err) })}\n`);
		process.exitCode = 1;
	}
}

if (import.meta.main) {
	await main();
}
