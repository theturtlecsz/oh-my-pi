import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { type CpkManifest, parseCpkManifest } from "../src/extensibility/cpk/manifest";
import { composeCpk, proveShadowIdentity, runCpkShadow } from "../src/extensibility/cpk/shadow";

const fixturePath = path.resolve(import.meta.dir, "fixtures/cpk/manifests.json");

async function loadFixture(): Promise<CpkManifest[]> {
	const raw = (await Bun.file(fixturePath).json()) as unknown[];
	return raw.map(parseCpkManifest);
}

describe("CPK-1/CPK-2 shadow composition (OMP-205)", () => {
	it("returns the live composition by reference and reports identity when disabled", async () => {
		const manifests = await loadFixture();
		const live = composeCpk(manifests, ["core", "search"]);
		const result = runCpkShadow(live, manifests, ["core", "search", "writer"]);
		expect(result.enabled).toBe(false);
		expect(result.live).toBe(live);
		expect(result.shadow).toBe(live);
		expect(result.identical).toBe(true);
		expect(result.differences).toEqual([]);
	});

	it("proves an empty selection composes to the live composition", async () => {
		const manifests = await loadFixture();
		const live = composeCpk([], []);
		const result = proveShadowIdentity(live, manifests);
		expect(result.enabled).toBe(true);
		expect(result.shadow).toEqual(live);
		expect(result.identical).toBe(true);
		expect(result.differences).toEqual([]);
	});

	it("reports the diverging fields when the shadow selection differs", async () => {
		const manifests = await loadFixture();
		const live = composeCpk(manifests, ["core"]);
		const result = runCpkShadow(live, manifests, ["core", "search"], { enabled: true });
		expect(result.identical).toBe(false);
		expect(result.differences).toContain("plugins");
		expect(result.differences).toContain("effects");
		expect(result.shadow.plugins).toEqual(["core", "search"]);
	});

	it("composes independently of manifest and selection order", async () => {
		const manifests = await loadFixture();
		const forward = composeCpk(manifests, ["core", "search", "writer"]);
		const reversed = composeCpk([...manifests].reverse(), ["writer", "search", "core"]);
		expect(reversed).toEqual(forward);
	});
});
