import { afterEach, describe, expect, test } from "bun:test";
import { $ } from "bun";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { type InstallResult, update } from "../ecc/adapter/apply";
import { ECC_ROOT, loadManifest } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { runEccCli } from "../ecc/adapter/cli";
import { readLock, writeLock } from "../ecc/adapter/lock";
import type { LockAsset } from "../ecc/adapter/types";

interface LegacyFixture {
	watchdog: { path: string; text: string };
	asset: LockAsset;
}

const legacy = (await Bun.file(path.join(import.meta.dir, "fixtures", "ecc-advisor-legacy-install.json")).json()) as LegacyFixture;
const RETIRED_ID = legacy.asset.id;
const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true });
});

async function makeProject(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-advisor-retire-"));
	tempDirs.push(dir);
	const init = await $`git init -q`.cwd(dir).quiet().nothrow();
	if (init.exitCode !== 0) throw new Error("git init failed");
	return dir;
}

const lockFile = (project: string): string => path.join(project, ".omp", "ecc", "adapted.lock.json");
const run = (command: "install" | "update", project: string) =>
	runEccCli({ command, project, eccRoot: ECC_ROOT }) as Promise<InstallResult>;

/** Install current ECC, then graft the legacy advisor onto the project's lock. */
async function legacyProject(options: { roster?: string | null; owned?: boolean } = {}): Promise<string> {
	const project = await makeProject();
	await run("install", project);
	const roster = path.join(project, legacy.watchdog.path);
	if (options.roster !== null) await Bun.write(roster, options.roster ?? legacy.watchdog.text);
	if (options.owned !== false) {
		const lock = (await readLock(lockFile(project)))!;
		lock.assets.push(structuredClone(legacy.asset));
		await writeLock(lockFile(project), lock);
	}
	return project;
}

async function expectRetiredAndIdempotent(project: string): Promise<void> {
	expect((await readLock(lockFile(project)))!.assets.some(asset => asset.id === RETIRED_ID)).toBe(false);
	const again = await run("update", project);
	expect(again.status).toBe("unchanged");
	expect(again.written).toEqual([]);
	expect(again.removed).toEqual([]);
	expect(again.skipped).not.toContain(legacy.watchdog.path);
}

