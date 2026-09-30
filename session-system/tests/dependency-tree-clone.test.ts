import { afterEach, describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { cloneDependencyTree } from "../extensions/workflow/dependency-tree";

const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) {
		try {
			await fs.rm(dir, { recursive: true, force: true });
		} catch {
			// ignore cleanup error
		}
	}
});

async function createFixture() {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "omp-dep-tree-test-"));
	tempDirs.push(root);

	const primary = path.join(root, "primary");
	const wt = path.join(root, "wt");

	const primaryNodeModules = path.join(primary, "node_modules");
	const primaryWsDir = path.join(primaryNodeModules, "@ws");
	const primaryLibDir = path.join(primaryNodeModules, "lib");
	const primaryLibDeepDir = path.join(primaryLibDir, "deep");

	const wtPackagesPkg = path.join(wt, "packages/pkg");

	await fs.mkdir(primaryWsDir, { recursive: true });
	await fs.mkdir(primaryLibDeepDir, { recursive: true });
	await fs.mkdir(wtPackagesPkg, { recursive: true });

	// Symlink: primary/node_modules/@ws/pkg -> ../../packages/pkg
	await fs.symlink("../../packages/pkg", path.join(primaryWsDir, "pkg"));

	// Files:
	// primary/node_modules/lib/index.js
	await fs.writeFile(path.join(primaryLibDir, "index.js"), "module.exports = 'lib';\n");
	// primary/node_modules/lib/deep/x.js
	await fs.writeFile(path.join(primaryLibDeepDir, "x.js"), "module.exports = 'deep-x';\n");
	// wt/packages/pkg/index.js
	await fs.writeFile(path.join(wtPackagesPkg, "index.js"), "export const pkg = 1;\n");

	return {
		root,
		primary,
		primaryNodeModules,
		wt,
		wtPackagesPkg,
	};
}

describe("cloneDependencyTree", () => {
	test("clones dependency tree preserving verbatim symlinks and hardlinks", async () => {
		const fixture = await createFixture();
		const wtNodeModules = path.join(fixture.wt, "node_modules");

		await cloneDependencyTree(fixture.primaryNodeModules, wtNodeModules);

		// realpath of wt/node_modules/@ws/pkg equals realpath of wt/packages/pkg
		const wtWsPkgRealpath = await fs.realpath(path.join(wtNodeModules, "@ws/pkg"));
		const expectedPkgRealpath = await fs.realpath(fixture.wtPackagesPkg);
		expect(wtWsPkgRealpath).toBe(expectedPkgRealpath);

		// readlink text equals the source's
		const srcLinkText = await fs.readlink(path.join(fixture.primaryNodeModules, "@ws/pkg"));
		const targetLinkText = await fs.readlink(path.join(wtNodeModules, "@ws/pkg"));
		expect(targetLinkText).toBe(srcLinkText);
		expect(targetLinkText).toBe("../../packages/pkg");

		// lib/index.js content equal and same inode as the source (same filesystem)
		const srcLibContent = await fs.readFile(path.join(fixture.primaryNodeModules, "lib/index.js"), "utf8");
		const targetLibContent = await fs.readFile(path.join(wtNodeModules, "lib/index.js"), "utf8");
		expect(targetLibContent).toBe(srcLibContent);

		const srcStat = await fs.stat(path.join(fixture.primaryNodeModules, "lib/index.js"));
		const targetStat = await fs.stat(path.join(wtNodeModules, "lib/index.js"));
		expect(targetStat.ino).toBe(srcStat.ino);

		// lib/deep/x.js present
		const deepStat = await fs.lstat(path.join(wtNodeModules, "lib/deep/x.js"));
		expect(deepStat.isFile()).toBe(true);
	});

	test("rejects when target already exists and leaves target contents unchanged", async () => {
		const fixture = await createFixture();
		const existingTarget = path.join(fixture.wt, "node_modules");
		await fs.mkdir(existingTarget, { recursive: true });
		const canaryFile = path.join(existingTarget, "canary.txt");
		await fs.writeFile(canaryFile, "pre-existing content\n");

		await expect(cloneDependencyTree(fixture.primaryNodeModules, existingTarget)).rejects.toThrow(
			`Target already exists: ${existingTarget}`,
		);

		const content = await fs.readFile(canaryFile, "utf8");
		expect(content).toBe("pre-existing content\n");

		const entries = await fs.readdir(existingTarget);
		expect(entries).toEqual(["canary.txt"]);
	});

	test("cleans up partial directory on failure mid-walk and rethrows", async () => {
		const isRoot = typeof process.getuid === "function" && process.getuid() === 0;
		if (isRoot) return;

		const fixture = await createFixture();
		const forbiddenDir = path.join(fixture.primaryNodeModules, "forbidden");
		await fs.mkdir(forbiddenDir);
		await fs.writeFile(path.join(forbiddenDir, "unreadable.js"), "secret");

		await fs.chmod(forbiddenDir, 0o000);

		const wtNodeModules = path.join(fixture.wt, "node_modules");
		try {
			await expect(cloneDependencyTree(fixture.primaryNodeModules, wtNodeModules)).rejects.toThrow();

			// no wt/node_modules
			const targetExists = await fs.lstat(wtNodeModules).then(
				() => true,
				() => false,
			);
			expect(targetExists).toBe(false);

			// no wt/node_modules.partial-* remains
			const wtEntries = await fs.readdir(fixture.wt);
			const partialEntries = wtEntries.filter(name => name.startsWith("node_modules.partial-"));
			expect(partialEntries).toEqual([]);
		} finally {
			await fs.chmod(forbiddenDir, 0o755).catch(() => {});
		}
	});

	test("falls back to copyFile when fs.link throws EXDEV", async () => {
		const fixture = await createFixture();
		const wtNodeModules = path.join(fixture.wt, "node_modules");

		const linkSpy = spyOn(fs, "link").mockImplementation(async () => {
			const err = new Error("Cross-device link") as NodeJS.ErrnoException;
			err.code = "EXDEV";
			throw err;
		});

		try {
			await cloneDependencyTree(fixture.primaryNodeModules, wtNodeModules);

			const srcLibContent = await fs.readFile(path.join(fixture.primaryNodeModules, "lib/index.js"), "utf8");
			const targetLibContent = await fs.readFile(path.join(wtNodeModules, "lib/index.js"), "utf8");
			expect(targetLibContent).toBe(srcLibContent);

			const srcStat = await fs.stat(path.join(fixture.primaryNodeModules, "lib/index.js"));
			const targetStat = await fs.stat(path.join(wtNodeModules, "lib/index.js"));
			expect(targetStat.ino).not.toBe(srcStat.ino);
		} finally {
			linkSpy.mockRestore();
		}
	});
});
