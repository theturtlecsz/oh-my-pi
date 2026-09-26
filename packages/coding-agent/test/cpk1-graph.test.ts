import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { CpkGraphError, resolveCpkGraph } from "../src/extensibility/cpk/graph";
import { type CpkManifest, parseCpkManifest } from "../src/extensibility/cpk/manifest";

const fixturePath = path.resolve(import.meta.dir, "fixtures/cpk/manifests.json");

function manifest(id: string, requires: string[]): CpkManifest {
	return parseCpkManifest({
		schema: "cpk1/v1",
		id,
		version: "1.0.0",
		provides: [`${id}.api`],
		requires,
		effects: ["read"],
		scopes: ["install"],
	});
}

async function loadFixture(): Promise<CpkManifest[]> {
	const raw = (await Bun.file(fixturePath).json()) as unknown[];
	return raw.map(parseCpkManifest);
}

describe("CPK-1 dependency graph (OMP-205)", () => {
	it("resolves the fixture DAG dependency-first with a lexicographic tie-break", async () => {
		const graph = resolveCpkGraph(await loadFixture());
		expect(graph.order).toEqual(["core", "audit", "search", "writer"]);
		expect(graph.cycles).toEqual([]);
		expect(graph.missing).toEqual([]);
		expect(graph.unresolved).toEqual([]);
	});

	it("is independent of input order for nodes, order, and content address", async () => {
		const manifests = await loadFixture();
		const forward = resolveCpkGraph(manifests);
		const reversed = resolveCpkGraph([...manifests].reverse());
		expect(reversed.order).toEqual(forward.order);
		expect(reversed.nodes).toEqual(forward.nodes);
		expect(reversed.address).toBe(forward.address);
	});

	it("reports a missing requirement instead of throwing", () => {
		const graph = resolveCpkGraph([manifest("a", ["z"])]);
		expect(graph.missing).toEqual(["z"]);
		expect(graph.order).toEqual(["a"]);
		expect(graph.unresolved).toEqual([]);
	});

	it("reports a cycle and leaves its members unresolved", () => {
		const graph = resolveCpkGraph([manifest("a", ["b"]), manifest("b", ["a"])]);
		expect(graph.cycles).toEqual([["a", "b"]]);
		expect(graph.unresolved).toEqual(["a", "b"]);
		expect(graph.order).toEqual([]);
	});

	it("rejects duplicate manifest ids", () => {
		try {
			resolveCpkGraph([manifest("a", []), manifest("a", [])]);
		} catch (err) {
			expect(err).toBeInstanceOf(CpkGraphError);
			expect((err as CpkGraphError).code).toBe("duplicate_id");
			return;
		}
		throw new Error("expected CpkGraphError");
	});
});
