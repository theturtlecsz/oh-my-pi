/**
 * CPK-2 closed effect scopes.
 *
 * A scope ceiling declares the maximum set of effects a plugin may exercise at
 * a given scope. Ceilings form a tree (each scope has at most one parent) and
 * must be monotone: a child ceiling may never contain an effect its parent
 * ceiling does not, and a parent must be strictly wider than its child.
 * Composition intersects a requested effect set with both the child and parent
 * ceilings, so a request can only ever narrow — never widen — authority.
 *
 * This module is a pure library: it is not wired into any live execution path.
 */

import { CPK_SCOPE_ORDER, CpkManifestError, type CpkScope, isCpkScope, parseStringArray, sortUnique } from "./manifest";

export interface CpkScopeCeiling {
	scope: CpkScope;
	effects: string[];
	/** Parent scope whose ceiling must contain this ceiling's effects. */
	parent?: CpkScope;
}

export type CpkScopeViolationCode =
	| "unknown_scope"
	| "duplicate_scope"
	| "unknown_parent"
	| "inverted_parent"
	| "widening";

export interface CpkScopeViolation {
	code: CpkScopeViolationCode;
	scope: CpkScope;
	/** Effects that violate the closure, sorted. Empty for structural errors. */
	effects: string[];
	message: string;
}

/**
 * Validate and canonicalize an untrusted scope ceiling. Throws
 * {@link CpkManifestError} with a stable `code` on malformed input.
 */
export function parseCpkScopeCeiling(input: unknown): CpkScopeCeiling {
	if (typeof input !== "object" || input === null || Array.isArray(input)) {
		throw new CpkManifestError("not_object", "scope ceiling must be a JSON object");
	}
	const record = input as Record<string, unknown>;
	const scope = record.scope;
	if (typeof scope !== "string" || !isCpkScope(scope)) {
		throw new CpkManifestError("invalid_scope", `scope ceiling scope must be one of ${CPK_SCOPE_ORDER.join(", ")}`);
	}
	let parent: CpkScope | undefined;
	if (record.parent !== undefined) {
		const rawParent = record.parent;
		if (typeof rawParent !== "string" || !isCpkScope(rawParent)) {
			throw new CpkManifestError(
				"invalid_scope",
				`scope ceiling parent must be one of ${CPK_SCOPE_ORDER.join(", ")}`,
			);
		}
		parent = rawParent;
	}
	return {
		scope,
		effects: parseStringArray(record.effects, "effects"),
		parent,
	};
}

/**
 * Validate that every child ceiling is a subset of its parent ceiling and that
 * parents are strictly wider than their children.
 *
 * Returns one violation per offending scope, sorted by scope order. An empty
 * array means the ceilings are closed.
 */
export function validateScopeClosure(ceilings: CpkScopeCeiling[]): CpkScopeViolation[] {
	const violations: CpkScopeViolation[] = [];
	const byScope = new Map<CpkScope, CpkScopeCeiling>();

	for (const ceiling of ceilings) {
		if (!isCpkScope(ceiling.scope)) {
			violations.push({
				code: "unknown_scope",
				scope: ceiling.scope,
				effects: [],
				message: `unknown scope "${ceiling.scope}"`,
			});
			continue;
		}
		if (byScope.has(ceiling.scope)) {
			violations.push({
				code: "duplicate_scope",
				scope: ceiling.scope,
				effects: [],
				message: `duplicate ceiling for scope "${ceiling.scope}"`,
			});
			continue;
		}
		byScope.set(ceiling.scope, ceiling);
	}

	for (const ceiling of byScope.values()) {
		if (ceiling.parent === undefined) continue;
		if (!isCpkScope(ceiling.parent)) {
			violations.push({
				code: "unknown_parent",
				scope: ceiling.scope,
				effects: [],
				message: `scope "${ceiling.scope}" names unknown parent "${ceiling.parent}"`,
			});
			continue;
		}
		const parent = byScope.get(ceiling.parent);
		if (!parent) {
			violations.push({
				code: "unknown_parent",
				scope: ceiling.scope,
				effects: [],
				message: `scope "${ceiling.scope}" names parent "${ceiling.parent}" with no ceiling`,
			});
			continue;
		}
		if (CPK_SCOPE_ORDER.indexOf(ceiling.parent) >= CPK_SCOPE_ORDER.indexOf(ceiling.scope)) {
			violations.push({
				code: "inverted_parent",
				scope: ceiling.scope,
				effects: [],
				message: `scope "${ceiling.scope}" parent "${ceiling.parent}" is not wider`,
			});
			continue;
		}
		const parentEffects = new Set(parent.effects);
		const widened = sortUnique(ceiling.effects.filter(effect => !parentEffects.has(effect)));
		if (widened.length > 0) {
			violations.push({
				code: "widening",
				scope: ceiling.scope,
				effects: widened,
				message: `scope "${ceiling.scope}" widens beyond parent "${ceiling.parent}": ${widened.join(", ")}`,
			});
		}
	}

	return violations.sort((a, b) => CPK_SCOPE_ORDER.indexOf(a.scope) - CPK_SCOPE_ORDER.indexOf(b.scope));
}

export interface CpkScopeComposition {
	scope: CpkScope;
	/** Requested effects permitted by the child (and parent) ceiling, sorted. */
	granted: string[];
	/** Requested effects outside the ceiling, sorted. */
	refused: string[];
	/** True when at least one requested effect was refused. */
	widening: boolean;
}

/**
 * Compose a requested effect set against a child ceiling.
 *
 * The effective grant is `requested ∩ child.effects`, further narrowed by
 * `parent.effects` when a parent is supplied. Any requested effect outside the
 * ceiling is refused and reported, never silently widened, so the grant is
 * always a subset of both ceilings.
 */
export function composeScopeEffects(
	parent: CpkScopeCeiling | undefined,
	child: CpkScopeCeiling,
	requested: string[],
): CpkScopeComposition {
	const allowed = new Set(child.effects);
	if (parent) {
		const parentEffects = new Set(parent.effects);
		for (const effect of Array.from(allowed)) {
			if (!parentEffects.has(effect)) allowed.delete(effect);
		}
	}

	const granted: string[] = [];
	const refused: string[] = [];
	for (const effect of sortUnique(requested)) {
		if (allowed.has(effect)) granted.push(effect);
		else refused.push(effect);
	}

	return {
		scope: child.scope,
		granted,
		refused,
		widening: refused.length > 0,
	};
}
