import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { setAgentDir } from "@oh-my-pi/pi-utils";
import { discoverAdvisorConfigs } from "../../packages/coding-agent/src/advisor/config";
import { clearCache } from "../../packages/coding-agent/src/capability/fs";
import { type Rule, ruleCapability } from "../../packages/coding-agent/src/capability/rule";
import { type Skill, skillCapability } from "../../packages/coding-agent/src/capability/skill";
import { loadCapability } from "../../packages/coding-agent/src/discovery";
import { InstallError, install, remove, update } from "../ecc/adapter/apply";
import { assetsForPack, ECC_ROOT, installablePackedAssets, loadManifest } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { readLock } from "../ecc/adapter/lock";
import type { Manifest, ManifestAsset } from "../ecc/adapter/types";
import manifestData from "../ecc/manifest.json";

const manifest = manifestData as unknown as Manifest;

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

async function makeProject(): Promise<string> {
	const dir = await makeTempDir("ecc-install-project-");
	await fs.mkdir(path.join(dir, ".git"), { recursive: true });
	return dir;
}

async function installAssets(projectRoot: string, packId: string) {
	const loaded = await loadManifest(ECC_ROOT);
	return install({ eccRoot: ECC_ROOT, projectRoot }, loaded, assetsForPack(loaded, packId));
}

async function snapshot(dir: string): Promise<Map<string, string>> {
	const files = new Map<string, string>();
	const walk = async (current: string): Promise<void> => {
		for (const entry of await fs.readdir(current, { withFileTypes: true }).catch(() => [])) {
			const full = path.join(current, entry.name);
			if (entry.isDirectory()) await walk(full);
			else if (entry.isFile())
				files.set(path.relative(dir, full), Bun.hash(await Bun.file(full).arrayBuffer()).toString());
		}
	};
	await walk(dir);
	return files;
}

