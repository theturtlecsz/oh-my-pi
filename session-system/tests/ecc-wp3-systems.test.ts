import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { sha256Hex } from "../ecc/adapter/hash";
import { applyOverlay, loadOverlay } from "../ecc/adapter/overlay";
import { buildIndexFromManifest, transformAsset } from "../ecc/adapter/transform";
import type { ManifestAsset } from "../ecc/adapter/types";
import manifest from "../ecc/manifest.json";

const eccRoot = path.resolve(import.meta.dir, "../ecc");
const assets: ManifestAsset[] = await Bun.file(path.join(eccRoot, "developer/systems.json")).json();

// Index solely from systems.json: the fragment is a self-contained closure, so
// a rendered rule can only resolve links to a sibling listed here.
const index = buildIndexFromManifest(assets);

const LANGS = ["common", "rust", "typescript"] as const;
const STEMS = ["coding-style", "patterns", "testing", "security"] as const;
type Lang = (typeof LANGS)[number];
type Stem = (typeof STEMS)[number];

/** Actual included rule:// targets and mirror-relative links, derived from the pinned overlays. */
const EXPECTED_DEPENDENCIES: Record<Lang, Record<Stem, string[]>> = {
	common: { "coding-style": [], patterns: [], testing: [], security: [] },
	rust: {
		"coding-style": ["ecc-wp3-common-coding-style", "ecc-wp3-rust-patterns", "ecc-wp3-rust-security"],
		patterns: ["ecc-wp3-common-patterns", "ecc-wp3-rust-coding-style", "ecc-wp3-rust-security"],
		testing: ["ecc-wp3-common-testing", "ecc-wp3-rust-patterns"],
		security: ["ecc-wp3-common-security", "ecc-wp3-rust-coding-style"],
	},
	typescript: {
		"coding-style": ["ecc-wp3-common-coding-style"],
		patterns: ["ecc-wp3-common-patterns"],
		testing: ["ecc-wp3-common-testing"],
		security: ["ecc-wp3-common-security"],
	},
};

/** Stems whose upstream file is already compliant and carry no overlay. */
const OVERLAYLESS = new Set(["common:coding-style", "typescript:patterns"]);

const byId = new Map(assets.map(asset => [asset.id, asset]));

function assetFor(lang: Lang, stem: Stem): ManifestAsset {
	const asset = byId.get(`ecc-wp3-${lang}-${stem}`);
	if (!asset) throw new Error(`Missing system rule ${lang}/${stem}`);
	return asset;
}

async function render(asset: ManifestAsset): Promise<string> {
	const original = await Bun.file(path.join(eccRoot, "mirror", asset.upstream.path)).text();
	expect(sha256Hex(original)).toBe(asset.upstream.sha256);
	const edited = asset.adaptation.overlay
		? applyOverlay(
				original,
				await loadOverlay(asset.adaptation.overlay, eccRoot, asset.id, asset.upstream.path),
				sha256Hex(original),
			)
		: original;
	const result = transformAsset(asset, new TextEncoder().encode(edited), { index });
	return new TextDecoder().decode(result.files[0].bytes);
}

function links(text: string): string[] {
	return [...text.matchAll(/\]\(([^)]+)\)/g)].map(match => match[1]);
}

function ruleRefs(text: string): string[] {
	const body = text.replace(/^---\n[\s\S]*?\n---\n/, "");
	return [...body.matchAll(/rule:\/\/([a-z0-9-]+)/g)].map(match => match[1]);
}

