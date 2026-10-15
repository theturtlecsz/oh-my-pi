import { afterEach, describe, expect, test } from "bun:test";
import { $ } from "bun";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { discoverAdvisorConfigs } from "../../packages/coding-agent/src/advisor";
import { type InstallResult, buildPlan } from "../ecc/adapter/apply";
import { ECC_ROOT, assetsForPack, isInstallable, loadManifest } from "../ecc/adapter/catalog";
import { runEccCli } from "../ecc/adapter/cli";
import { lockAssetsById, lockPathFor, readLock } from "../ecc/adapter/lock";
import type { BuildLock } from "../ecc/adapter/pin";

const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) {
		await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
	}
});

async function makeTempDir(prefix: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

async function makeProject(): Promise<string> {
	const dir = await makeTempDir("ecc-advisor-project-");
	const initResult = await $`git init -q`.cwd(dir).quiet().nothrow();
	if (initResult.exitCode !== 0) {
		throw new Error(`git init failed with exit code ${initResult.exitCode}`);
	}
	return dir;
}

describe("retired ECC database advisor cannot be installed", () => {
	test("fresh project: preview, install, update omit WATCHDOG.yml and advisor is absent", async () => {
		const project = await makeProject();
		const agentDir = await makeTempDir("ecc-advisor-agent-");
		const watchdogPath = path.join(project, ".omp", "WATCHDOG.yml");
		const projectLockPath = path.join(project, ".omp", "ecc", "adapted.lock.json");

		// 1. Preview
		const previewOutput = (await runEccCli({
			command: "preview",
			project,
			eccRoot: ECC_ROOT,
		})) as { willWrite: string[]; conflicts: string[] };

		expect(previewOutput.willWrite).not.toContain(".omp/WATCHDOG.yml");
		expect(previewOutput.conflicts).not.toContain(".omp/WATCHDOG.yml");
		expect(previewOutput.willWrite.some(p => p.includes("WATCHDOG.yml"))).toBe(false);
		expect(await Bun.file(watchdogPath).exists()).toBe(false);

		const previewAdvisors = await discoverAdvisorConfigs(project, agentDir);
		expect(previewAdvisors.advisors.some(a => a.name === "ECC Database Reviewer")).toBe(false);
		expect(await Bun.file(projectLockPath).exists()).toBe(false);

		// 2. Install
		const installOutput = (await runEccCli({
			command: "install",
			project,
			eccRoot: ECC_ROOT,
		})) as InstallResult;

		expect(installOutput.written).not.toContain(".omp/WATCHDOG.yml");
		expect(installOutput.conflicts).not.toContain(".omp/WATCHDOG.yml");
		expect(installOutput.written.some(p => p.includes("WATCHDOG.yml"))).toBe(false);
		expect(await Bun.file(watchdogPath).exists()).toBe(false);

		const installAdvisors = await discoverAdvisorConfigs(project, agentDir);
		expect(installAdvisors.advisors.some(a => a.name === "ECC Database Reviewer")).toBe(false);

		expect(await Bun.file(projectLockPath).exists()).toBe(true);
		const projectLock = await readLock(projectLockPath);
		expect(projectLock).not.toBeNull();
		expect(projectLock!.assets.some(a => a.id === "ecc-database-reviewer")).toBe(false);
		expect(projectLock!.assets.some(a => a.files.some(f => f.path.includes("WATCHDOG.yml")))).toBe(false);

		// 3. Update
		const updateOutput = (await runEccCli({
			command: "update",
			project,
			eccRoot: ECC_ROOT,
		})) as InstallResult;

		expect(updateOutput.written).not.toContain(".omp/WATCHDOG.yml");
		expect(updateOutput.conflicts).not.toContain(".omp/WATCHDOG.yml");
		expect(updateOutput.written.some(p => p.includes("WATCHDOG.yml"))).toBe(false);
		expect(await Bun.file(watchdogPath).exists()).toBe(false);

		const updateAdvisors = await discoverAdvisorConfigs(project, agentDir);
		expect(updateAdvisors.advisors.some(a => a.name === "ECC Database Reviewer")).toBe(false);

		const updatedProjectLock = await readLock(projectLockPath);
		expect(updatedProjectLock).not.toBeNull();
		expect(updatedProjectLock!.assets.some(a => a.id === "ecc-database-reviewer")).toBe(false);
		expect(updatedProjectLock!.assets.some(a => a.files.some(f => f.path.includes("WATCHDOG.yml")))).toBe(false);

		// 4. Build lock and manifest verification
		const buildLock = (await readLock(lockPathFor(ECC_ROOT))) as BuildLock | null;
		expect(buildLock).not.toBeNull();
		expect(buildLock!.assets.some(a => a.id === "ecc-database-reviewer")).toBe(false);
		expect(buildLock!.assets.some(a => a.files.some(f => f.path.includes("WATCHDOG.yml")))).toBe(false);

		const manifest = await loadManifest(ECC_ROOT);
		expect(manifest.assets.some(a => a.id === "ecc-database-reviewer")).toBe(false);
		expect(manifest.packs.some(p => p.assetIds.includes("ecc-database-reviewer"))).toBe(false);
		expect(manifest.assets.some(a => a.target.path.includes("WATCHDOG.yml"))).toBe(false);
	});

	test("project with own .omp/WATCHDOG.yml: bytes remain identical across preview, install, update, remove", async () => {
		const project = await makeProject();
		const watchdogPath = path.join(project, ".omp", "WATCHDOG.yml");
		await fs.mkdir(path.dirname(watchdogPath), { recursive: true });
		const arbitraryBytes = new Uint8Array([
			0x23, 0x20, 0x75, 0x73, 0x65, 0x72, 0x2d, 0x6f, 0x77, 0x6e, 0x65, 0x64, 0x20, 0x77, 0x61, 0x74, 0x63, 0x68,
			0x64, 0x6f, 0x67, 0x0a, 0x61, 0x64, 0x76, 0x69, 0x73, 0x6f, 0x72, 0x73, 0x3a, 0x20, 0x5b, 0x5d, 0x0a,
		]);
		await Bun.write(watchdogPath, arbitraryBytes);

		// 1. Preview
		const previewOutput = (await runEccCli({
			command: "preview",
			project,
			eccRoot: ECC_ROOT,
		})) as { willWrite: string[]; conflicts: string[] };
		expect(previewOutput.willWrite).not.toContain(".omp/WATCHDOG.yml");
		expect(previewOutput.conflicts).not.toContain(".omp/WATCHDOG.yml");
		let currentBytes = await Bun.file(watchdogPath).bytes();
		expect(currentBytes).toEqual(arbitraryBytes);

		// 2. Install
		const installOutput = (await runEccCli({
			command: "install",
			project,
			eccRoot: ECC_ROOT,
		})) as InstallResult;
		expect(installOutput.written).not.toContain(".omp/WATCHDOG.yml");
		expect(installOutput.conflicts).not.toContain(".omp/WATCHDOG.yml");
		currentBytes = await Bun.file(watchdogPath).bytes();
		expect(currentBytes).toEqual(arbitraryBytes);

		// 3. Update
		const updateOutput = (await runEccCli({
			command: "update",
			project,
			eccRoot: ECC_ROOT,
		})) as InstallResult;
		expect(updateOutput.written).not.toContain(".omp/WATCHDOG.yml");
		expect(updateOutput.conflicts).not.toContain(".omp/WATCHDOG.yml");
		currentBytes = await Bun.file(watchdogPath).bytes();
		expect(currentBytes).toEqual(arbitraryBytes);

		// 4. Remove
		const removeOutput = (await runEccCli({
			command: "remove",
			project,
			eccRoot: ECC_ROOT,
		})) as InstallResult;
		expect(removeOutput.removed).not.toContain(".omp/WATCHDOG.yml");
		expect(await Bun.file(watchdogPath).exists()).toBe(true);
		currentBytes = await Bun.file(watchdogPath).bytes();
		expect(currentBytes).toEqual(arbitraryBytes);
	});

	test("rebuild plan for engineering/research assets matches build lock adapted hashes", async () => {
		const project = await makeProject();
		const manifest = await loadManifest(ECC_ROOT);
		const buildLock = (await readLock(lockPathFor(ECC_ROOT))) as BuildLock | null;
		expect(buildLock).not.toBeNull();
		const lockAssets = lockAssetsById(buildLock!);

		// Engineering pack assets
		const engineeringAssets = assetsForPack(manifest, "ecc-engineering");
		expect(engineeringAssets.length).toBeGreaterThan(0);
		expect(engineeringAssets.some(a => a.id === "ecc-database-reviewer")).toBe(false);

		const engineeringPlan = await buildPlan({ eccRoot: ECC_ROOT, projectRoot: project }, manifest, engineeringAssets);
		expect(engineeringPlan.assets.length).toBe(engineeringAssets.length);
		for (const asset of engineeringPlan.assets) {
			const lockAsset = lockAssets.get(asset.id);
			expect(lockAsset).toBeDefined();
			expect(asset.upstreamSha256).toBe(lockAsset!.upstreamSha256);
			expect(asset.adaptedSha256).toBe(lockAsset!.adaptedSha256);
			expect(asset.files).toEqual(lockAsset!.files);
		}

		// Research pack assets
		const researchAssets = assetsForPack(manifest, "ecc-research");
		expect(researchAssets.length).toBeGreaterThan(0);
		expect(researchAssets.some(a => a.id === "ecc-database-reviewer")).toBe(false);

		const researchPlan = await buildPlan({ eccRoot: ECC_ROOT, projectRoot: project }, manifest, researchAssets);
		expect(researchPlan.assets.length).toBe(researchAssets.length);
		for (const asset of researchPlan.assets) {
			const lockAsset = lockAssets.get(asset.id);
			expect(lockAsset).toBeDefined();
			expect(asset.upstreamSha256).toBe(lockAsset!.upstreamSha256);
			expect(asset.adaptedSha256).toBe(lockAsset!.adaptedSha256);
			expect(asset.files).toEqual(lockAsset!.files);
		}

		// Combined deduplicated engineering and research assets
		const combinedAssetIds = new Set([
			...manifest.packs.find(p => p.id === "ecc-engineering")!.assetIds,
			...manifest.packs.find(p => p.id === "ecc-research")!.assetIds,
		]);
		const combinedAssets = manifest.assets.filter(a => combinedAssetIds.has(a.id) && isInstallable(a));
		const combinedPlan = await buildPlan({ eccRoot: ECC_ROOT, projectRoot: project }, manifest, combinedAssets);
		expect(combinedPlan.assets.length).toBe(combinedAssets.length);
		for (const asset of combinedPlan.assets) {
			const lockAsset = lockAssets.get(asset.id);
			expect(lockAsset).toBeDefined();
			expect(asset.upstreamSha256).toBe(lockAsset!.upstreamSha256);
			expect(asset.adaptedSha256).toBe(lockAsset!.adaptedSha256);
			expect(asset.files).toEqual(lockAsset!.files);
		}
	});
});
