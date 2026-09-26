/**
 * CPK-1 deterministic, content-addressed dependency graph.
 *
 * Resolves a set of canonical manifests into a graph whose node order, cycle
 * report, and content address are independent of the input order. The graph is
 * a pure data structure: it is not wired into any live execution path.
 */

import { type CpkManifest, contentAddress, sortUnique } from "./manifest";

export interface CpkGraphNode {
	id: string;
	address: string;
	manifest: CpkManifest;
	/** Declared requirements, sorted. May name ids absent from the graph. */
	dependencies: string[];
}

export interface CpkGraph {
	nodes: CpkGraphNode[];
	/** Dependency-first topological order of resolvable nodes. */
	order: string[];
	/** Content address over sorted (id, address) pairs and edges. */
	address: string;
	/** Strongly connected components of size > 1, plus self-loops; each sorted. */
	cycles: string[][];
	/** Required ids with no manifest in the set, sorted. */
	missing: string[];
	/** Ids absent from `order` (cycle members and their dependents), sorted. */
	unresolved: string[];
}

export type CpkGraphErrorCode = "duplicate_id";

export class CpkGraphError extends Error {
	readonly code: CpkGraphErrorCode;

	constructor(code: CpkGraphErrorCode, message: string) {
		super(message);
		this.name = "CpkGraphError";
		this.code = code;
	}
}

/**
 * Tarjan's strongly connected components, iterative so deep graphs cannot
 * overflow the stack. Returns components in deterministic discovery order.
 */
function stronglyConnectedComponents(ids: string[], adjacency: Map<string, string[]>): string[][] {
	const index = new Map<string, number>();
	const lowlink = new Map<string, number>();
	const onStack = new Set<string>();
	const stack: string[] = [];
	const components: string[][] = [];
	let nextIndex = 0;

	for (const root of ids) {
		if (index.has(root)) continue;
		// Each frame: [node, next neighbor position].
		const frames: Array<[string, number]> = [[root, 0]];
		index.set(root, nextIndex);
		lowlink.set(root, nextIndex);
		nextIndex += 1;
		stack.push(root);
		onStack.add(root);

		while (frames.length > 0) {
			const frame = frames[frames.length - 1];
			const node = frame[0];
			const neighbors = adjacency.get(node) ?? [];
			if (frame[1] < neighbors.length) {
				const neighbor = neighbors[frame[1]];
				frame[1] += 1;
				if (!index.has(neighbor)) {
					index.set(neighbor, nextIndex);
					lowlink.set(neighbor, nextIndex);
					nextIndex += 1;
					stack.push(neighbor);
					onStack.add(neighbor);
					frames.push([neighbor, 0]);
				} else if (onStack.has(neighbor)) {
					lowlink.set(node, Math.min(lowlink.get(node)!, index.get(neighbor)!));
				}
				continue;
			}

			frames.pop();
			if (frames.length > 0) {
				const parent = frames[frames.length - 1][0];
				lowlink.set(parent, Math.min(lowlink.get(parent)!, lowlink.get(node)!));
			}
			if (lowlink.get(node) === index.get(node)) {
				const component: string[] = [];
				for (;;) {
					const member = stack.pop()!;
					onStack.delete(member);
					component.push(member);
					if (member === node) break;
				}
				components.push(component);
			}
		}
	}

	return components;
}

/**
 * Resolve canonical manifests into a deterministic dependency graph.
 *
 * Throws {@link CpkGraphError} when two manifests share an id. Missing
 * requirements and cycles are reported on the graph rather than thrown, so a
 * caller can inspect a partially resolvable set.
 */
export function resolveCpkGraph(manifests: CpkManifest[]): CpkGraph {
	const byId = new Map<string, CpkManifest>();
	for (const manifest of manifests) {
		if (byId.has(manifest.id)) {
			throw new CpkGraphError("duplicate_id", `duplicate manifest id "${manifest.id}"`);
		}
		byId.set(manifest.id, manifest);
	}

	const ids = Array.from(byId.keys()).sort();
	const idSet = new Set(ids);

	const nodes: CpkGraphNode[] = ids.map(id => {
		const manifest = byId.get(id)!;
		return {
			id,
			address: contentAddress(manifest),
			manifest,
			dependencies: sortUnique(manifest.requires),
		};
	});

	const missing = sortUnique(nodes.flatMap(node => node.dependencies.filter(dep => !idSet.has(dep))));

	// Adjacency over present nodes only; missing deps cannot be ordered.
	const adjacency = new Map<string, string[]>();
	for (const node of nodes) {
		adjacency.set(
			node.id,
			node.dependencies.filter(dep => idSet.has(dep)),
		);
	}

	const components = stronglyConnectedComponents(ids, adjacency);
	const cycles = components
		.filter(component => component.length > 1 || adjacency.get(component[0])?.includes(component[0]))
		.map(component => [...component].sort())
		.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));

	const cyclic = new Set(cycles.flat());

	// Kahn's algorithm with lexicographic tie-break among ready nodes.
	const indegree = new Map<string, number>();
	for (const id of ids) indegree.set(id, adjacency.get(id)!.length);
	const ready = ids.filter(id => indegree.get(id) === 0 && !cyclic.has(id));
	const order: string[] = [];
	while (ready.length > 0) {
		ready.sort();
		const id = ready.shift()!;
		order.push(id);
		for (const dependent of ids) {
			if (!adjacency.get(dependent)!.includes(id)) continue;
			const next = indegree.get(dependent)! - 1;
			indegree.set(dependent, next);
			if (next === 0 && !cyclic.has(dependent)) ready.push(dependent);
		}
	}

	const ordered = new Set(order);
	const unresolved = ids.filter(id => !ordered.has(id));

	const addressLines: string[] = [];
	for (const node of nodes) addressLines.push(`node\t${node.id}\t${node.address}`);
	for (const node of nodes) {
		for (const dep of node.dependencies) addressLines.push(`edge\t${node.id}\t${dep}`);
	}
	const hasher = new Bun.CryptoHasher("sha256");
	hasher.update(addressLines.join("\n"));

	return {
		nodes,
		order,
		address: `sha256:${hasher.digest("hex")}`,
		cycles,
		missing,
		unresolved,
	};
}