describe("WP3 common, Rust and TypeScript native systems", () => {
	test("declares twelve pinned project rulebook assets for common, Rust and TypeScript", () => {
		expect(assets.map(asset => asset.id)).toEqual(
			LANGS.flatMap(lang => STEMS.map(stem => `ecc-wp3-${lang}-${stem}`)),
		);
		for (const lang of LANGS) {
			for (const stem of STEMS) {
				const asset = assetFor(lang, stem);
				expect(asset.pack).toBe("engineering");
				expect(asset.target).toEqual({ path: `.omp/rules/${asset.id}.md`, scope: "project" });
				expect(asset.adaptation.kind).toBe("rule-metadata");
				expect(asset.adaptation.transformVersion).toBe(1);
				expect(asset.disposition).toBe("supported-with-adaptation");
				expect(asset.activation.state).toBe("inactive");
				expect(asset.activation.mode).toBe("rulebook");
				expect(asset.activation.description?.trim()).toBeTruthy();
				expect(asset.qualification).toEqual({
					mechanicalTests: ["session-system/tests/ecc-wp3-systems.test.ts"],
					behavioralTrial: null,
					qualifiedRelease: null,
				});
				expect(asset.rollback).toEqual({ removeOwnedArtifactOnly: true });
				// Baseline manifest repository/commit, and the exact mirrored bytes.
				expect(asset.upstream.repository).toBe(manifest.upstream.repository);
				expect(asset.upstream.commit).toBe(manifest.upstream.commit);
				expect(asset.upstream.path).toBe(`rules/${lang}/${stem}.md`);
			}
		}
	});

	test("common rules are description-only broad retrieval while Rust and TypeScript declare language globs", async () => {
		for (const lang of LANGS) {
			for (const stem of STEMS) {
				const asset = assetFor(lang, stem);
				if (lang === "common") {
					expect(asset.activation.globs).toBeUndefined();
				} else if (lang === "rust") {
					expect(asset.activation.globs).toEqual(["**/*.rs"]);
				} else {
					expect(asset.activation.globs).toEqual(["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"]);
				}
				expect(asset.activation).not.toHaveProperty("alwaysApply");
				expect(asset.activation).not.toHaveProperty("ttsrTrigger");
				const text = await render(asset);
				// Globs select discovery; they never claim to force retrieval or suppress native TTSR.
				expect(text).not.toMatch(/alwaysApply|ttsr|forc\w+ retrieval|suppress\w* .{0,20}\bTTSR/i);
				// Frontmatter carries only description/globs for a rulebook rule.
				const frontmatter = text.split("\n---\n")[0];
				expect(frontmatter).toContain("description:");
				expect(frontmatter).not.toMatch(/alwaysApply|ttsr|mode:|state:/i);
			}
		}
	});

	test("overlay paths are ECC-root relative, cover the reviewed edits, and unchanged stems omit the overlay", () => {
		for (const lang of LANGS) {
			for (const stem of STEMS) {
				const asset = assetFor(lang, stem);
				const overlay = asset.adaptation.overlay;
				if (OVERLAYLESS.has(`${lang}:${stem}`)) {
					expect(overlay).toBeUndefined();
					continue;
				}
				expect(overlay).toBe(`developer/overlays/${lang}-${stem}.json`);
				expect(path.isAbsolute(overlay!)).toBe(false);
				expect(overlay!.startsWith("/")).toBe(false);
			}
		}
		// The three overlays earlier slices own are consumed as-is.
		for (const consumed of [
			"developer/overlays/common-patterns.json",
			"developer/overlays/rust-security.json",
			"developer/overlays/typescript-security.json",
		]) {
			expect(assets.some(asset => asset.adaptation.overlay === consumed)).toBe(true);
		}
	});

	test("rendered common links resolve by installed namespaced names and declared dependencies match", async () => {
		for (const lang of LANGS) {
			for (const stem of STEMS) {
				const asset = assetFor(lang, stem);
				expect(asset.dependencies).toEqual(EXPECTED_DEPENDENCIES[lang][stem]);
				expect(asset.dependencies).not.toContain(asset.id);
				const text = await render(asset);
				// A language rule extends its common peer by installed namespaced filename.
				const expectedLinks = lang === "common" ? [] : [`ecc-wp3-common-${stem}.md`];
				expect(links(text)).toEqual(expectedLinks);
				// Every rule:// target exists in this fragment and is declared.
				for (const target of ruleRefs(text)) {
					expect(byId.has(target)).toBe(true);
					expect(asset.dependencies).toContain(target);
				}
				// Declared rule:// dependencies are actually referenced by the rendered body.
				for (const dep of asset.dependencies.filter(
					dep => dep.startsWith("ecc-wp3-") && dep !== `ecc-wp3-common-${stem}`,
				)) {
					expect(ruleRefs(text)).toContain(dep);
				}
			}
		}
	});

	test("a missing referenced target fails reference closure instead of shipping", async () => {
		const withoutCommon = buildIndexFromManifest(assets.filter(asset => !asset.id.startsWith("ecc-wp3-common-")));
		for (const lang of ["rust", "typescript"] as const) {
			const asset = assetFor(lang, "coding-style");
			const overlay = await loadOverlay(asset.adaptation.overlay!, eccRoot, asset.id, asset.upstream.path);
			const source = await Bun.file(path.join(eccRoot, "mirror", asset.upstream.path)).text();
			const edited = applyOverlay(source, overlay, sha256Hex(source));
			expect(() => transformAsset(asset, new TextEncoder().encode(edited), { index: withoutCommon })).toThrow(
				/reference closure error/i,
			);
		}
	});

	test("retains each language method set without inherited coverage or reviewer gates", async () => {
		// Method-level survival only; the group tests own the detailed example checks.
		const retained: Record<Lang, Record<Stem, string[]>> = {
			common: {
				"coding-style": ["Immutability", "KISS", "DRY", "YAGNI", "validate at system boundaries"],
				patterns: ["Repository Pattern", "API Response Format", "existing repository patterns"],
				testing: ["RED/GREEN", "Arrange-Act-Assert", "Check test isolation"],
				security: ["NEVER hardcode secrets", "parameterized queries with bound values", "XSS prevention"],
			},
			rust: {
				"coding-style": ["Ownership and Borrowing", "Error Handling", "Iterators Over Loops"],
				patterns: [
					"Repository Pattern with Traits",
					"Service Layer",
					"Newtype Pattern",
					"Enum State Machines",
					"Builder Pattern",
					"Sealed Traits",
				],
				testing: [
					"Unit Test Pattern",
					"Parameterized Tests",
					"Async Tests",
					"Mocking with mockall",
					"bun run test:rs",
				],
				security: ["Secrets Management", "SQL Injection Prevention", "Input Validation", "Unsafe Code"],
			},
			typescript: {
				"coding-style": ["Types and Interfaces", "Immutability", "Error Handling", "Input Validation", "bun check"],
				patterns: ["API Response Format", "Custom Hooks Pattern", "Repository Pattern"],
				testing: ["E2E Testing", "rule://ecc-wp3-common-testing"],
				security: ["Secret Management", "rule://ecc-wp3-common-security"],
			},
		};
		// Mandate forms only: adapted text states the same topics as negations
		// ("adds no coverage threshold", "optional audit"), which must stay legal.
		const forbidden = [
			["numeric coverage target", /\b\d+\s*%|--fail-under/],
			["named reviewer agent", /\b(?:security-reviewer|code-reviewer|e2e-runner|tdd-guide|database-reviewer)\b/],
			[
				"required coverage, review or audit",
				/(?<!\b(?:no|not|never|without)\s)(?:(?:requires?|needs?|must|mandatory|enforce)\b[^.\n]{0,60}\b(?:coverage|reviewer|audit)|(?:coverage|reviewer|audit)\b[^.\n]{0,40}\b(?:required|mandatory)\b)/i,
			],
			[
				"blanket commit or hook gate",
				/\bMANDATORY\b|Test Types \(ALL required\)|Before ANY commit|(?:requires?|must|enforce)\b[^.\n]{0,40}\b(?:pre-commit|commit gate)\b/i,
			],
			["activation override", /alwaysApply|ttsr/i],
		] as const;
		for (const lang of LANGS) {
			for (const stem of STEMS) {
				const text = await render(assetFor(lang, stem));
				for (const method of retained[lang][stem]) expect(text).toContain(method);
				for (const [label, pattern] of forbidden) {
					const hit = text.match(pattern);
					expect(hit === null ? null : `${label}: ${hit[0]}`).toBeNull();
				}
			}
		}
		// The guard must catch a renamed required audit, not just the upstream name,
		// while leaving an explicitly optional audit legal.
		const requiredReview = forbidden.find(([label]) => label === "required coverage, review or audit")![1];
		expect(requiredReview.test("The workflow requires a fresh compliance audit before merge.")).toBe(true);
		expect(requiredReview.test("Coverage is required for every change.")).toBe(true);
		expect(requiredReview.test("Finding an issue does not require an independent audit.")).toBe(false);
		expect(requiredReview.test("An independent audit is explicitly optional. No coverage gate.")).toBe(false);
	});

	test("tampered source bytes, supplied hash and adapted hash all refuse output", async () => {
		for (const lang of LANGS) {
			for (const stem of STEMS) {
				const asset = assetFor(lang, stem);
				if (!asset.adaptation.overlay) continue;
				const original = await Bun.file(path.join(eccRoot, "mirror", asset.upstream.path)).text();
				const overlay = await loadOverlay(asset.adaptation.overlay, eccRoot, asset.id, asset.upstream.path);
				const stale = `${original}\ntampered upstream`;
				expect(() => applyOverlay(stale, overlay, sha256Hex(stale))).toThrow("upstream sha256 mismatch");
				expect(() => applyOverlay(stale, overlay, asset.upstream.sha256)).toThrow("adapted sha256 mismatch");
				// Reviewed original still refuses a spoofed adapted hash.
				expect(() =>
					applyOverlay(original, { ...overlay, adaptedSha256: "0".repeat(64) }, asset.upstream.sha256),
				).toThrow("adapted sha256 mismatch");
			}
		}
	});
});
