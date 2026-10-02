/**
 * ECC adapter type contract.
 *
 * The pack-scoped mirror under session-system/ecc/mirror is the pinned upstream
 * content; manifest.json maps each mirrored asset onto a native OMP discovery
 * destination through a deterministic transform. These types are the shape
 * persisted in manifest.json and adapted.lock.json, so field names are part of
 * the on-disk contract and must not drift silently.
 */

/** Adaptation kinds the transform understands. */
export type AdaptationKind = "rule-metadata" | "skill-namespaced" | "agent-metadata" | "database-advisor-profile";

/** Adoption disposition from ECC-IMPLEMENTATION-SPEC.md section 5. */
export type Disposition =
	| "supported-without-changes"
	| "supported-with-adaptation"
	| "reference-only"
	| "deferred"
	| "excluded";

/** Assets with these dispositions are recorded but never installed. */
export const NON_INSTALLABLE_DISPOSITIONS: readonly Disposition[] = ["reference-only", "deferred", "excluded"];

export interface ManifestUpstream {
	repository: string;
	commit: string;
	tree: string;
	version: string;
	describe: string;
}

export interface PackSelection {
	profileId: string | null;
	/** Upstream ECC module ids this pack selects from (informational). */
	moduleIds: string[];
}

export interface ManifestPack {
	id: string;
	/** Which of the two deliverables (R01) this pack belongs to. */
	lane: "engineering" | "research";
	selection: PackSelection;
	assetIds: string[];
}

export interface AssetUpstream {
	repository: string;
	commit: string;
	/** Path within the pinned upstream checkout (also the mirror path). */
	path: string;
	sha256: string;
}

export interface AssetTarget {
	/** Destination relative to the install root, e.g. `.omp/rules/x.md`. */
	path: string;
	scope: "project" | "user";
}

export interface AssetAdaptation {
	kind: AdaptationKind;
	transformVersion: number;
	/** Overlay file relative to session-system/ecc, when the kind uses one. */
	overlay?: string;
}

export interface AssetActivation {
	/** `inactive` unless the asset is explicitly enabled by the owner. */
	state: "inactive" | "active";
	mode: string;
	globs?: string[];
	description?: string;
	selection?: string;
}

export interface AssetQualification {
	mechanicalTests: string[];
	behavioralTrial: null;
	qualifiedRelease: null;
}

export interface ManifestAsset {
	id: string;
	pack: "engineering" | "research";
	upstream: AssetUpstream;
	target: AssetTarget;
	adaptation: AssetAdaptation;
	activation: AssetActivation;
	/**
	 * Internal/helper references the adapted content needs: assets for rules,
	 * or files relative to the asset's own directory for skills. An empty list
	 * is valid only after inherited references were actually resolved.
	 */
	dependencies: string[];
	runtimes?: string[];
	disposition: Disposition;
	reason: string;
	qualification: AssetQualification;
	rollback: { removeOwnedArtifactOnly: true };
}

export interface DispositionRule {
	prefix: string;
	disposition: Disposition;
	reason: string;
}

export interface Manifest {
	schemaVersion: 1;
	upstream: ManifestUpstream;
	transformVersion: number;
	packs: ManifestPack[];
	/** Slash-command owners this pack must never displace. */
	nativeCommandNames: string[];
	assets: ManifestAsset[];
	dispositionRules: DispositionRule[];
}

export interface OverlayEdit {
	reason: string;
	find: string[];
	replace: string[];
}

export interface Overlay {
	schemaVersion: 1;
	assetId: string;
	upstreamPath: string;
	upstreamSha256: string;
	adaptedSha256: string;
	edits: OverlayEdit[];
}

/** One installed file recorded in adapted.lock.json. */
export interface LockFile {
	path: string;
	sha256: string;
	mode: string;
}

export interface LockAsset {
	id: string;
	upstreamPath: string;
	upstreamSha256: string;
	adaptedSha256: string;
	adaptationKind: AdaptationKind;
	disposition: Disposition;
	files: LockFile[];
}

export interface AdaptedLock {
	schemaVersion: 1;
	transformVersion: number;
	upstream: { commit: string; tree: string };
	assets: LockAsset[];
}

/** One file an update changes, with the adapted hashes before and after. */
export interface ReviewChange {
	assetId: string;
	path: string;
	fromSha256: string;
	toSha256: string;
}

/**
 * The owner's review of a proposed upstream update. Authored as a JSON file
 * relative to the ECC root and pinned into the build lock by path+sha256 so a
 * later verification run re-reads and re-validates the exact reviewed record.
 */
export interface UpdateReview {
	schemaVersion: 1;
	baseCommit: string;
	candidate: { commit: string; tree: string; version: string; describe: string };
	changes: ReviewChange[];
	newFiles: string[];
	reviewer: string;
	/** ISO-8601 date (or date-time) the review was recorded. */
	reviewedAt: string;
	decision: "accept" | "reject";
	notes: string;
}

/**
 * The committed build lock beside the mirror: an AdaptedLock plus a pin over
 * the manifest bytes that produced it and, when an update review is attached,
 * the review file's path (relative to the ECC root) and content sha256.
 */
export interface BuildLock extends AdaptedLock {
	manifestSha256: string;
	review: { path: string; sha256: string } | null;
}

export interface PlannedFile extends LockFile {
	/** Asset that owns this file — removal only touches owned paths. */
	assetId: string;
	bytes: Uint8Array;
}

export interface InstallPlan {
	files: PlannedFile[];
	assets: LockAsset[];
}
