import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { ECC_ROOT, listMirrorFiles, loadManifest } from "../ecc/adapter/catalog";
import { buildFileCatalog } from "../ecc/adapter/file-catalog";
import type { Manifest } from "../ecc/adapter/types";

const tempDirs: string[] = [];
afterEach(async () => {
	for (const root of tempDirs.splice(0)) await fs.rm(root, { recursive: true, force: true });
});

async function tempRoot(): Promise<string> {
	const root = await fs.mkdtemp(path.join(path.resolve(import.meta.dir, "../.."), ".ecc-catalog-"));
	tempDirs.push(root);
	await Bun.write(path.join(root, "manifest.json"), await Bun.file(path.join(ECC_ROOT, "manifest.json")).text());
	await Bun.write(path.join(root, "mirror/README.md"), "fixture\n");
	return root;
}

async function check(root: string): Promise<{ code: number; stderr: string }> {
	const proc = Bun.spawn(["bun", path.join(ECC_ROOT, "adapter/emit-catalog.ts"), "--ecc-root", root, "--check"], {
		stdin: "ignore",
		stdout: "ignore",
		stderr: "pipe",
	});
	const [code, stderr] = await Promise.all([proc.exited, new Response(proc.stderr).text()]);
	return { code, stderr };
}

describe("ECC file catalog", () => {
	test("committed catalog reproduces all mirror paths and family dispositions", async () => {
		const fresh = await buildFileCatalog(ECC_ROOT, await loadManifest());
		expect(await Bun.file(path.join(ECC_ROOT, "catalog.json")).json()).toEqual(fresh);
		expect(fresh.files.map(file => file.path)).toEqual(await listMirrorFiles());
		expect(fresh.files).toHaveLength(3716);
		expect(fresh.files.every(file => file.reason.trim().length > 0)).toBe(true);
		expect(fresh.files.find(file => file.path === "skills/search-first/SKILL.md")?.assetId).toBe("ecc-search-first");
		expect(fresh.files.find(file => file.path === "skills/continuous-learning-v2/SKILL.md")).toMatchObject({
			classification: "deferred",
			assetId: null,
			reason: expect.stringContaining("WP7A"),
		});
		expect(fresh.files.find(file => file.path === "skills/skill-comply/pyproject.toml")?.classification).toBe(
			"reference-only",
		);
		expect(fresh.files.find(file => file.path === "install.sh")?.classification).toBe("excluded");
	});

	test("unknown root file fails build and check naming its path", async () => {
		const root = await tempRoot();
		await Bun.write(path.join(root, "mirror/NEWFILE.md"), "new\n");
		await expect(buildFileCatalog(root, await loadManifest(root))).rejects.toThrow(
			"unclassified upstream file NEWFILE.md",
		);
		const result = await check(root);
		expect(result.code).toBe(1);
		expect(result.stderr).toContain("unclassified upstream file NEWFILE.md");
	});

	for (const invalid of ["empty prefix", "duplicate prefix", "empty rule reason", "empty asset reason"] as const) {
		test(`loadManifest rejects ${invalid}`, async () => {
			const root = await tempRoot();
			const manifest: Manifest = await loadManifest(root);
			if (invalid === "empty prefix")
				manifest.dispositionRules.push({ prefix: "", disposition: "reference-only", reason: "catch-all" });
			if (invalid === "duplicate prefix") manifest.dispositionRules.push({ ...manifest.dispositionRules[0]! });
			if (invalid === "empty rule reason") manifest.dispositionRules[0]!.reason = " ";
			if (invalid === "empty asset reason") manifest.assets[0]!.reason = " ";
			await Bun.write(path.join(root, "manifest.json"), JSON.stringify(manifest));
			await expect(loadManifest(root)).rejects.toThrow(
				invalid === "duplicate prefix" ? "duplicate disposition rule prefix" : "must be non-empty",
			);
		});
	}

	test("competing assets fail instead of choosing one owner", async () => {
		const root = await tempRoot();
		const manifest = await loadManifest(root);
		const asset = manifest.assets[0]!;
		manifest.assets = [
			{ ...asset, upstream: { ...asset.upstream, path: "README.md" } },
			{
				...asset,
				id: "second-owner",
				upstream: { ...asset.upstream, path: "README.md" },
				target: { ...asset.target, path: ".omp/rules/second.md" },
			},
		];
		manifest.packs = [];
		await expect(buildFileCatalog(root, manifest)).rejects.toThrow("multiple assets claim upstream file README.md");
	});

	test("asset directories include helpers but not sibling names; longest rule wins", async () => {
		const root = await tempRoot();
		await Bun.write(path.join(root, "mirror/skills/search-first/helper.txt"), "helper");
		await Bun.write(path.join(root, "mirror/skills/search-first-other/SKILL.md"), "sibling");
		await Bun.write(path.join(root, "mirror/skills/tdd-workflow/SKILL.md"), "workflow");
		const catalog = await buildFileCatalog(root, await loadManifest(root));
		expect(catalog.files.find(file => file.path.endsWith("helper.txt"))?.assetId).toBe("ecc-search-first");
		expect(catalog.files.find(file => file.path.includes("search-first-other"))).toMatchObject({
			classification: "reference-only",
			assetId: null,
		});
		expect(catalog.files.find(file => file.path.includes("tdd-workflow"))).toMatchObject({
			classification: "deferred",
			reason: expect.stringContaining("WP3"),
		});
	});

	test("check accepts fresh catalog then exits 1 naming stale mirror bytes", async () => {
		const root = await tempRoot();
		await Bun.write(
			path.join(root, "catalog.json"),
			`${JSON.stringify(await buildFileCatalog(root, await loadManifest(root)), null, 2)}\n`,
		);
		expect(await check(root)).toEqual({ code: 0, stderr: "" });
		await Bun.write(path.join(root, "mirror/README.md"), "changed\n");
		const result = await check(root);
		expect(result.code).toBe(1);
		expect(result.stderr).toContain("stale catalog path README.md");
	});
});
