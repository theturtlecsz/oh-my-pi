/**
 * CPK-1/CPK-2 shadow composition.
 *
 * Shadow mode computes what a manifest-driven composition *would* produce
 * without touching the live composition. It is default-off: when disabled it
 * returns the live composition by reference, so callers can prove that
 * enabling the shadow path changes nothing observable.
 *
 * This module is a pure library: it is not wired into any live execution path.
 */

import { resolveCpkGraph } from "./graph";
import { type CpkManifest, sortUnique } from "./manifest";

export interface CpkComposition {
	/** Selected plugin ids, sorted. */
	plugins: string[];
	/** Union of selected plugin effects, sorted. */
	effects: string[];
	/** Content address of the resolved dependency graph. */
	graphAddress: string;
}

export interface CpkShadowOptions {
	/** Shadow mode is off unless explicitly enabled. */
	enabled?: boolean;
}

export interface CpkShadowResult {
	enabled: boolean;
	live: CpkComposition;
	shadow: CpkComposition;
	identical: boolean;
	/** Names of the composition fields that differ, in a stable order. */
	differences: string[];
}

/**
 * Compose a manifest set into a deterministic composition. Only manifests whose
 * id is in `selection` participate; the result is independent of input order.
 */
export function composeCpk(manifests: CpkManifest[], selection: string[]): CpkComposition {
	const selected = new Set(selection);
	const chosen = manifests.filter(manifest => selected.has(manifest.id));
	const graph = resolveCpkGraph(chosen);
	return {
		plugins: graph.nodes.map(node => node.id),
		effects: sortUnique(chosen.flatMap(manifest => manifest.effects)),
		graphAddress: graph.address,
	};
}

function sameStrings(a: string[], b: string[]): boolean {
	if (a.length !== b.length) return false;
	for (let i = 0; i < a.length; i += 1) {
		if (a[i] !== b[i]) return false;
	}
	return true;
}

function diffCompositions(live: CpkComposition, shadow: CpkComposition): string[] {
	const differences: string[] = [];
	if (live.graphAddress !== shadow.graphAddress) differences.push("graphAddress");
	if (!sameStrings(live.plugins, shadow.plugins)) differences.push("plugins");
	if (!sameStrings(live.effects, shadow.effects)) differences.push("effects");
	return differences;
}

/**
 * Run shadow composition against a live composition.
 *
 * When disabled (the default) the live composition is returned by reference and
 * reported identical, so the shadow path cannot alter live execution. When
 * enabled, the shadow composition is computed and compared field by field.
 */
export function runCpkShadow(
	live: CpkComposition,
	manifests: CpkManifest[],
	selection: string[],
	options: CpkShadowOptions = {},
): CpkShadowResult {
	if (!(options.enabled ?? false)) {
		return { enabled: false, live, shadow: live, identical: true, differences: [] };
	}
	const shadow = composeCpk(manifests, selection);
	const differences = diffCompositions(live, shadow);
	return { enabled: true, live, shadow, identical: differences.length === 0, differences };
}

/**
 * Prove that an empty selection composes to the live composition. Used to
 * demonstrate that the shadow path is inert when no plugin is selected.
 */
export function proveShadowIdentity(live: CpkComposition, manifests: CpkManifest[]): CpkShadowResult {
	return runCpkShadow(live, manifests, [], { enabled: true });
}
