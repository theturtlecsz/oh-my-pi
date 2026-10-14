import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { buildPlan, install, previewPlan, remove, update } from "../ecc/adapter/apply";
import { assetsForPack, ECC_ROOT, loadManifest } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { readLock } from "../ecc/adapter/lock";

// OMP-611-s01 reproduction evidence: ran this file against a disposable archive
// of session-system/ecc at 5c57a261d6: 0 pass, 3 fail (identical destination,
// pack ownership, omitted helper). Fixed implementation: 3 pass, 0 fail;
// combined with unchanged ecc-install.test.ts: 20 pass, 0 fail.
const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true });
});

async function makeProject(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(import.meta.dir, ".ecc-ownership-"));
	tempDirs.push(dir);
	return dir;
}

function lockPath(projectRoot: string): string {
	return path.join(projectRoot, ".omp", "ecc", "adapted.lock.json");
}

test("identical unowned destination conflicts in preview and install without adopting user bytes", async () => {
	const projectRoot = await makeProject();
	const options = { eccRoot: ECC_ROOT, projectRoot };
	const manifest = await loadManifest();
	const assets = assetsForPack(manifest, "ecc-engineering");
	const plan = await buildPlan(options, manifest, assets);
	const file = plan.files[0];
	const destination = path.join(projectRoot, file.path);
	await Bun.write(destination, file.bytes);

	const preview = await previewPlan(options, plan, null);
	expect(preview.conflicts).toEqual([file.path]);
	expect(preview.willWrite).not.toContain(file.path);
	await expect(install(options, manifest, assets)).rejects.toThrow(/refusing to overwrite unowned files/);
	expect(await Bun.file(destination).bytes()).toEqual(file.bytes);
	expect(await readLock(lockPath(projectRoot))).toBeNull();
	for (const other of plan.files.slice(1)) {
		expect(await Bun.file(path.join(projectRoot, other.path)).exists()).toBe(false);
	}
	expect((await remove(options)).removed).toEqual([]);
	expect(await Bun.file(destination).bytes()).toEqual(file.bytes);
});

test("engineering research engineering installs retain ownership so explicit remove deletes every owned file", async () => {
	const projectRoot = await makeProject();
	const options = { eccRoot: ECC_ROOT, projectRoot };
	const manifest = await loadManifest();
	const tracked = new Map<string, string>();
	const unrelated = path.join(projectRoot, ".omp", "rules", "user-rule.md");
	await Bun.write(unrelated, "user rule\n");

	for (const pack of ["ecc-engineering", "ecc-research", "ecc-engineering"]) {
		const assets = assetsForPack(manifest, pack);
		const plan = await buildPlan(options, manifest, assets);
		for (const file of plan.files) tracked.set(file.path, file.sha256);
		await install(options, manifest, assets);
		const lock = await readLock(lockPath(projectRoot));
		const owned = new Map(lock!.assets.flatMap(asset => asset.files.map(file => [file.path, file.sha256])));
		expect(owned).toEqual(tracked);
		for (const [filePath, hash] of tracked) {
			expect(sha256Hex(await Bun.file(path.join(projectRoot, filePath)).bytes())).toBe(hash);
		}
	}

	const before = await Bun.file(lockPath(projectRoot)).text();
	const reinstall = await install(options, manifest, assetsForPack(manifest, "ecc-engineering"));
	expect(reinstall.status).toBe("unchanged");
	expect(reinstall.written).toEqual([]);
	expect(await Bun.file(lockPath(projectRoot)).text()).toBe(before);
	const result = await remove(options);
	expect(new Set(result.removed)).toEqual(new Set(tracked.keys()));
	for (const filePath of tracked.keys()) {
		expect(await Bun.file(path.join(projectRoot, filePath)).exists()).toBe(false);
	}
	expect(await Bun.file(unrelated).text()).toBe("user rule\n");
	expect(await readLock(lockPath(projectRoot))).toBeNull();
});

test("updated skill retains omitted helper hash and ownership until explicit remove", async () => {
	const projectRoot = await makeProject();
	const eccRoot = await makeProject();
	const options = { eccRoot, projectRoot };
	const manifest = await loadManifest();
	const asset = structuredClone(manifest.assets.find(entry => entry.adaptation.kind === "skill-namespaced")!);
	asset.adaptation.overlay = undefined;
	asset.dependencies = ["helper.txt"];
	const source = "---\nname: ownership-fixture\ndescription: Ownership fixture\n---\n\nInitial skill.\n";
	asset.upstream.sha256 = sha256Hex(source);
	await Bun.write(path.join(eccRoot, "mirror", asset.upstream.path, "SKILL.md"), source);
	await Bun.write(path.join(eccRoot, "mirror", asset.upstream.path, "helper.txt"), "original helper\n");
	await install(options, manifest, [asset]);
	const initial = await readLock(lockPath(projectRoot));
	const helper = initial!.assets[0].files.find(file => file.path.endsWith("/helper.txt"))!;

	asset.dependencies = [];
	const changedSource = source.replace("Initial skill.", "Updated skill.");
	asset.upstream.sha256 = sha256Hex(changedSource);
	await Bun.write(path.join(eccRoot, "mirror", asset.upstream.path, "SKILL.md"), changedSource);
	await update(options, manifest, [asset]);
	const updated = await readLock(lockPath(projectRoot));
	expect(updated!.assets[0].files.find(file => file.path === helper.path)).toEqual(helper);
	const skill = updated!.assets[0].files.find(file => file.path.endsWith("/SKILL.md"))!;
	expect(skill.sha256).not.toBe(initial!.assets[0].files.find(file => file.path === skill.path)!.sha256);
	expect(sha256Hex(await Bun.file(path.join(projectRoot, skill.path)).bytes())).toBe(skill.sha256);
	expect(await Bun.file(path.join(projectRoot, helper.path)).text()).toBe("original helper\n");

	const result = await remove(options);
	expect(new Set(result.removed)).toEqual(new Set([skill.path, helper.path]));
	expect(await Bun.file(path.join(projectRoot, helper.path)).exists()).toBe(false);
});