describe("update retires lock assets absent from the manifest", () => {
	test("unchanged owned roster is deleted, lock drops the asset, repeat update is idempotent", async () => {
		const project = await legacyProject();
		const userFile = path.join(project, ".omp", "rules", "user-rule.md");
		await Bun.write(userFile, "mine\n");
		const before = (await readLock(lockFile(project)))!;
		const keptIds = before.assets.filter(a => a.id !== RETIRED_ID).map(a => a.id);

		const result = await run("update", project);
		expect(result.status).toBe("updated");
		expect(result.written).toEqual([]);
		expect(result.removed).toEqual([legacy.watchdog.path]);
		expect(await Bun.file(path.join(project, legacy.watchdog.path)).exists()).toBe(false);
		expect(await Bun.file(userFile).text()).toBe("mine\n");
		// .omp survives the prune: it still holds the install lock and user files.
		expect((await fs.stat(path.join(project, ".omp"))).isDirectory()).toBe(true);

		const after = (await readLock(lockFile(project)))!;
		expect(after.assets.some(a => a.id === RETIRED_ID)).toBe(false);
		expect(after.assets.map(a => a.id)).toEqual(keptIds);
		for (const asset of after.assets) {
			for (const file of asset.files) {
				expect(sha256Hex(await Bun.file(path.join(project, file.path)).bytes())).toBe(file.sha256);
			}
		}

		const again = await run("update", project);
		expect(again.status).toBe("unchanged");
		expect(again.removed).toEqual([]);
		expect(again.skipped).not.toContain(legacy.watchdog.path);
	});

	test("enabled/edited roster is kept and reported skipped", async () => {
		const edited = legacy.watchdog.text.replace("enabled: false", "enabled: true");
		expect(edited).not.toBe(legacy.watchdog.text);
		const project = await legacyProject({ roster: edited });
		const result = await run("update", project);
		expect(result.status).toBe("unchanged");
		expect(result.written).toEqual([]);
		expect(result.removed).toEqual([]);
		expect(result.skipped).toContain(legacy.watchdog.path);
		expect(await Bun.file(path.join(project, legacy.watchdog.path)).text()).toBe(edited);
		expect((await readLock(lockFile(project)))!.assets.some(a => a.id === RETIRED_ID)).toBe(false);
		await expectRetiredAndIdempotent(project);
	});

	test("roster with project advisors and comments is kept byte-for-byte", async () => {
		const mixed = `# my advisors\nadvisors:\n  - name: Mine\n    enabled: true\n${legacy.watchdog.text}`;
		const project = await legacyProject({ roster: mixed });
		const result = await run("update", project);
		expect(result.status).toBe("unchanged");
		expect(result.written).toEqual([]);
		expect(result.removed).toEqual([]);
		expect(result.skipped).toContain(legacy.watchdog.path);
		expect(await Bun.file(path.join(project, legacy.watchdog.path)).text()).toBe(mixed);
		await expectRetiredAndIdempotent(project);
	});

	test("missing roster does not throw and is reported skipped", async () => {
		const project = await legacyProject({ roster: null });
		const result = await run("update", project);
		expect(result.status).toBe("unchanged");
		expect(result.written).toEqual([]);
		expect(result.removed).toEqual([]);
		expect(result.skipped).toContain(legacy.watchdog.path);
		expect(await Bun.file(path.join(project, legacy.watchdog.path)).exists()).toBe(false);
		expect((await readLock(lockFile(project)))!.assets.some(a => a.id === RETIRED_ID)).toBe(false);
		await expectRetiredAndIdempotent(project);
	});

	test("roster not owned by the lock is never deleted", async () => {
		const project = await legacyProject({ owned: false });
		const result = await run("update", project);
		expect(result.status).toBe("unchanged");
		expect(result.written).toEqual([]);
		expect(result.removed).toEqual([]);
		expect(result.skipped).not.toContain(legacy.watchdog.path);
		expect(await Bun.file(path.join(project, legacy.watchdog.path)).text()).toBe(legacy.watchdog.text);
		await expectRetiredAndIdempotent(project);
	});

	test("retirement prunes emptied directories deepest first but keeps .omp", async () => {
		const project = await legacyProject();
		const deep = ".omp/retired/nested/old.md";
		const bytes = "old\n";
		await Bun.write(path.join(project, deep), bytes);
		const lock = (await readLock(lockFile(project)))!;
		const retired = lock.assets.find(a => a.id === RETIRED_ID)!;
		retired.files.push({ path: deep, sha256: sha256Hex(bytes), mode: "644" });
		await writeLock(lockFile(project), lock);

		const result = await run("update", project);
		expect(result.removed.sort()).toEqual([deep, legacy.watchdog.path].sort());
		expect(await Bun.file(path.join(project, ".omp", "retired")).exists()).toBe(false);
		expect((await fs.stat(path.join(project, ".omp"))).isDirectory()).toBe(true);
	});

	test("retirement compares raw bytes and retains unselected declared assets and helpers", async () => {
		const project = await legacyProject();
		const manifest = await loadManifest();
		const lock = (await readLock(lockFile(project)))!;
		const retained = lock.assets.filter(asset => asset.id !== RETIRED_ID);
		const helper = ".omp/skills/omitted-helper.bin";
		const binary = new Uint8Array([0xff, 0xfe, 0x00]);
		await Bun.write(path.join(project, helper), binary);
		retained[0].files.push({ path: helper, sha256: sha256Hex(binary), mode: "644" });
		const retiredFile = ".omp/retired/binary.bin";
		await Bun.write(path.join(project, retiredFile), binary);
		lock.assets.find(asset => asset.id === RETIRED_ID)!.files.push({
			path: retiredFile, sha256: sha256Hex(binary), mode: "644",
		});
		await writeLock(lockFile(project), lock);

		const result = await update({ eccRoot: ECC_ROOT, projectRoot: project }, manifest, []);
		expect(result.status).toBe("updated");
		expect(result.written).toEqual([]);
		expect(new Set(result.removed)).toEqual(new Set([legacy.watchdog.path, retiredFile]));
		expect(result.skipped).toEqual([]);
		expect(await Bun.file(path.join(project, retiredFile)).exists()).toBe(false);
		expect((await readLock(lockFile(project)))!.assets).toEqual(retained);
		for (const asset of retained) {
			for (const file of asset.files) {
				expect(sha256Hex(await Bun.file(path.join(project, file.path)).bytes())).toBe(file.sha256);
			}
		}
		const again = await update({ eccRoot: ECC_ROOT, projectRoot: project }, manifest, []);
		expect(again.status).toBe("unchanged");
		expect(again.removed).toEqual([]);
	});

	test("modified active asset still refuses update and retired roster stays", async () => {
		const project = await legacyProject();
		const lock = (await readLock(lockFile(project)))!;
		const active = lock.assets.find(a => a.id !== RETIRED_ID)!;
		const target = path.join(project, active.files[0].path);
		await Bun.write(target, "local edit\n");
		await expect(run("update", project)).rejects.toThrow(/modified owned file/);
		expect(await Bun.file(target).text()).toBe("local edit\n");
		expect(await Bun.file(path.join(project, legacy.watchdog.path)).text()).toBe(legacy.watchdog.text);
	});
});
