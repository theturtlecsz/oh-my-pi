import { describe, expect, it } from "bun:test";
import { parseCpkManifest } from "../src/extensibility/cpk/manifest";
import {
	CPK4_SCHEMA,
	CpkBootFrozenSession,
	CpkProfileError,
	type CpkProfileErrorCode,
	parseCpkProfile,
	resolveCpkBoot,
} from "../src/extensibility/cpk/profiles";
import manifestFixtures from "./fixtures/cpk/manifests.json";
import profileFixtures from "./fixtures/cpk/profiles.json";

function captureProfileError(fn: () => unknown): CpkProfileError {
	try {
		fn();
	} catch (err) {
		if (err instanceof CpkProfileError) return err;
		throw err;
	}
	throw new Error("expected CpkProfileError");
}

function expectCode(fn: () => unknown, code: CpkProfileErrorCode): void {
	expect(captureProfileError(fn).code).toBe(code);
}

const manifests = manifestFixtures.map(parseCpkManifest);

describe("CPK-4 boot-frozen qualified profiles (OMP-207)", () => {
	it("canonicalizes a valid profile and rejects malformed inputs with stable error codes", () => {
		const parsed = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "interactive",
			version: "1.0.0",
			requiredPlugins: ["core"],
			optionalPlugins: ["search"],
			allowedEffects: ["read", "network"],
			maxContextTokens: 5000,
			maxTools: 5,
		});
		expect(parsed.schema).toBe(CPK4_SCHEMA);
		expect(parsed.mode).toBe("interactive");
		expect(parsed.requiredPlugins).toEqual(["core"]);
		expect(parsed.optionalPlugins).toEqual(["search"]);
		expect(parsed.allowedEffects).toEqual(["network", "read"]);

		expectCode(() => parseCpkProfile(null), "not_object");
		expectCode(() => parseCpkProfile([]), "not_object");
		expectCode(() => parseCpkProfile({ schema: "cpk4/v2", mode: "interactive", version: "1" }), "invalid_schema");
		expectCode(
			() => parseCpkProfile({ schema: CPK4_SCHEMA, mode: "unsupported_mode", version: "1" }),
			"invalid_mode",
		);
		expectCode(
			() =>
				parseCpkProfile({
					schema: CPK4_SCHEMA,
					mode: "interactive",
					version: "",
					maxContextTokens: 1000,
					maxTools: 2,
				}),
			"invalid_field",
		);
		expectCode(
			() =>
				parseCpkProfile({
					schema: CPK4_SCHEMA,
					mode: "interactive",
					version: "1.0.0",
					maxContextTokens: -1,
					maxTools: 2,
				}),
			"invalid_budget",
		);
	});

	it("resolves deterministically: identical inputs produce identical graph digests regardless of manifest ordering", () => {
		const profile = parseCpkProfile(profileFixtures.find(p => p.mode === "interactive"));
		const reversed = [...manifests].reverse();

		const res1 = resolveCpkBoot(profile, manifests);
		const res2 = resolveCpkBoot(profile, reversed);

		expect(res1.graphDigest).toBe(res2.graphDigest);
		expect(res1.selectedPlugins).toEqual(res2.selectedPlugins);
		expect(res1.effectiveEffects).toEqual(res2.effectiveEffects);
		expect(res1.contextUsage).toEqual(res2.contextUsage);
		expect(res1.graphDigest.startsWith("sha256:")).toBe(true);
	});

	it("fails closed when a required plugin is missing from manifests", () => {
		const profile = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "plan",
			version: "1.0.0",
			requiredPlugins: ["non_existent_plugin"],
			optionalPlugins: [],
			allowedEffects: ["read"],
			maxContextTokens: 4000,
			maxTools: 4,
		});

		expectCode(() => resolveCpkBoot(profile, manifests), "required_plugin_missing");
	});

	it("fails closed when a required plugin has a cyclic dependency or unsatisfied requirement", () => {
		const cyclicManifests = [
			parseCpkManifest({
				schema: "cpk1/v1",
				id: "plug-a",
				version: "1.0.0",
				provides: ["a"],
				requires: ["plug-b"],
				effects: ["read"],
				scopes: ["agent"],
			}),
			parseCpkManifest({
				schema: "cpk1/v1",
				id: "plug-b",
				version: "1.0.0",
				provides: ["b"],
				requires: ["plug-a"],
				effects: ["read"],
				scopes: ["agent"],
			}),
		];

		const profile = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "implementation",
			version: "1.0.0",
			requiredPlugins: ["plug-a"],
			optionalPlugins: [],
			allowedEffects: ["read"],
			maxContextTokens: 4000,
			maxTools: 4,
		});

		expectCode(() => resolveCpkBoot(profile, cyclicManifests), "sealed_conflict");
	});

	it("degrades cleanly when optional plugins are missing or conflicted without breaking required plugins", () => {
		const profile = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "plan",
			version: "1.0.0",
			requiredPlugins: ["core"],
			optionalPlugins: ["missing-opt", "search"],
			allowedEffects: ["read", "network"],
			maxContextTokens: 5000,
			maxTools: 5,
		});

		const res = resolveCpkBoot(profile, manifests);
		expect(res.selectedPlugins).toContain("core");
		expect(res.selectedPlugins).toContain("search");
		expect(res.selectedPlugins).not.toContain("missing-opt");

		const missingExp = res.explanations.find(e => e.id === "missing-opt");
		expect(missingExp).toBeDefined();
		expect(missingExp?.status).toBe("unavailable");
	});

	it("narrows plugin effects to profile ceiling and explains downgrades", () => {
		const profile = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "audit",
			version: "1.0.0",
			requiredPlugins: ["core"],
			optionalPlugins: ["search"],
			// "search" requests ["network", "read"], but allowed is only ["read"]
			allowedEffects: ["read"],
			maxContextTokens: 5000,
			maxTools: 5,
		});

		const res = resolveCpkBoot(profile, manifests);
		expect(res.effectiveEffects).toEqual(["read"]);
		expect(res.effectiveEffects).not.toContain("network");

		const searchExp = res.explanations.find(e => e.id === "search");
		expect(searchExp?.status).toBe("downgraded");
		expect(searchExp?.reason).toContain("effects narrowed");
	});

	it("enforces context token and tool budgets strictly", () => {
		const lowTokenProfile = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "diagnostic",
			version: "1.0.0",
			requiredPlugins: ["core", "audit"],
			optionalPlugins: [],
			allowedEffects: ["read"],
			maxContextTokens: 50, // Insufficient budget
			maxTools: 10,
		});

		expectCode(() => resolveCpkBoot(lowTokenProfile, manifests), "budget_exceeded");

		const lowToolProfile = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "diagnostic",
			version: "1.0.0",
			requiredPlugins: ["core", "search", "writer"], // search and writer provide tools (total 2)
			optionalPlugins: [],
			allowedEffects: ["read", "write", "network"],
			maxContextTokens: 10000,
			maxTools: 1, // insufficient tools budget (total is 2)
		});

		expectCode(() => resolveCpkBoot(lowToolProfile, manifests), "budget_exceeded");
	});

	it("freezes authority and tools in CpkBootFrozenSession and rejects live hot reloads", () => {
		const profile = parseCpkProfile(profileFixtures.find(p => p.mode === "interactive"));
		const resolution = resolveCpkBoot(profile, manifests);
		const session = new CpkBootFrozenSession(resolution, "session-test-1");

		expect(session.isFrozen).toBe(true);
		expect(session.graphDigest).toBe(resolution.graphDigest);
		expect(session.selectedPlugins).toEqual(resolution.selectedPlugins);

		expectCode(() => session.hotReloadAuthority(["extra-plugin"]), "hot_reload_forbidden");
		expectCode(() => session.hotReloadTools(["new_tool"]), "hot_reload_forbidden");
	});

	it("permits isolated cosmetic reloads while preserving the frozen graph digest", () => {
		const profile = parseCpkProfile(profileFixtures.find(p => p.mode === "interactive"));
		const resolution = resolveCpkBoot(profile, manifests);
		const session = new CpkBootFrozenSession(resolution, "session-test-2");

		const initialDigest = session.graphDigest;
		const reloadResult = session.reloadCosmetic({ theme: "dracula", font: "mono" });

		expect(reloadResult.cosmeticVersion).toBe(2);
		expect(reloadResult.graphDigest).toBe(initialDigest);
		expect(reloadResult.authorityPreserved).toBe(true);
		expect(session.cosmeticState).toEqual({ theme: "dracula", font: "mono" });
	});

	it("supports single-switch rollback to legacy profile while preserving session state", () => {
		const profile = parseCpkProfile(profileFixtures.find(p => p.mode === "interactive"));
		const resolution = resolveCpkBoot(profile, manifests);
		const session = new CpkBootFrozenSession(resolution, "session-test-3");

		const rollback = session.rollbackToLegacy(true);
		expect(rollback.legacyActive).toBe(true);
		expect(rollback.sessionPreserved).toBe(true);
		expect(session.isLegacyActive).toBe(true);
		expect(session.isFrozen).toBe(false);
		expect(session.sessionId).toBe("session-test-3");
	});

	it("fails closed when a required plugin has a transitive dependency dropped from optional pruning", () => {
		const customManifests = [
			parseCpkManifest({
				schema: "cpk1/v1",
				id: "plug-main",
				version: "1.0.0",
				provides: ["main"],
				requires: ["plug-opt-helper"],
				effects: ["read"],
				scopes: ["agent"],
			}),
			parseCpkManifest({
				schema: "cpk1/v1",
				id: "plug-opt-helper",
				version: "1.0.0",
				provides: ["helper"],
				requires: ["ghost-missing"],
				effects: ["read"],
				scopes: ["agent"],
			}),
		];

		const profile = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "implementation",
			version: "1.0.0",
			requiredPlugins: ["plug-main"],
			optionalPlugins: ["plug-opt-helper"],
			allowedEffects: ["read"],
			maxContextTokens: 4000,
			maxTools: 4,
		});

		// Must fail closed with sealed_conflict because required plug-main cannot satisfy plug-opt-helper
		expectCode(() => resolveCpkBoot(profile, customManifests), "sealed_conflict");
	});

	it("enforces immutability of frozen capability sets and prevents smuggled modifications", () => {
		const profile = parseCpkProfile(profileFixtures.find(p => p.mode === "interactive"));
		const resolution = resolveCpkBoot(profile, manifests);
		const session = new CpkBootFrozenSession(resolution, "session-freeze-test");

		expect(Object.isFrozen(session.selectedPlugins)).toBe(true);
		expect(Object.isFrozen(session.effectiveEffects)).toBe(true);
		expect(() => {
			(session.selectedPlugins as string[]).push("smuggled");
		}).toThrow();
		expect(session.selectedPlugins).toEqual(resolution.selectedPlugins);
	});

	it("enforces cosmeticReloadable: false and rejects cosmetic reloads when disallowed or with authoritative keys", () => {
		const planProfile = parseCpkProfile(profileFixtures.find(p => p.mode === "plan"));
		expect(planProfile.cosmeticReloadable).toBe(false);

		const resPlan = resolveCpkBoot(planProfile, manifests);
		const planSession = new CpkBootFrozenSession(resPlan, "session-plan-1");

		expectCode(() => planSession.reloadCosmetic({ theme: "dark" }), "hot_reload_forbidden");

		const interactiveProfile = parseCpkProfile(profileFixtures.find(p => p.mode === "interactive"));
		const resInteractive = resolveCpkBoot(interactiveProfile, manifests);
		const interactiveSession = new CpkBootFrozenSession(resInteractive, "session-interactive-1");

		expectCode(() => interactiveSession.reloadCosmetic({ tools: "new-tools" }), "hot_reload_forbidden");
		expectCode(() => interactiveSession.reloadCosmetic({ plugins: "new-plugins" }), "hot_reload_forbidden");
	});

	it("rejects unknown profile fields and incompatible engine version requirements", () => {
		expectCode(
			() =>
				parseCpkProfile({
					schema: CPK4_SCHEMA,
					mode: "interactive",
					version: "1.0.0",
					maxContextTokens: 1000,
					maxTools: 2,
					unknownField: "bogus",
				}),
			"invalid_field",
		);

		expectCode(
			() =>
				parseCpkProfile({
					schema: CPK4_SCHEMA,
					mode: "interactive",
					version: "1.0.0",
					maxContextTokens: 1000,
					maxTools: 2,
					minEngineVersion: "99.0.0",
				}),
			"compatibility_failure",
		);
	});

	it("validates all 8 qualified operating modes from fixture", () => {
		const modes = ["interactive", "intake", "plan", "implementation", "audit", "summary", "fleet", "diagnostic"];
		expect(profileFixtures.length).toBe(8);
		for (const mode of modes) {
			const found = profileFixtures.find(p => p.mode === mode);
			expect(found).toBeDefined();
			const parsed = parseCpkProfile(found);
			expect(parsed.mode).toBe(mode as typeof parsed.mode);
		}
	});

	it("enforces first-tool performance latency budget when specified", () => {
		const profile = parseCpkProfile({
			schema: CPK4_SCHEMA,
			mode: "diagnostic",
			version: "1.0.0",
			requiredPlugins: ["core"],
			optionalPlugins: [],
			allowedEffects: ["read"],
			maxContextTokens: 5000,
			maxTools: 5,
			firstToolBudgetMs: 0.000001, // Impossibly tight budget
		});

		expectCode(() => resolveCpkBoot(profile, manifests), "budget_exceeded");
	});
});
