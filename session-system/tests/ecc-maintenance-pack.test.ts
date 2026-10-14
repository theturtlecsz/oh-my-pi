import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { install } from "../ecc/adapter/apply";
import { assetsForPack, ECC_ROOT, isInstallable, loadManifest, readAssetSource } from "../ecc/adapter/catalog";
import type { FileCatalog } from "../ecc/adapter/file-catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { readLock } from "../ecc/adapter/lock";
import type { ManifestAsset } from "../ecc/adapter/types";

const maintenancePaths = [
	"skills/benchmark-optimization-loop",
	"skills/agent-architecture-audit",
	"skills/loop-design-check",
	"skills/skill-stocktake",
	"skills/rules-distill",
	"skills/context-budget",
];
const newSkillPaths = maintenancePaths.filter(p => p !== "skills/benchmark-optimization-loop");
const tempDirs: string[] = [];
afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true });
});

/** Mirror-relative helper files of a skill directory, i.e. every file but SKILL.md. */
async function mirrorHelperFiles(asset: ManifestAsset): Promise<string[]> {
	const dir = path.join(ECC_ROOT, "mirror", asset.upstream.path);
	const found: string[] = [];
	const walk = async (current: string): Promise<void> => {
		for (const entry of await fs.readdir(current, { withFileTypes: true })) {
			const full = path.join(current, entry.name);
			if (entry.isDirectory()) await walk(full);
			else if (entry.isFile() && entry.name !== "SKILL.md") {
				found.push(path.posix.normalize(path.relative(dir, full).replaceAll("\\", "/")));
			}
		}
	};
	await walk(dir);
	return found.sort();
}

describe("ECC maintenance pack", () => {
	test("ecc-maintenance resolves the six pinned members as an empty-selection maintenance lane", async () => {
		const manifest = await loadManifest();
		const assets = assetsForPack(manifest, "ecc-maintenance");
		expect(assets.map(asset => asset.upstream.path).sort()).toEqual([...maintenancePaths].sort());
		expect(manifest.packs.find(pack => pack.id === "ecc-maintenance")).toMatchObject({
			lane: "maintenance",
			selection: { profileId: null, moduleIds: [] },
		});
	});

	test("each new maintenance record pins its mirror SKILL.md sha256", async () => {
		const manifest = await loadManifest();
		for (const upstreamPath of newSkillPaths) {
			const id = `ecc-${path.posix.basename(upstreamPath)}`;
			const asset = manifest.assets.find(entry => entry.id === id);
			expect(asset).toBeDefined();
			expect(asset!.pack).toBe("maintenance");
			expect(asset!.upstream.path).toBe(upstreamPath);
			expect(asset!.target).toEqual({ path: `.omp/skills/${id}`, scope: "project" });
			expect(asset!.adaptation.kind).toBe("skill-namespaced");
			expect(asset!.activation.state).toBe("inactive");
			expect(asset!.disposition).toBe("deferred");
			expect(asset!.qualification.mechanicalTests).toEqual([]);

			const bytes = await readAssetSource(ECC_ROOT, asset!);
			expect(sha256Hex(bytes)).toBe(asset!.upstream.sha256);
			const mirrorBytes = await Bun.file(path.join(ECC_ROOT, "mirror", upstreamPath, "SKILL.md")).bytes();
			expect(asset!.upstream.sha256).toBe(sha256Hex(mirrorBytes));
		}
	});

	test("each new record's dependencies equal its mirror directory's non-SKILL.md files", async () => {
		const manifest = await loadManifest();
		for (const upstreamPath of newSkillPaths) {
			const id = `ecc-${path.posix.basename(upstreamPath)}`;
			const asset = manifest.assets.find(entry => entry.id === id)!;
			const expectedHelpers = await mirrorHelperFiles(asset);
			expect([...asset.dependencies].sort()).toEqual(expectedHelpers);
			if (expectedHelpers.length > 0) expect(asset.runtimes).toEqual(["bash"]);
			else expect(asset.runtimes).toBeUndefined();
		}
	});

	test("installing ecc-maintenance writes only the benchmark-optimization-loop files", async () => {
		const projectRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-maintenance-"));
		tempDirs.push(projectRoot);
		await fs.mkdir(path.join(projectRoot, ".git"));
		const manifest = await loadManifest();
		const assets = assetsForPack(manifest, "ecc-maintenance");
		const result = await install({ eccRoot: ECC_ROOT, projectRoot }, manifest, assets);
		expect(result.conflicts).toEqual([]);
		expect(result.written.length).toBeGreaterThan(0);
		const benchmarkRoot = ".omp/skills/ecc-benchmark-optimization-loop";
		expect(result.written.every(file => file.startsWith(`${benchmarkRoot}/`))).toBe(true);

		const lock = await readLock(path.join(projectRoot, ".omp/ecc/adapted.lock.json"));
		expect(lock?.assets.map(asset => asset.id)).toEqual(["ecc-benchmark-optimization-loop"]);

		for (const asset of assets.filter(entry => !isInstallable(entry))) {
			expect(await Bun.file(path.join(projectRoot, asset.target.path)).exists()).toBe(false);
		}
	});

	test("catalog defers skill-stocktake's scan.sh to asset ecc-skill-stocktake", async () => {
		const catalog = (await Bun.file(path.join(ECC_ROOT, "catalog.json")).json()) as FileCatalog;
		const record = catalog.files.find(file => file.path === "skills/skill-stocktake/scripts/scan.sh");
		expect(record).toMatchObject({ classification: "deferred", assetId: "ecc-skill-stocktake" });
	});
});
