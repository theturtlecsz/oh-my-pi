/**
 * Owner update-review record validation.
 *
 * An update review is the human acceptance act for a proposed upstream change:
 * it names the base and candidate revisions, enumerates the per-file adapted
 * hash changes, and carries the reviewer's identity and decision. The build
 * lock pins the review file by path and sha256, so verification re-reads the
 * exact bytes that were reviewed and refuses a record that has drifted.
 *
 * Validation is strict about structure: unknown keys are rejected so a typo
 * cannot silently drop a reviewed fact, and revisions/hashes are checked for
 * the full hex length so a short id is never mistaken for a pin.
 */
import type { UpdateReview } from "./types";

export class ReviewError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "ReviewError";
	}
}

const REVIEW_KEYS: ReadonlySet<string> = new Set([
	"schemaVersion",
	"baseCommit",
	"candidate",
	"changes",
	"newFiles",
	"reviewer",
	"reviewedAt",
	"decision",
	"notes",
]);
const CANDIDATE_KEYS: ReadonlySet<string> = new Set(["commit", "tree", "version", "describe"]);
const CHANGE_KEYS: ReadonlySet<string> = new Set(["assetId", "path", "fromSha256", "toSha256"]);

const HEX40 = /^[0-9a-f]{40}$/i;
const HEX64 = /^[0-9a-f]{64}$/i;

function requireObject(value: unknown, label: string): Record<string, unknown> {
	if (!value || typeof value !== "object" || Array.isArray(value)) {
		throw new ReviewError(`${label} must be a JSON object`);
	}
	return value as Record<string, unknown>;
}

/** Reject unknown keys and require every expected key, at one object level. */
function requireKeys(obj: Record<string, unknown>, allowed: ReadonlySet<string>, label: string): void {
	for (const key of Object.keys(obj)) {
		if (!allowed.has(key)) throw new ReviewError(`${label} has unknown key '${key}'`);
	}
	for (const key of allowed) {
		if (!(key in obj)) throw new ReviewError(`${label} is missing required key '${key}'`);
	}
}

function requireHex(value: unknown, length: 40 | 64, label: string): string {
	const pattern = length === 40 ? HEX40 : HEX64;
	if (typeof value !== "string" || !pattern.test(value)) {
		throw new ReviewError(`${label} must be a ${length}-character hex string, got ${JSON.stringify(value)}`);
	}
	return value.toLowerCase();
}

function requireString(value: unknown, label: string): string {
	if (typeof value !== "string") throw new ReviewError(`${label} must be a string`);
	return value;
}

function requireNonEmptyString(value: unknown, label: string): string {
	if (typeof value !== "string" || value.trim() === "") {
		throw new ReviewError(`${label} must be a non-empty string`);
	}
	return value;
}

/** Validate an update-review record, rejecting unknown keys and bad pins. */
export function validateReview(raw: unknown): UpdateReview {
	const obj = requireObject(raw, "review");
	requireKeys(obj, REVIEW_KEYS, "review");
	if (obj.schemaVersion !== 1) {
		throw new ReviewError(`review schemaVersion must be 1, got ${JSON.stringify(obj.schemaVersion)}`);
	}
	const baseCommit = requireHex(obj.baseCommit, 40, "review baseCommit");

	const candidateObj = requireObject(obj.candidate, "review candidate");
	requireKeys(candidateObj, CANDIDATE_KEYS, "review candidate");
	const candidate = {
		commit: requireHex(candidateObj.commit, 40, "review candidate.commit"),
		tree: requireHex(candidateObj.tree, 40, "review candidate.tree"),
		version: requireNonEmptyString(candidateObj.version, "review candidate.version"),
		describe: requireString(candidateObj.describe, "review candidate.describe"),
	};

	if (!Array.isArray(obj.changes)) throw new ReviewError("review changes must be an array");
	const changes = obj.changes.map((entry, index) => {
		const change = requireObject(entry, `review change at index ${index}`);
		requireKeys(change, CHANGE_KEYS, `review change at index ${index}`);
		return {
			assetId: requireNonEmptyString(change.assetId, `review change ${index} assetId`),
			path: requireNonEmptyString(change.path, `review change ${index} path`),
			fromSha256: requireHex(change.fromSha256, 64, `review change ${index} fromSha256`),
			toSha256: requireHex(change.toSha256, 64, `review change ${index} toSha256`),
		};
	});

	if (!Array.isArray(obj.newFiles)) throw new ReviewError("review newFiles must be an array");
	const newFiles = obj.newFiles.map((entry, index) => requireNonEmptyString(entry, `review newFiles[${index}]`));

	const reviewer = requireNonEmptyString(obj.reviewer, "review reviewer");
	const reviewedAt = requireNonEmptyString(obj.reviewedAt, "review reviewedAt");
	if (Number.isNaN(Date.parse(reviewedAt))) {
		throw new ReviewError(`review reviewedAt is not an ISO date, got '${reviewedAt}'`);
	}
	if (obj.decision !== "accept" && obj.decision !== "reject") {
		throw new ReviewError(`review decision must be 'accept' or 'reject', got ${JSON.stringify(obj.decision)}`);
	}
	const notes = requireString(obj.notes, "review notes");

	return {
		schemaVersion: 1,
		baseCommit,
		candidate,
		changes,
		newFiles,
		reviewer,
		reviewedAt,
		decision: obj.decision,
		notes,
	};
}
