/**
 * CPK-1 canonical plugin manifest.
 *
 * A CPK-1 manifest is a declarative, content-addressable description of what a
 * plugin provides, what it requires, which effects it may exercise, and which
 * closed scopes it may be active in. Parsing canonicalizes every array
 * (sorted, deduplicated) so two manifests that differ only in declaration order
 * produce the same content address.
 *
 * This module is a pure library: it is not wired into any live execution path.
 */

/** Schema discriminator for CPK-1 manifests. */
export const CPK1_SCHEMA = "cpk1/v1";

/**
 * Closed effect scopes, ordered from the widest (installation) to the
 * narrowest (single agent). A child scope may never widen beyond its parent.
 */
export const CPK_SCOPE_ORDER = ["install", "project", "work", "session", "agent"] as const;

export type CpkScope = (typeof CPK_SCOPE_ORDER)[number];

export interface CpkManifest {
	schema: typeof CPK1_SCHEMA;
	id: string;
	version: string;
	provides: string[];
	requires: string[];
	effects: string[];
	scopes: CpkScope[];
}

export type CpkManifestErrorCode = "not_object" | "invalid_schema" | "invalid_field" | "invalid_scope";

export class CpkManifestError extends Error {
	readonly code: CpkManifestErrorCode;

	constructor(code: CpkManifestErrorCode, message: string) {
		super(message);
		this.name = "CpkManifestError";
		this.code = code;
	}
}

/** Sorted, deduplicated copy of a string array. */
export function sortUnique(values: string[]): string[] {
	return Array.from(new Set(values)).sort();
}

/** Type guard for the closed set of CPK scopes. */
export function isCpkScope(value: string): value is CpkScope {
	return (CPK_SCOPE_ORDER as readonly string[]).includes(value);
}

function sortScopes(scopes: CpkScope[]): CpkScope[] {
	const unique = Array.from(new Set(scopes));
	return unique.sort((a, b) => CPK_SCOPE_ORDER.indexOf(a) - CPK_SCOPE_ORDER.indexOf(b));
}

function requireNonEmptyString(value: unknown, field: string): string {
	if (typeof value !== "string" || value.length === 0) {
		throw new CpkManifestError("invalid_field", `manifest field "${field}" must be a non-empty string`);
	}
	return value;
}

/**
 * Parse an optional array of non-empty strings into a sorted, deduplicated
 * copy. Shared by manifest and scope-ceiling parsing so both reject the same
 * malformed shapes with the same error code.
 */
export function parseStringArray(value: unknown, field: string): string[] {
	if (value === undefined) return [];
	if (!Array.isArray(value)) {
		throw new CpkManifestError("invalid_field", `field "${field}" must be an array of strings`);
	}
	const out: string[] = [];
	for (const entry of value) {
		if (typeof entry !== "string" || entry.length === 0) {
			throw new CpkManifestError("invalid_field", `field "${field}" must contain only non-empty strings`);
		}
		out.push(entry);
	}
	return sortUnique(out);
}

function parseScopes(value: unknown): CpkScope[] {
	if (value === undefined) return [];
	if (!Array.isArray(value)) {
		throw new CpkManifestError("invalid_field", `manifest field "scopes" must be an array`);
	}
	const out: CpkScope[] = [];
	for (const entry of value) {
		if (typeof entry !== "string" || !isCpkScope(entry)) {
			throw new CpkManifestError("invalid_scope", `manifest scope must be one of ${CPK_SCOPE_ORDER.join(", ")}`);
		}
		out.push(entry);
	}
	return sortScopes(out);
}

/**
 * Validate and canonicalize an untrusted manifest value. Throws
 * {@link CpkManifestError} with a stable `code` on malformed input.
 */
export function parseCpkManifest(input: unknown): CpkManifest {
	if (typeof input !== "object" || input === null || Array.isArray(input)) {
		throw new CpkManifestError("not_object", "manifest must be a JSON object");
	}
	const record = input as Record<string, unknown>;
	if (record.schema !== CPK1_SCHEMA) {
		throw new CpkManifestError("invalid_schema", `manifest schema must be "${CPK1_SCHEMA}"`);
	}
	return {
		schema: CPK1_SCHEMA,
		id: requireNonEmptyString(record.id, "id"),
		version: requireNonEmptyString(record.version, "version"),
		provides: parseStringArray(record.provides, "provides"),
		requires: parseStringArray(record.requires, "requires"),
		effects: parseStringArray(record.effects, "effects"),
		scopes: parseScopes(record.scopes),
	};
}

/** Deterministic JSON encoding of a canonical manifest. */
export function canonicalizeCpkManifest(manifest: CpkManifest): string {
	return JSON.stringify({
		schema: manifest.schema,
		id: manifest.id,
		version: manifest.version,
		provides: sortUnique(manifest.provides),
		requires: sortUnique(manifest.requires),
		effects: sortUnique(manifest.effects),
		scopes: sortScopes(manifest.scopes),
	});
}

/** Content address of a manifest: `sha256:<hex>` over its canonical encoding. */
export function contentAddress(manifest: CpkManifest): string {
	const hasher = new Bun.CryptoHasher("sha256");
	hasher.update(canonicalizeCpkManifest(manifest));
	return `sha256:${hasher.digest("hex")}`;
}
