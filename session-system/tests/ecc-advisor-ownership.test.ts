import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import {
	discoverAdvisorConfigs,
	loadWatchdogConfigFile,
	resolveAdvisorConfigEditPath,
	saveWatchdogConfigFile,
} from "../../packages/coding-agent/src/advisor/config";
import { install, remove, update } from "../ecc/adapter/apply";
import { ECC_ROOT, installablePackedAssets, loadManifest } from "../ecc/adapter/catalog";
import { readLock } from "../ecc/adapter/lock";

const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true });
});

// E0202: the retired asset must never reclaim the editor's ordinary roster.
// Cover both the old install conflict and editing after a fresh installation.
// Against archived session-system/ecc at 5c57a261d6: 0 pass, 2 fail
// (unowned WATCHDOG conflict; unexpected generated WATCHDOG on fresh install).
for (const existingRoster of [true, false]) {
	test(`E0202: editor-owned roster survives ECC lifecycle (existing=${existingRoster})`, async () => {
		const root = await fs.mkdtemp(path.join(import.meta.dir, ".ecc-advisor-ownership-"));
		tempDirs.push(root);
		const projectRoot = path.join(root, "project");
		const agentDir = path.join(root, "agent");
		const nested = path.join(projectRoot, "src");
		await fs.mkdir(path.join(projectRoot, ".git", "objects"), { recursive: true });
		await fs.mkdir(path.join(projectRoot, ".git", "refs"));
		await Bun.write(path.join(projectRoot, ".git", "HEAD"), "ref: refs/heads/main\n");
		await fs.mkdir(nested);
		const dirs = { projectDir: path.join(projectRoot, ".omp"), agentDir };
		const rosterPath = await resolveAdvisorConfigEditPath("project", dirs);
		expect(rosterPath).toBe(path.join(projectRoot, ".omp", "WATCHDOG.yml"));
		await saveWatchdogConfigFile(path.join(agentDir, "WATCHDOG.yml"), {
			advisors: [
				{ name: "Project Reviewer", model: "user-model", enabled: true },
				{ name: "User Reviewer", enabled: false },
			],
		});
		if (existingRoster) {
			await saveWatchdogConfigFile(rosterPath, {
				advisors: [{ name: "Project Reviewer", model: "project-model", enabled: false }],
			});
		}
		const manifest = await loadManifest(ECC_ROOT);
		const assets = installablePackedAssets(manifest);
		const options = { eccRoot: ECC_ROOT, projectRoot };
		await install(options, manifest, assets);
		expect(await Bun.file(rosterPath).exists()).toBe(existingRoster);
		const discovered = await discoverAdvisorConfigs(projectRoot, agentDir);
		expect(discovered.warnings).toEqual([]);
		expect(discovered.advisors.map(a => a.name).sort()).toEqual(["Project Reviewer", "User Reviewer"]);
		expect(discovered.advisors.find(a => a.name === "Project Reviewer")).toMatchObject({
			model: existingRoster ? "project-model" : "user-model",
			enabled: !existingRoster,
		});

		const lockPath = path.join(projectRoot, ".omp", "ecc", "adapted.lock.json");
		const policyBefore = await Bun.file(lockPath).bytes();
		const doc = await loadWatchdogConfigFile(rosterPath);
		doc.advisors = [{ name: "Project Reviewer", enabled: true, model: "edited-model", maxNotesPerUpdate: 3 }];
		await saveWatchdogConfigFile(rosterPath, doc);
		const editedBytes = await Bun.file(rosterPath).bytes();
		expect(await Bun.file(lockPath).bytes()).toEqual(policyBefore);
		const lock = (await readLock(lockPath))!;
		expect(lock.assets.some(a => a.adaptationKind === "database-advisor-profile")).toBe(false);
		expect(lock.assets.flatMap(a => a.files).some(f => /WATCHDOG\.ya?ml$/.test(f.path))).toBe(false);
		const ownedBytes = new Map<string, Uint8Array>();
		for (const file of lock.assets.flatMap(a => a.files)) {
			ownedBytes.set(file.path, await Bun.file(path.join(projectRoot, file.path)).bytes());
		}

		await saveWatchdogConfigFile(path.join(nested, "WATCHDOG.yaml"), {
			advisors: [{ name: "Project Reviewer", model: "nested-model", enabled: false, maxNotesPerUpdate: 1 }],
		});
		await update(options, manifest, assets);
		expect(await Bun.file(lockPath).bytes()).toEqual(policyBefore);
		expect(await Bun.file(rosterPath).bytes()).toEqual(editedBytes);
		for (const [file, bytes] of ownedBytes) {
			expect(await Bun.file(path.join(projectRoot, file)).bytes()).toEqual(bytes);
		}
		const projectRoster = await discoverAdvisorConfigs(projectRoot, agentDir);
		expect(projectRoster.advisors.find(a => a.name === "Project Reviewer")).toMatchObject({
			enabled: true,
			model: "edited-model",
			maxNotesPerUpdate: 3,
		});
		const nestedRoster = await discoverAdvisorConfigs(nested, agentDir);
		expect(nestedRoster.advisors.find(a => a.name === "Project Reviewer")).toMatchObject({
			enabled: false,
			model: "nested-model",
			maxNotesPerUpdate: 1,
		});
		await remove(options);
		expect(await Bun.file(rosterPath).bytes()).toEqual(editedBytes);
		expect(await discoverAdvisorConfigs(projectRoot, agentDir)).toEqual(projectRoster);
		expect(await discoverAdvisorConfigs(nested, agentDir)).toEqual(nestedRoster);
		for (const file of ownedBytes.keys()) {
			expect(await Bun.file(path.join(projectRoot, file)).exists()).toBe(false);
		}
		expect(await readLock(lockPath)).toBeNull();
	});
}
