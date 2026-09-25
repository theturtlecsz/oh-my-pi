import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import {
	CpkManifestError,
	type CpkManifestErrorCode,
	canonicalizeCpkManifest,
	contentAddress,
	parseCpkManifest,
} from "../src/extensibility/cpk/manifest";

const fixturePath = path.resolve(import.meta.dir, "fixtures/cpk/manifests.json");

function captureManifestError(fn: () => unknown): CpkManifestError {
	try {
		fn();
	} catch (err) {
		if (err instanceof CpkManifestError) return err;
		throw err;
	}
	throw new Error("expected CpkManifestError");
}

function expectCode(fn: () => unknown, code: CpkManifestErrorCode): void {
	expect(captureManifestError(fn).code).toBe(code);
}

const base = {
	schema: "cpk1/v1",
	id: "demo",
	version: "1.0.0",
	provides: ["b", "a", "a"],
	requires: [],
	effects: ["read"],
	scopes: ["session", "install", "session"],
};

describe("CPK-1 manifest canonicalization (OMP-205)", () => {
	it("sorts and dedupes arrays, ordering scopes by scope order rather than lexicographically", () => {
		const manifest = parseCpkManifest(base);
		expect(manifest.provides).toEqual(["a", "b"]);
		expect(manifest.scopes).toEqual(["install", "session"]);
	});

	it("produces identical canonical JSON and content address for manifests differing only in array order", () => {
		const first = parseCpkManifest({ ...base, provides: ["a", "b"], scopes: ["install", "session"] });
		const second = parseCpkManifest({ ...base, provides: ["b", "a"], scopes: ["session", "install"] });
		expect(canonicalizeCpkManifest(first)).toBe(canonicalizeCpkManifest(second));
		expect(contentAddress(first)).toBe(contentAddress(second));
	});

	it("changes the content address when a semantic field changes", () => {
		const original = parseCpkManifest(base);
		const bumped = parseCpkManifest({ ...base, version: "1.0.1" });
		expect(contentAddress(bumped)).not.toBe(contentAddress(original));
	});

	it("rejects malformed input with a stable error code", () => {
		expectCode(() => parseCpkManifest(null), "not_object");
		expectCode(() => parseCpkManifest([]), "not_object");
		expectCode(() => parseCpkManifest({ ...base, schema: "cpk1/v2" }), "invalid_schema");
		expectCode(() => parseCpkManifest({ ...base, id: "" }), "invalid_field");
		expectCode(() => parseCpkManifest({ ...base, provides: "not-an-array" }), "invalid_field");
		expectCode(() => parseCpkManifest({ ...base, effects: ["read", 7] }), "invalid_field");
		expectCode(() => parseCpkManifest({ ...base, scopes: ["bogus"] }), "invalid_scope");
	});

	it("parses the committed fixture into canonical manifests", async () => {
		const raw = (await Bun.file(fixturePath).json()) as unknown[];
		const manifests = raw.map(parseCpkManifest);
		expect(manifests.map(manifest => manifest.id).sort()).toEqual(["audit", "core", "search", "writer"]);
		for (const manifest of manifests) {
			expect(manifest.schema).toBe("cpk1/v1");
			expect(contentAddress(manifest)).toMatch(/^sha256:[0-9a-f]{64}$/);
		}
	});
});
