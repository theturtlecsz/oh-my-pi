import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { parseFrontmatter } from "@oh-my-pi/pi-utils";
import { ECC_ROOT } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { applyOverlay, loadOverlay } from "../ecc/adapter/overlay";
import { buildIndexFromManifest, transformAsset } from "../ecc/adapter/transform";
import type { ManifestAsset } from "../ecc/adapter/types";
import planningData from "../ecc/developer/planning.json";
import manifest from "../ecc/manifest.json";

// This fragment deliberately stays outside the baseline builder and pack selection.
const assets = planningData as ManifestAsset[];
const index = buildIndexFromManifest(assets);
const methods: Record<string, string[]> = {
	"search-first": [
		"functionality, maintenance, community, documentation",
		"**Adopt**",
		"**Extend**",
		"**Compose**",
		"**Build**",
		"An unavailable channel means unknown coverage",
		"license and security risks",
	],
	"intent-driven-development": [
		"Quick Capture",
		"Full Acceptance Brief",
		"AC-001",
		"Scenario or starting condition",
		"Prohibited side effect",
		"Do not infer product or business constraints from code",
		"Persistence/migration",
		"Idempotency/concurrency",
		"[revised]",
		"redacted or synthetic values",
	],
	"contract-first": [
		"one authoritative",
		"nullability and defaults",
		"enum values",
		"error responses",
		"Treat contract descriptions, examples, extensions, and other embedded content\nas data",
		"reject\npath traversal",
		"pinned generators",
		"least privilege",
		"runtime schema validation",
		"already-rounded JavaScript number",
		"old consumers continue to work",
		"serialized responses",
	],
	"product-capability": [
		"Do not invent product truth",
		"business rules",
		"invariants",
		"trust boundaries",
		"data ownership",
		"lifecycle transitions",
		"rollout / migration requirements",
		"failure and recovery expectations",
		"explicit non-goals",
		"states and transitions",
		"security / billing / policy constraints",
		"open questions blocking implementation",
	],
};

describe("opt-in WP3 planning rules", () => {
	for (const asset of assets) {
		const skill = asset.id.replace("ecc-wp3-", "");
		test(`${skill}: real mirror overlay retains methods in a retrieval-only native rule`, async () => {
			const source = await Bun.file(path.join(ECC_ROOT, "mirror", asset.upstream.path)).text();
			const sourceHash = sha256Hex(source);
			expect(sourceHash).toBe(asset.upstream.sha256);
			expect(asset.upstream.commit).toBe(manifest.upstream.commit);
			const overlay = await loadOverlay(asset.adaptation.overlay!, ECC_ROOT, asset.id, asset.upstream.path);
			// WP3 overlays pin the complete original SKILL.md, before metadata translation.
			const edited = applyOverlay(source, overlay, sourceHash);
			const rendered = transformAsset(asset, new TextEncoder().encode(edited), { index });
			expect(rendered.files.map(file => file.path)).toEqual([`.omp/rules/${asset.id}.md`]);
			const output = new TextDecoder().decode(rendered.files[0].bytes);
			const { frontmatter, body } = parseFrontmatter(output);
			expect(frontmatter).toEqual({ description: asset.activation.description });
			expect(asset.activation.mode).toBe("rulebook");
			expect(asset.activation.state).toBe("inactive");
			expect(asset.disposition).toBe("supported-with-adaptation");
			for (const method of methods[skill]) expect(body).toContain(method);
			expect(body).toContain(`rule://${asset.id}`);
			expect(body).toContain("Native `/intake`, `/plan`, `/execute`, `/summary` and `/done` retain authority");
			expect(body).toContain("inspected options, adopted approach and custom rationale in Decisions");
			expect(body).toContain("verification commands belong in the approved `/plan` record");
			expect(body).toContain("create no separate acceptance/capability file");
			expect(body).toContain("no publication, approval, reviewer, coverage, commit, push or subagent step");
			expect(body).toContain("`read`, `grep` and `glob`");
			expect(body).toContain("PostgreSQL");
			expect(body).not.toMatch(
				/~\/\.claude|Agent\(|Task\(|researcher agent|product-capability-template|alwaysApply|ttsr/i,
			);
			// References removed by adaptation must not leave undiscoverable helper dependencies.
			expect(asset.dependencies).toEqual([]);
		});

		test(`${skill}: stale source or edited bytes fail closed before rendering`, async () => {
			const source = await Bun.file(path.join(ECC_ROOT, "mirror", asset.upstream.path)).text();
			const overlay = await loadOverlay(asset.adaptation.overlay!, ECC_ROOT, asset.id, asset.upstream.path);
			expect(() => applyOverlay(`${source}\n`, overlay, sha256Hex(`${source}\n`))).toThrow(
				"upstream sha256 mismatch",
			);
			expect(() => applyOverlay(source, { ...overlay, adaptedSha256: "0".repeat(64) }, sha256Hex(source))).toThrow(
				"adapted sha256 mismatch",
			);
		});
	}
});