describe("engineering pack install, update, remove", () => {
	test("install writes exactly the lock-listed files with the recorded hashes", async () => {
		const projectRoot = await makeProject();
		const result = await installAssets(projectRoot, "ecc-engineering");

		expect(result.status).toBe("installed");
		expect(result.conflicts).toEqual([]);
		expect(result.written.length).toBeGreaterThan(0);

		const lock = await readLock(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json"));
		expect(lock).not.toBeNull();
		const written = new Set(result.written);
		for (const asset of lock!.assets) {
			for (const file of asset.files) {
				expect(written.has(file.path)).toBe(true);
				const installed = await Bun.file(path.join(projectRoot, file.path)).bytes();
				expect(sha256Hex(installed)).toBe(file.sha256);
			}
		}
	});

	test("re-installing is unchanged and writes nothing", async () => {
		const projectRoot = await makeProject();
		await installAssets(projectRoot, "ecc-engineering");
		const before = await snapshot(projectRoot);

		const second = await installAssets(projectRoot, "ecc-engineering");
		expect(second.status).toBe("unchanged");
		expect(second.written).toEqual([]);
		expect(await snapshot(projectRoot)).toEqual(before);
	});

	test("install refuses to overwrite an unowned destination and writes nothing", async () => {
		const projectRoot = await makeProject();
		await fs.mkdir(path.join(projectRoot, ".omp", "rules"), { recursive: true });
		const userFile = path.join(projectRoot, ".omp", "rules", "ecc-common-coding-style.md");
		await Bun.write(userFile, "# user-owned rule\n");
		const before = await snapshot(projectRoot);

		await expect(installAssets(projectRoot, "ecc-engineering")).rejects.toThrow(/refusing to overwrite unowned/);
		expect(await Bun.file(userFile).text()).toBe("# user-owned rule\n");
		expect(await snapshot(projectRoot)).toEqual(before);
		expect(await Bun.file(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json")).exists()).toBe(false);
	});

	test("update refuses an owned file modified after install", async () => {
		const projectRoot = await makeProject();
		await installAssets(projectRoot, "ecc-engineering");
		const owned = path.join(projectRoot, ".omp", "rules", "ecc-common-coding-style.md");
		await Bun.write(owned, `${await Bun.file(owned).text()}\nlocal edit\n`);

		const loaded = await loadManifest(ECC_ROOT);
		await expect(
			update({ eccRoot: ECC_ROOT, projectRoot }, loaded, assetsForPack(loaded, "ecc-engineering")),
		).rejects.toThrow(/refusing to overwrite unowned|refusing to overwrite modified owned/);
	});

	test("update rewrites an untouched owned file when the adapted output changes", async () => {
		const projectRoot = await makeProject();
		await installAssets(projectRoot, "ecc-engineering");
		const owned = path.join(projectRoot, ".omp", "rules", "ecc-common-coding-style.md");
		const beforeText = await Bun.file(owned).text();

		// A manifest change that alters the adapted bytes (rule frontmatter
		// description) while the mirror bytes, and thus the upstream pin, hold.
		const loaded = await loadManifest(ECC_ROOT);
		const mutated: Manifest = structuredClone(loaded);
		const target = mutated.assets.find(a => a.id === "ecc-common-coding-style")!;
		target.activation.description = "Updated description for the coding style rule";

		const result = await update({ eccRoot: ECC_ROOT, projectRoot }, mutated, [target]);
		expect(result.status).toBe("updated");
		expect(result.written).toContain(".omp/rules/ecc-common-coding-style.md");
		const afterText = await Bun.file(owned).text();
		expect(afterText).not.toBe(beforeText);
		expect(afterText).toContain("Updated description for the coding style rule");
	});

	test("update without a prior install fails closed", async () => {
		const projectRoot = await makeProject();
		const loaded = await loadManifest(ECC_ROOT);
		await expect(
			update({ eccRoot: ECC_ROOT, projectRoot }, loaded, assetsForPack(loaded, "ecc-engineering")),
		).rejects.toThrow(/no adapted.lock.json to update/);
	});

	test("remove deletes every owned file and leaves user files untouched", async () => {
		const projectRoot = await makeProject();
		await installAssets(projectRoot, "ecc-engineering");
		const userFile = path.join(projectRoot, "user-notes.md");
		await Bun.write(userFile, "keep me\n");

		const result = await remove({ eccRoot: ECC_ROOT, projectRoot });
		expect(result.status).toBe("removed");
		expect(result.removed.length).toBeGreaterThan(0);

		expect(await Bun.file(userFile).text()).toBe("keep me\n");
		expect(await Bun.file(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json")).exists()).toBe(false);
		expect(await Bun.file(path.join(projectRoot, ".omp", "rules", "ecc-common-coding-style.md")).exists()).toBe(
			false,
		);
	});

	test("remove refuses a modified owned file", async () => {
		const projectRoot = await makeProject();
		await installAssets(projectRoot, "ecc-engineering");
		const owned = path.join(projectRoot, ".omp", "rules", "ecc-common-coding-style.md");
		await Bun.write(owned, "# tampered\n");

		await expect(remove({ eccRoot: ECC_ROOT, projectRoot })).rejects.toThrow(/refusing to remove modified file/);
		expect(await Bun.file(owned).text()).toBe("# tampered\n");
	});

	test("remove is idempotent when nothing is installed", async () => {
		const projectRoot = await makeProject();
		const result = await remove({ eccRoot: ECC_ROOT, projectRoot });
		expect(result).toEqual({ status: "removed", written: [], removed: [], skipped: [], conflicts: [] });
	});
});

describe("research pack installs script-dependent assets with helpers", () => {
	test("the research pack installs the self-evaluation skill and every helper file", async () => {
		const projectRoot = await makeProject();
		const result = await installAssets(projectRoot, "ecc-research");
		expect(result.status).toBe("installed");

		const asset = manifest.assets.find(a => a.id === "ecc-agent-self-evaluation") as ManifestAsset;
		for (const dep of asset.dependencies) {
			const installed = path.join(projectRoot, ".omp", "skills", "ecc-agent-self-evaluation", dep);
			expect(await Bun.file(installed).exists()).toBe(true);
			const mirrored = await Bun.file(path.join(ECC_ROOT, "mirror", asset.upstream.path, dep)).bytes();
			expect(Bun.hash(await Bun.file(installed).arrayBuffer()).toString()).toBe(Bun.hash(mirrored).toString());
		}

		clearCache();
		const skills = await loadCapability<Skill>(skillCapability.id, { cwd: projectRoot });
		expect(skills.items.some(s => s.name === "ecc-agent-self-evaluation")).toBe(true);
	});
});

describe("database advisor installs disabled and activates only on explicit opt-in", () => {
	test("installed advisor is discovered but disabled by default", async () => {
		const projectRoot = await makeProject();
		const agentDir = await makeTempDir("ecc-advisor-agent-");
		await installAssets(projectRoot, "ecc-engineering");

		const discovered = await discoverAdvisorConfigs(projectRoot, agentDir);
		const advisor = discovered.advisors.find(a => a.name === "ECC Database Reviewer");
		expect(advisor).toBeDefined();
		expect(advisor?.enabled).toBe(false);
		expect(advisor?.tools).toEqual(["read", "grep", "glob"]);
		expect(advisor?.instructions).toContain("read-only database and data-systems advisor");
	});

	test("after removal no advisor remains discoverable", async () => {
		const projectRoot = await makeProject();
		const agentDir = await makeTempDir("ecc-advisor-agent-");
		await installAssets(projectRoot, "ecc-engineering");
		await remove({ eccRoot: ECC_ROOT, projectRoot });

		const discovered = await discoverAdvisorConfigs(projectRoot, agentDir);
		expect(discovered.advisors).toEqual([]);
	});
});

describe("no accidental activation", () => {
	test("removing the install removes every adopted rule and skill from discovery", async () => {
		const projectRoot = await makeProject();
		await installAssets(projectRoot, "ecc-engineering");

		clearCache();
		const rulesBefore = await loadCapability<Rule>(ruleCapability.id, { cwd: projectRoot });
		expect(rulesBefore.items.filter(r => r.name.startsWith("ecc-")).length).toBeGreaterThan(0);

		await remove({ eccRoot: ECC_ROOT, projectRoot });
		clearCache();
		const rulesAfter = await loadCapability<Rule>(ruleCapability.id, { cwd: projectRoot });
		const skillsAfter = await loadCapability<Skill>(skillCapability.id, { cwd: projectRoot });
		expect(rulesAfter.items.filter(r => r.name.startsWith("ecc-")).length).toBe(0);
		expect(skillsAfter.items.filter(s => s.name.startsWith("ecc-")).length).toBe(0);
	});

	test("installing the engineering pack never installs research-pack-only assets", async () => {
		const projectRoot = await makeProject();
		await installAssets(projectRoot, "ecc-engineering");
		const researchOnly = assetsForPack(manifest, "ecc-research").filter(
			asset => !assetsForPack(manifest, "ecc-engineering").some(e => e.id === asset.id),
		);
		for (const asset of researchOnly) {
			const dest = path.join(projectRoot, asset.target.path);
			expect(await Bun.file(dest).exists()).toBe(false);
		}
	});

	test("no installable target basename collides with a reserved native command", () => {
		const reserved = new Set(manifest.nativeCommandNames);
		for (const asset of installablePackedAssets(manifest)) {
			const base = path.posix.basename(asset.target.path).replace(/\.(md|yml|yaml)$/, "");
			expect(reserved.has(base)).toBe(false);
		}
	});
});

describe("install state is per project", () => {
	test("two projects do not share an ownership record", async () => {
		const first = await makeProject();
		const second = await makeProject();
		await installAssets(first, "ecc-engineering");
		await installAssets(second, "ecc-engineering");

		expect(await readLock(path.join(first, ".omp", "ecc", "adapted.lock.json"))).not.toBeNull();
		expect(await readLock(path.join(second, ".omp", "ecc", "adapted.lock.json"))).not.toBeNull();
		await remove({ eccRoot: ECC_ROOT, projectRoot: first });
		// Removing one install must not disturb the other.
		expect(await Bun.file(path.join(second, ".omp", "rules", "ecc-common-coding-style.md")).exists()).toBe(true);
	});
});

describe("InstallError is a named failure", () => {
	test("refusals surface as InstallError", async () => {
		const projectRoot = await makeProject();
		await fs.mkdir(path.join(projectRoot, ".omp", "rules"), { recursive: true });
		await Bun.write(path.join(projectRoot, ".omp", "rules", "ecc-common-coding-style.md"), "x");
		await expect(installAssets(projectRoot, "ecc-engineering")).rejects.toThrow(InstallError);
	});
});
