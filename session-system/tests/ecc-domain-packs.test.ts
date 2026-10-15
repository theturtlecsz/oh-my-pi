import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { install } from "../ecc/adapter/apply";
import { assetsForPack, ECC_ROOT, isInstallable, loadManifest, readAssetSource } from "../ecc/adapter/catalog";
import type { FileCatalog } from "../ecc/adapter/file-catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { readLock } from "../ecc/adapter/lock";

const rulePaths = (language: string, stems: string[]): string[] => stems.map(stem => `rules/${language}/${stem}.md`);
const skillPaths = (...names: string[]): string[] => names.map(name => `skills/${name}`);
const typescriptSkills = ["coding-standards", "hexagonal-architecture", "architecture-decision-records", "api-design"];
const expectedPacks = {
	rust: [
		"rules/common/coding-style.md",
		...rulePaths("rust", ["coding-style", "hooks", "patterns", "security", "testing"]),
		...skillPaths("rust-patterns", "rust-testing"),
		"agents/rust-reviewer.md",
	],
	typescript: [
		...rulePaths("typescript", ["coding-style", "hooks", "patterns", "security", "testing"]),
		...skillPaths(...typescriptSkills),
	],
	python: [
		...rulePaths("python", ["coding-style", "fastapi", "hooks", "patterns", "security", "testing"]),
		...skillPaths("python-patterns", "python-testing"),
		"agents/python-reviewer.md",
	],
	postgresql: [...skillPaths("postgres-patterns", "database-migrations")],
	security: [
		...["common", "rust", "typescript", "python"].map(language => `rules/${language}/security.md`),
		"agents/security-reviewer.md",
		...skillPaths("security-scan"),
	],
};
const installedIds: Record<string, string[]> = {
	rust: ["ecc-common-coding-style", "ecc-rust-coding-style"],
	typescript: [],
	python: [],
	postgresql: [],
	security: [],
};
const tempDirs: string[] = [];
afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true });
});

describe("ECC domain packs", () => {
	for (const [domain, paths] of Object.entries(expectedPacks)) {
		const packId = `ecc-domain-${domain}`;
		test(`${packId} resolves pinned members with installable rule dependencies in pack`, async () => {
			const manifest = await loadManifest();
			const assets = assetsForPack(manifest, packId);
			expect(assets.map(asset => asset.upstream.path).sort()).toEqual([...paths].sort());
			expect(manifest.packs.find(pack => pack.id === packId)).toMatchObject({
				lane: "domain",
				selection: { profileId: null, moduleIds: [] },
			});
			for (const asset of assets) {
				expect(sha256Hex(await readAssetSource(ECC_ROOT, asset))).toBe(asset.upstream.sha256);
				if (isInstallable(asset) && asset.adaptation.kind === "rule-metadata") {
					for (const dependency of asset.dependencies) {
						expect(assets.some(member => member.id === dependency && isInstallable(member))).toBe(true);
					}
				} else if (!isInstallable(asset)) {
					expect(asset.disposition).toBe("deferred");
					expect(asset.activation.state).toBe("inactive");
				}
			}
		});

		test(`${packId} installs only adapted members with resolvable Markdown links`, async () => {
			const projectRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-domain-"));
			tempDirs.push(projectRoot);
			await fs.mkdir(path.join(projectRoot, ".git"));
			const manifest = await loadManifest();
			const assets = assetsForPack(manifest, packId);
			const result = await install({ eccRoot: ECC_ROOT, projectRoot }, manifest, assets);
			expect(result.conflicts).toEqual([]);
			const lock = await readLock(path.join(projectRoot, ".omp/ecc/adapted.lock.json"));
			expect(lock?.assets.map(asset => asset.id).sort()).toEqual(installedIds[domain]);
			for (const asset of assets.filter(asset => !isInstallable(asset))) {
				expect(await Bun.file(path.join(projectRoot, asset.target.path)).exists()).toBe(false);
			}
			for (const file of result.written.filter(file => file.endsWith(".md"))) {
				const fullPath = path.join(projectRoot, file);
				const content = await Bun.file(fullPath).text();
				for (const match of content.matchAll(/!?\]\(([^()\s]+)/g)) {
					const target = match[1]!;
					if (/^(?:[a-z][a-z0-9+.-]*:|\/|#)/i.test(target)) continue;
					const relative = target.split(/[?#]/)[0]!;
					const resolved = await fs.stat(path.resolve(path.dirname(fullPath), relative));
					expect(resolved.isFile() || resolved.isDirectory()).toBe(true);
				}
			}
			if (domain === "rust") {
				const rust = await Bun.file(path.join(projectRoot, ".omp/rules/ecc-rust-coding-style.md")).text();
				expect(rust).toContain("](ecc-common-coding-style.md)");
			}
		});
	}

	test("catalog attributes deferred TypeScript rules and skill files to their asset ids", async () => {
		const catalog = (await Bun.file(path.join(ECC_ROOT, "catalog.json")).json()) as FileCatalog;
		const roots = [
			...rulePaths("typescript", ["coding-style", "hooks", "patterns", "security", "testing"]),
			...skillPaths(...typescriptSkills),
		];
		for (const root of roots) {
			const id = root.startsWith("rules/")
				? `ecc-typescript-${path.basename(root, ".md")}-rule`
				: `ecc-${path.basename(root)}`;
			const files = catalog.files.filter(file => file.path === root || file.path.startsWith(`${root}/`));
			expect(files.map(file => file.path)).toContain(root.startsWith("skills/") ? `${root}/SKILL.md` : root);
			for (const file of files) expect(file).toMatchObject({ classification: "deferred", assetId: id });
		}
	});
});
