import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { parseFrontmatter, setAgentDir } from "@oh-my-pi/pi-utils";
import { clearCache } from "../../packages/coding-agent/src/capability/fs";
import { type Rule, ruleCapability } from "../../packages/coding-agent/src/capability/rule";
import { type Skill, skillCapability } from "../../packages/coding-agent/src/capability/skill";
import { loadCapability } from "../../packages/coding-agent/src/discovery";
import lockData from "../ecc/adapted.lock.json";
import { buildPlan } from "../ecc/adapter/apply";
import {
	assetsForPack,
	ECC_ROOT,
	installablePackedAssets,
	isInstallable,
	loadManifest,
	readAssetSource,
	readMirrorFile,
	validateManifest,
} from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { serializeLock } from "../ecc/adapter/lock";
import { applyOverlay, loadOverlay, type Overlay, validateOverlay } from "../ecc/adapter/overlay";
import { buildIndexFromManifest, transformAsset, verifyReferenceClosure } from "../ecc/adapter/transform";
import type { AdaptedLock, Manifest, ManifestAsset } from "../ecc/adapter/types";
import manifestData from "../ecc/manifest.json";

const manifest = manifestData as unknown as Manifest;
const committedLock = lockData as unknown as AdaptedLock;

const tempDirs: string[] = [];
let originalAgentDir: string | undefined;

beforeEach(() => {
	originalAgentDir = process.env.PI_CODING_AGENT_DIR;
	clearCache();
});

afterEach(async () => {
	if (originalAgentDir !== undefined) setAgentDir(originalAgentDir);
	else delete process.env.PI_CODING_AGENT_DIR;
	clearCache();
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
});

