import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { CpkManifestError, type CpkManifestErrorCode } from "../src/extensibility/cpk/manifest";
import {
	type CpkScopeCeiling,
	composeScopeEffects,
	parseCpkScopeCeiling,
	validateScopeClosure,
} from "../src/extensibility/cpk/scopes";

const fixturePath = path.resolve(import.meta.dir, "fixtures/cpk/scopes.json");

function ceiling(
	scope: CpkScopeCeiling["scope"],
	effects: string[],
	parent?: CpkScopeCeiling["scope"],
): CpkScopeCeiling {
	return { scope, effects, parent };
}

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

async function loadFixture(): Promise<CpkScopeCeiling[]> {
	const raw = (await Bun.file(fixturePath).json()) as unknown[];
	return raw.map(parseCpkScopeCeiling);
}

describe("CPK-2 closed effect scopes (OMP-205)", () => {
	it("accepts the committed nested fixture chain as closed", async () => {
		expect(validateScopeClosure(await loadFixture())).toEqual([]);
	});

	it("reports a child that widens beyond its parent, naming the offending effects", () => {
		const violations = validateScopeClosure([
			ceiling("work", ["read", "write"]),
			ceiling("session", ["read", "write", "exec"], "work"),
		]);
		expect(violations).toEqual([
			{
				code: "widening",
				scope: "session",
				effects: ["exec"],
				message: 'scope "session" widens beyond parent "work": exec',
			},
		]);
	});

	it("reports a missing parent and an inverted parent as structural violations", () => {
		const missing = validateScopeClosure([ceiling("work", ["read"], "project")]);
		expect(missing.map(violation => violation.code)).toEqual(["unknown_parent"]);

		const inverted = validateScopeClosure([ceiling("session", ["read"]), ceiling("project", ["read"], "session")]);
		expect(inverted.map(violation => violation.code)).toEqual(["inverted_parent"]);
	});

	it("narrows a request to the intersection of child and parent ceilings", () => {
		const composition = composeScopeEffects(
			ceiling("project", ["read", "write", "network"]),
			ceiling("work", ["read", "write"]),
			["read", "write", "network", "exec"],
		);
		expect(composition.granted).toEqual(["read", "write"]);
		expect(composition.refused).toEqual(["exec", "network"]);
		expect(composition.widening).toBe(true);
	});

	it("never grants beyond the parent even when the child ceiling is invalid", () => {
		const composition = composeScopeEffects(
			ceiling("work", ["read", "write"]),
			ceiling("session", ["read", "write", "exec"], "work"),
			["read", "write", "exec"],
		);
		expect(composition.granted).toEqual(["read", "write"]);
		expect(composition.refused).toEqual(["exec"]);
	});

	it("rejects malformed ceilings with a stable error code", () => {
		expectCode(() => parseCpkScopeCeiling(null), "not_object");
		expectCode(() => parseCpkScopeCeiling({ scope: "bogus", effects: [] }), "invalid_scope");
		expectCode(() => parseCpkScopeCeiling({ scope: "work", parent: "bogus", effects: [] }), "invalid_scope");
		expectCode(() => parseCpkScopeCeiling({ scope: "work", effects: "read" }), "invalid_field");
	});
});
