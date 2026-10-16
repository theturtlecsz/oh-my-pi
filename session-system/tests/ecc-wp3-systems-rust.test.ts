import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { sha256Hex } from "../ecc/adapter/hash";
import { applyOverlay, loadOverlay } from "../ecc/adapter/overlay";
import { buildIndexFromManifest, transformAsset } from "../ecc/adapter/transform";
import type { ManifestAsset } from "../ecc/adapter/types";

const eccRoot = path.resolve(import.meta.dir, "../ecc");
const commit = "8321021c54d670126ce3b2969d5deb880b4b0c2a";
const stems = ["coding-style", "patterns", "testing", "security"] as const;
type Stem = (typeof stems)[number];

function makeAsset(id: string, upstreamPath: string, sha256: string, overlay?: string): ManifestAsset {
	return {
		id,
		pack: "engineering",
		upstream: { repository: "https://github.com/affaan-m/ECC.git", commit, path: upstreamPath, sha256 },
		target: { path: `.omp/rules/${id}.md`, scope: "project" },
		adaptation: { kind: "rule-metadata", transformVersion: 1, ...(overlay ? { overlay } : {}) },
		activation: { state: "inactive", mode: "rulebook", description: `Rust ${id}`, globs: ["**/*.rs"] },
		dependencies: overlay ? [`ecc-wp3-common-${upstreamPath.split("/")[2].replace(".md", "")}`] : [],
		disposition: "supported-with-adaptation",
		reason: "Rust rule fixture",
		qualification: { mechanicalTests: [], behavioralTrial: null, qualifiedRelease: null },
		rollback: { removeOwnedArtifactOnly: true },
	};
}

async function mirror(upstreamPath: string): Promise<string> {
	return Bun.file(path.join(eccRoot, "mirror", upstreamPath)).text();
}

const commons = stems.map(stem => makeAsset(`ecc-wp3-common-${stem}`, `rules/common/${stem}.md`, ""));
const rustAssets = await Promise.all(
	stems.map(async stem =>
		makeAsset(
			`ecc-wp3-rust-${stem}`,
			`rules/rust/${stem}.md`,
			sha256Hex(await mirror(`rules/rust/${stem}.md`)),
			`developer/overlays/rust-${stem}.json`,
		),
	),
);
const index = buildIndexFromManifest([...rustAssets, ...commons]);

async function edited(stem: Stem): Promise<string> {
	const asset = rustAssets.find(a => a.id === `ecc-wp3-rust-${stem}`)!;
	const original = await mirror(asset.upstream.path);
	const overlay = await loadOverlay(asset.adaptation.overlay!, eccRoot, asset.id, asset.upstream.path);
	return applyOverlay(original, overlay, sha256Hex(original));
}

async function rendered(stem: Stem): Promise<string> {
	const asset = rustAssets.find(a => a.id === `ecc-wp3-rust-${stem}`)!;
	const result = transformAsset(asset, new TextEncoder().encode(await edited(stem)), { index });
	return new TextDecoder().decode(result.files[0].bytes);
}

describe("WP3 Rust native rules", () => {
	test("each overlay pins the full original in one edit and hashes pre-metadata source", async () => {
		for (const asset of rustAssets) {
			const original = await mirror(asset.upstream.path);
			const overlay = await loadOverlay(asset.adaptation.overlay!, eccRoot, asset.id, asset.upstream.path);
			expect(overlay.edits).toHaveLength(1);
			expect(overlay.edits[0].find.join("\n")).toBe(original);
			expect(overlay.upstreamSha256).toBe(sha256Hex(original));
			const stem = asset.upstream.path.split("/")[2].replace(".md", "") as Stem;
			const source = await edited(stem);
			expect(overlay.adaptedSha256).toBe(sha256Hex(source));
			expect(source).toContain(`](../common/${stem}.md)`);
			const stale = `${original}\nchanged upstream`;
			expect(() => applyOverlay(stale, overlay, sha256Hex(stale))).toThrow("upstream sha256 mismatch");
			expect(() => applyOverlay(stale, overlay, overlay.upstreamSha256)).toThrow("adapted sha256 mismatch");
		}
	});

	test("rendered rules link common rules natively and drop uninstalled skill references", async () => {
		for (const stem of stems) {
			const text = await rendered(stem);
			const links = [...text.matchAll(/\]\(([^)]+)\)/g)].map(m => m[1]);
			expect(links).toEqual([`ecc-wp3-common-${stem}.md`]);
			expect(text).not.toMatch(/See skill|`rust-patterns`|`rust-testing`|`security-review`|\.\.\/common/);
		}
	});

	test("rules retain idiom examples", async () => {
		const expectations: Record<Stem, string[]> = {
			"coding-style": ["Cow<'_, str>", "Cow::Borrowed(input)", "thiserror::Error", "with_context(|| format!", "impl Into<String>"],
			patterns: [
				"pub trait OrderRepository: Send + Sync",
				"struct UserId(u64)",
				"enum ConnectionState",
				"ServerConfigBuilder",
				"trait Format: private::Sealed",
			],
			testing: ["#[cfg(test)]", "#[rstest]", "proptest", "mockall::mock!", "#[tokio::test]"],
			security: [".bind(&name)", "Parse, don't validate", "// SAFETY:", "Prefer `safe` FFI wrappers", "PAYMENT_API_KEY"],
		};
		for (const stem of stems) {
			const text = await rendered(stem);
			for (const needle of expectations[stem]) expect(text).toContain(needle);
		}
	});

	test("operational gates are removed and OMP Rust tests use the project runner", async () => {
		const all = (await Promise.all(stems.map(rendered))).join("\n");
		expect(all).not.toMatch(/80%|--fail-under|always run `cargo fmt` before committing|-D warnings|Dependabot|cargo llvm-cov --html/);
		const testing = await rendered("testing");
		expect(testing).toContain("bun run test:rs");
		expect(testing).toContain("not direct `cargo test`");
		expect(testing).not.toMatch(/cargo test\s+# Run all tests|cargo test --doc/);
		expect(testing).toContain("Coverage reporting is optional");
		expect(all).not.toMatch(/\breviewer\b|\bapproval\b|git commit|git push|PostToolUse|settings\.json/i);
	});

	test("independent audit is optional and never required", async () => {
		for (const stem of stems) {
			const text = await rendered(stem);
			expect(text).not.toMatch(/(?:requires?|needs?|must (?:have|get)|mandatory)[^.\n]*audit/i);
		}
		for (const stem of ["patterns", "testing", "security"] as const) {
			expect(await rendered(stem)).toContain("independent native audit is explicitly optional");
		}
	});

	test("ownership routes to coding-style and unsafe invariants to security, never patterns", async () => {
		const style = await rendered("coding-style");
		const patterns = await rendered("patterns");
		const security = await rendered("security");
		expect(patterns).not.toMatch(/unsafe.*rule:\/\/ecc-wp3-rust-patterns|rule:\/\/ecc-wp3-rust-patterns[^.\n]*unsafe/);
		expect(patterns).toContain("`unsafe` and FFI invariants are in rule://ecc-wp3-rust-security");
		expect(patterns).toContain("Ownership and borrowing guidance is in rule://ecc-wp3-rust-coding-style");
		expect(style).toContain("`unsafe` invariants and FFI see rule://ecc-wp3-rust-security");
		expect(security).toContain("Ownership and borrowing guidance is in rule://ecc-wp3-rust-coding-style");
		expect(security).not.toContain("rule://ecc-wp3-rust-patterns");
	});
});