async function makeTempDir(prefix: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

function assetById(id: string): ManifestAsset {
	const asset = manifest.assets.find(a => a.id === id);
	if (!asset) throw new Error(`test references unknown asset '${id}'`);
	return asset;
}

async function buildAll(): Promise<AdaptedLock> {
	const loaded = await loadManifest(ECC_ROOT);
	const assets = installablePackedAssets(loaded);
	const plan = await buildPlan({ eccRoot: ECC_ROOT, projectRoot: ECC_ROOT }, loaded, assets);
	return {
		schemaVersion: 1,
		transformVersion: loaded.transformVersion,
		upstream: loaded.upstream,
		assets: plan.assets,
	};
}

describe("pinned mirror: source hashes recorded and verifiable", () => {
	test("every installable asset's mirror bytes hash to its manifest pin", async () => {
		const loaded = await loadManifest(ECC_ROOT);
		const assets = installablePackedAssets(loaded);
		expect(assets.length).toBeGreaterThan(0);
		for (const asset of assets) {
			const bytes = await readAssetSource(ECC_ROOT, asset);
			expect(sha256Hex(bytes)).toBe(asset.upstream.sha256);
		}
	});

	test("lock records source and transformed hashes per asset, matching the committed lock", async () => {
		const rebuilt = await buildAll();
		expect(rebuilt.schemaVersion).toBe(committedLock.schemaVersion);
		expect(rebuilt.upstream.commit).toBe(committedLock.upstream.commit);
		expect(rebuilt.upstream.tree).toBe(committedLock.upstream.tree);
		expect(rebuilt.assets).toEqual(committedLock.assets);

		for (const asset of rebuilt.assets) {
			expect(asset.upstreamSha256).toMatch(/^[0-9a-f]{64}$/);
			expect(asset.adaptedSha256).toMatch(/^[0-9a-f]{64}$/);
			expect(asset.files.length).toBeGreaterThan(0);
			// The adapted sha is the primary file's sha, not a copy of the source.
			expect(asset.files.some(file => file.sha256 === asset.adaptedSha256)).toBe(true);
		}
	});

	test("two builds from the same mirror serialize byte-identically", async () => {
		const first = serializeLock(await buildAll());
		const second = serializeLock(await buildAll());
		expect(first).toBe(second);
	});
});

describe("manifest invariants", () => {
	test("well-formed manifest validates and rejects a colliding target", () => {
		expect(() => validateManifest(manifest)).not.toThrow();

		const colliding: Manifest = structuredClone(manifest);
		colliding.assets.push({
			...structuredClone(assetById("ecc-common-coding-style")),
			id: "ecc-collision",
		});
		expect(() => validateManifest(colliding)).toThrow(/target collision/);
	});

	test("a pack referencing an unknown asset id fails validation", () => {
		const broken: Manifest = structuredClone(manifest);
		broken.packs[0].assetIds = [...broken.packs[0].assetIds, "ecc-does-not-exist"];
		expect(() => validateManifest(broken)).toThrow(/references unknown asset 'ecc-does-not-exist'/);
	});
});

describe("dispositions gate installation", () => {
	test("reference-only, deferred, and excluded never install", () => {
		for (const disposition of ["reference-only", "deferred", "excluded"] as const) {
			expect(isInstallable({ ...assetById("ecc-common-coding-style"), disposition })).toBe(false);
		}
		for (const disposition of ["supported-without-changes", "supported-with-adaptation"] as const) {
			expect(isInstallable({ ...assetById("ecc-common-coding-style"), disposition })).toBe(true);
		}
	});

	test("the deferred harness optimizer is selected by no pack or install plan", async () => {
		const deferred = assetById("ecc-harness-optimizer");
		expect(deferred.disposition).toBe("deferred");
		expect(manifest.packs.some(pack => pack.assetIds.includes(deferred.id))).toBe(false);
		const assets = installablePackedAssets(manifest);
		expect(assets.some(asset => asset.id === deferred.id)).toBe(false);
	});

	test("no installable target basename claims a reserved native command name", () => {
		const reserved = new Set(manifest.nativeCommandNames);
		for (const asset of installablePackedAssets(manifest)) {
			const base = path.posix.basename(asset.target.path).replace(/\.(md|yml|yaml)$/, "");
			expect(reserved.has(base)).toBe(false);
		}
	});

	test("each pack selects only declared, installable assets", async () => {
		const loaded = await loadManifest(ECC_ROOT);
		for (const pack of loaded.packs) {
			const assets = assetsForPack(loaded, pack.id);
			expect(assets.length).toBeGreaterThan(0);
			expect(new Set(pack.assetIds).size).toBe(pack.assetIds.length);
			for (const asset of pack.assetIds) expect(loaded.assets.some(a => a.id === asset)).toBe(true);
		}
	});
});

describe("dependency and reference closure", () => {
	test("rule referencing an unmapped sibling fails closure by resolved path", async () => {
		const rust = assetById("ecc-rust-coding-style");
		const bytes = await readAssetSource(ECC_ROOT, rust);
		const onlyRust = buildIndexFromManifest([rust]);
		expect(() => transformAsset(rust, bytes, { index: onlyRust })).toThrow(
			/Reference closure error.*rules\/common\/coding-style\.md/,
		);
	});

	test("full index rewrites the relative link to the installed target and drops the upstream path", async () => {
		const rust = assetById("ecc-rust-coding-style");
		const bytes = await readAssetSource(ECC_ROOT, rust);
		const result = transformAsset(rust, bytes, { index: buildIndexFromManifest(manifest.assets) });
		const text = new TextDecoder().decode(result.files[0].bytes);
		expect(text).toContain("ecc-common-coding-style.md");
		expect(text).not.toContain("../common/coding-style.md");
	});

	test("skill install fails by name when a declared helper reference is absent", async () => {
		const skill = assetById("ecc-agent-self-evaluation");
		const bytes = await readAssetSource(ECC_ROOT, skill);
		const index = buildIndexFromManifest(manifest.assets);
		expect(() => transformAsset(skill, bytes, { index, helperFiles: new Map() })).toThrow(
			/Reference closure error: asset 'ecc-agent-self-evaluation' is missing dependency/,
		);
	});

	test("a dangling markdown reference in content fails closure by name", () => {
		const index = buildIndexFromManifest(manifest.assets);
		expect(() => verifyReferenceClosure("See [missing](nonexistent/file.md)", "rules/x.md", index)).toThrow(
			/Reference closure error: link 'nonexistent\/file.md' in rules\/x\.md resolves to uninstalled path/,
		);
	});

	test("skill helper files install verbatim with their mirrored bytes", async () => {
		const skill = assetById("ecc-agent-self-evaluation");
		const bytes = await readAssetSource(ECC_ROOT, skill);
		const helperFiles = new Map<string, Uint8Array>();
		for (const dep of skill.dependencies) {
			helperFiles.set(dep, await readMirrorFile(ECC_ROOT, path.posix.join(skill.upstream.path, dep)));
		}
		const result = transformAsset(skill, bytes, { index: buildIndexFromManifest(manifest.assets), helperFiles });
		expect(result.files.length).toBe(skill.dependencies.length + 1);
		for (const dep of skill.dependencies) {
			const planned = result.files.find(file => file.path.endsWith(dep));
			expect(planned).toBeDefined();
			expect(planned?.sha256).toBe(sha256Hex(helperFiles.get(dep)!));
		}
	});
});

describe("namespaced skills keep native identity and only rename the name line", () => {
	test("unoverlayed skill bodies are byte-identical upstream and namespaced by exactly one line", async () => {
		const asset = assetById("ecc-research-ops");
		expect(asset.adaptation.overlay).toBeUndefined();
		const upstreamBytes = await readAssetSource(ECC_ROOT, asset);
		const result = transformAsset(asset, upstreamBytes, { index: buildIndexFromManifest(manifest.assets) });
		const upstreamText = new TextDecoder().decode(upstreamBytes);
		const adaptedText = new TextDecoder().decode(result.files[0].bytes);

		const upstreamFm = parseFrontmatter(upstreamText, { source: asset.upstream.path });
		const adaptedFm = parseFrontmatter(adaptedText, { source: asset.target.path });
		expect(adaptedFm.frontmatter.name).toBe("ecc-research-ops");
		expect(adaptedFm.body).toBe(upstreamFm.body);
	});

	test("a skill whose upstream name differs from its directory is renamed by frontmatter name", async () => {
		const asset = assetById("ecc-scholar-evaluation");
		const upstreamBytes = await readAssetSource(ECC_ROOT, asset);
		const result = transformAsset(asset, upstreamBytes, { index: buildIndexFromManifest(manifest.assets) });
		const adaptedText = new TextDecoder().decode(result.files[0].bytes);
		expect(adaptedText).toContain("name: ecc-scholar-evaluation");
		expect(adaptedText).not.toContain("name: scholar-evaluation\n");
	});
});

describe("overlay drift refuses unreviewed output", () => {
	test("overlay pins its asset identity to the manifest upstream", async () => {
		for (const asset of manifest.assets) {
			if (!asset.adaptation.overlay) continue;
			const overlay = await loadOverlay(asset.adaptation.overlay, ECC_ROOT, asset.id, asset.upstream.path);
			expect(overlay.assetId).toBe(asset.id);
			expect(overlay.upstreamSha256).toBe(asset.upstream.sha256);
			// Skill overlays edit the whole SKILL.md, so the pinned adapted hash is
			// the installed file hash; the advisor overlay edits only the body and
			// is wrapped afterwards, so it is compared there instead.
			if (asset.adaptation.kind === "skill-namespaced") {
				const lockAsset = committedLock.assets.find(a => a.id === asset.id);
				expect(lockAsset).toBeDefined();
				expect(overlay.adaptedSha256).toBe(lockAsset!.adaptedSha256);
			}
		}
	});

	test("overlay application rejects stale upstream, missing find, ambiguous find, and stale adapted hash", async () => {
		const asset = assetById("ecc-search-first");
		const overlayPath = asset.adaptation.overlay;
		if (!overlayPath) throw new Error("ecc-search-first is expected to declare an overlay");
		const overlay = await loadOverlay(overlayPath, ECC_ROOT, asset.id, asset.upstream.path);
		const upstreamText = new TextDecoder().decode(await readAssetSource(ECC_ROOT, asset));

		expect(() => applyOverlay(upstreamText, overlay, "0".repeat(64))).toThrow(/upstream sha256 mismatch/);

		const missing: Overlay = {
			...overlay,
			edits: [{ reason: "gone", find: ["STRING THAT IS NOT PRESENT"], replace: ["x"] }],
		};
		expect(() => applyOverlay(upstreamText, missing, asset.upstream.sha256)).toThrow(/find block not found/);

		const ambiguous: Overlay = { ...overlay, edits: [{ reason: "too broad", find: ["the"], replace: ["x"] }] };
		expect(() => applyOverlay(upstreamText, ambiguous, asset.upstream.sha256)).toThrow(
			/find block matches multiple times/,
		);

		const stale: Overlay = { ...overlay, adaptedSha256: "0".repeat(64) };
		expect(() => applyOverlay(upstreamText, stale, asset.upstream.sha256)).toThrow(/adapted sha256 mismatch/);
	});

	test("overlay validation rejects a mismatched asset id and an escaping upstream path", () => {
		const base = {
			schemaVersion: 1,
			assetId: "ecc-search-first",
			upstreamPath: "skills/search-first/SKILL.md",
			upstreamSha256: "a".repeat(64),
			adaptedSha256: "b".repeat(64),
			edits: [{ reason: "r", find: ["a"], replace: ["b"] }],
		};
		expect(() => validateOverlay(base, "ecc-other", "skills/search-first/SKILL.md")).toThrow(/assetId mismatch/);
		expect(() => validateOverlay({ ...base, upstreamPath: "../escape.md" }, "ecc-search-first")).toThrow(
			/must be contained/,
		);
	});

	test("a transform whose overlay pins a stale upstream hash refuses to produce output", async () => {
		const asset = assetById("ecc-deep-research");
		const overlay = await loadOverlay(asset.adaptation.overlay!, ECC_ROOT, asset.id, asset.upstream.path);
		const upstreamText = new TextDecoder().decode(await readAssetSource(ECC_ROOT, asset));
		const staleOverlay: Overlay = {
			...overlay,
			upstreamSha256: "c".repeat(64),
		};
		expect(() => applyOverlay(upstreamText, staleOverlay, sha256Hex(upstreamText))).toThrow(
			/upstream sha256 mismatch/,
		);
	});
});

describe("database-reviewer adaptation drops execution authority", () => {
	test("the advisor target is a disabled read-only roster with a static policy", async () => {
		const asset = assetById("ecc-database-reviewer");
		const upstreamBytes = await readAssetSource(ECC_ROOT, asset);
		const index = buildIndexFromManifest(manifest.assets);

		// A database advisor profile cannot be produced without its safety overlay.
		expect(() => transformAsset(asset, upstreamBytes, { index })).toThrow(/requires a safety overlay/);

		const overlay = await loadOverlay(asset.adaptation.overlay!, ECC_ROOT, asset.id, asset.upstream.path);
		const policy = await Bun.file(path.join(ECC_ROOT, "advisor", "database-reviewer-policy.md")).text();
		const result = transformAsset(asset, upstreamBytes, { index, overlay, advisorPolicy: policy });
		expect(result.files[0].path).toBe(".omp/WATCHDOG.yml");
		const text = new TextDecoder().decode(result.files[0].bytes);
		expect(text).toContain("tools:\n      - read\n      - grep\n      - glob");
		expect(text).toContain("enabled: false");
		expect(text).not.toContain("- bash");
		expect(text).not.toContain("- Bash");
		expect(text).not.toContain("model:");
		// The executable diagnostic block is replaced, and no runnable psql
		// command or bash fence survives anywhere in the installed instructions.
		expect(text).not.toContain("psql $DATABASE_URL");
		expect(text).not.toContain("```bash");
		expect(text).toContain("## Inspection");
		expect(text).toContain("Never request or imply command execution");
	});
});

describe("candidate-execution refusal is preserved in the reference asset", () => {
	test("the eval harness installs with its upstream refusal text intact", async () => {
		const asset = assetById("ecc-eval-harness");
		const upstreamBytes = await readAssetSource(ECC_ROOT, asset);
		const result = transformAsset(asset, upstreamBytes, { index: buildIndexFromManifest(manifest.assets) });
		const text = new TextDecoder().decode(result.files[0].bytes);
		expect(text).toContain("gate.isolation_required");
		expect(text).toContain("No trust");
	});
});

describe("native discovery of adapted assets", () => {
	test("an adapted rule is discovered with mapped globs and no upstream paths", async () => {
		const tempRepo = await makeTempDir("ecc-discovery-rule-");
		await fs.mkdir(path.join(tempRepo, ".git"), { recursive: true });
		await fs.mkdir(path.join(tempRepo, ".omp", "rules"), { recursive: true });

		const rust = assetById("ecc-rust-coding-style");
		const upstreamBytes = await readAssetSource(ECC_ROOT, rust);
		const result = transformAsset(rust, upstreamBytes, { index: buildIndexFromManifest(manifest.assets) });
		await Bun.write(path.join(tempRepo, ".omp", "rules", "ecc-rust-coding-style.md"), result.files[0].bytes);

		clearCache();
		const discovered = await loadCapability<Rule>(ruleCapability.id, { cwd: tempRepo });
		const rule = discovered.items.find(item => item.name === "ecc-rust-coding-style");
		expect(rule).toBeDefined();
		expect(rule?.globs).toEqual(["**/*.rs"]);
		expect(rule?.description).toBe(rust.activation.description);
		expect(rule?._source?.provider).toBe("native");
	});

	test("an adapted skill is discovered natively by its namespaced name", async () => {
		const tempRepo = await makeTempDir("ecc-discovery-skill-");
		await fs.mkdir(path.join(tempRepo, ".git"), { recursive: true });
		await fs.mkdir(path.join(tempRepo, ".omp", "skills", "ecc-research-ops"), { recursive: true });

		const asset = assetById("ecc-research-ops");
		const upstreamBytes = await readAssetSource(ECC_ROOT, asset);
		const result = transformAsset(asset, upstreamBytes, { index: buildIndexFromManifest(manifest.assets) });
		await Bun.write(path.join(tempRepo, ".omp", "skills", "ecc-research-ops", "SKILL.md"), result.files[0].bytes);

		clearCache();
		const discovered = await loadCapability<Skill>(skillCapability.id, { cwd: tempRepo });
		const skill = discovered.items.find(item => item.name === "ecc-research-ops");
		expect(skill).toBeDefined();
		expect(skill?.frontmatter?.name).toBe("ecc-research-ops");
		// The upstream description is preserved verbatim by namespacing.
		expect(skill?.frontmatter?.description).toContain("Evidence-first current-state research workflow");
	});

	test("a skill missing its description is not discovered (namespacing does not bypass the gate)", async () => {
		const tempRepo = await makeTempDir("ecc-discovery-skill-nodec-");
		await fs.mkdir(path.join(tempRepo, ".git"), { recursive: true });
		await fs.mkdir(path.join(tempRepo, ".omp", "skills", "ecc-nodesc"), { recursive: true });
		await Bun.write(
			path.join(tempRepo, ".omp", "skills", "ecc-nodesc", "SKILL.md"),
			"---\nname: ecc-nodesc\n---\nbody\n",
		);

		clearCache();
		const discovered = await loadCapability<Skill>(skillCapability.id, { cwd: tempRepo });
		expect(discovered.items.some(item => item.name === "ecc-nodesc")).toBe(false);
	});
});
