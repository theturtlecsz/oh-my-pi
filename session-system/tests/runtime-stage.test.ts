import { afterEach, describe, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import * as git from "../../packages/coding-agent/src/utils/git";
import {
	collectReleaseFiles,
	releaseFileSha256,
	type StagedReleaseManifest,
	type StageReleaseOptions,
	stageRelease,
	verifyRelease,
} from "../runtime/stage";

const directories: string[] = [];
const disposers: (() => void)[] = [];

afterEach(async () => {
	for (const dispose of disposers.splice(0)) dispose();
	for (const directory of directories.splice(0)) await fs.rm(directory, { recursive: true, force: true });
});

async function fixture(): Promise<{ root: string; source: string; release: string; options: StageReleaseOptions }> {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "omp-stage-test-"));
	directories.push(root);
	const source = path.join(root, "candidate");
	const release = path.join(root, "release");
	await fs.mkdir(source);
	const options: StageReleaseOptions = {
		sourceRoot: source,
		destination: release,
		expectedBunVersion: Bun.version,
		bunPath: process.execPath,
		uvPath: process.execPath,
		pythonPath: process.execPath,
		nativeAddonPaths: [path.join(source, "pi_natives.linux-x64.node")],
	};
	return { root, source, release, options };
}

async function manifestedFixture(): Promise<{ release: string; interpreter: string; digest: string }> {
	const { source, release } = await fixture();
	const interpreter = path.join(source, "python");
	const serviceModule = path.join(release, "python/lib/omp_work/__init__.py");
	await Bun.write(interpreter, "host Python runtime");
	await Bun.write(path.join(release, "bin/bun"), "pinned Bun executable");
	await Bun.write(serviceModule, "installed service");
	await Bun.write(path.join(release, "source/native.node"), "pinned addon");
	await fs.mkdir(path.join(release, "python/bin"));
	await fs.symlink(interpreter, path.join(release, "python/bin/python"));
	const manifest: StagedReleaseManifest = {
		schemaVersion: 1,
		status: "STAGED",
		releaseRoot: release,
		createdAt: "2026-09-07T00:00:00.000Z",
		platform: process.platform,
		arch: process.arch,
		source: { commit: "a".repeat(40), path: "source", packageManager: "bun@1.3.14" },
		bun: { path: "bin/bun", version: "1.3.14", sha256: await releaseFileSha256(path.join(release, "bin/bun")) },
		python: {
			path: "python/bin/python",
			version: "3.13.7",
			executable: interpreter,
			executableSha256: await releaseFileSha256(interpreter),
			basePrefix: source,
			stdlib: source,
			serviceVersion: "0.1.0",
			serviceModule,
			contractVersion: "work.omp.dev/v1",
			editable: false,
			distributions: [{ name: "omp-work", version: "0.1.0" }],
		},
		uv: { version: "uv 0.8.13", sha256: "b".repeat(64) },
		natives: [
			{
				path: "source/native.node",
				version: "18.0.6",
				sha256: await releaseFileSha256(path.join(release, "source/native.node")),
			},
		],
		files: await collectReleaseFiles(release, [interpreter]),
	};
	const manifestPath = path.join(release, "manifest.json");
	await Bun.write(manifestPath, JSON.stringify(manifest));
	return { release, interpreter, digest: await releaseFileSha256(manifestPath) };
}

describe("release inventory isolation", () => {
	test("copied files retain their recorded bytes after candidate edits; internal workspace links remain usable", async () => {
		const { source, release } = await fixture();
		const candidate = path.join(source, "module.ts");
		const installed = path.join(release, "source/module.ts");
		await Bun.write(candidate, "export const value = 1;\n");
		await Bun.write(installed, Bun.file(candidate));
		await fs.symlink("source/module.ts", path.join(release, "entry.ts"));
		const inventory = await collectReleaseFiles(release);
		await Bun.write(candidate, "export const value = 2;\n");
		expect(await releaseFileSha256(candidate)).not.toBe(await releaseFileSha256(installed));
		expect(await collectReleaseFiles(release)).toEqual(inventory);
		expect(await Bun.file(path.join(release, "entry.ts")).text()).toBe("export const value = 1;\n");
	});

	test("external source links and cache hardlinks cannot enter the install manifest", async () => {
		const { source, release } = await fixture();
		await fs.mkdir(release);
		const candidate = path.join(source, "module.ts");
		await Bun.write(candidate, "candidate bytes");
		const link = path.join(release, "module.ts");
		await fs.symlink(candidate, link);
		await expect(collectReleaseFiles(release)).rejects.toThrow("symlink escapes installation");
		await fs.unlink(link);
		await fs.link(candidate, link);
		await expect(collectReleaseFiles(release)).rejects.toThrow("shares an inode");
	});

	test("explicit interpreter exception admits only that exact external file", async () => {
		const { source, release } = await fixture();
		await fs.mkdir(release);
		const interpreter = path.join(source, "python");
		const other = path.join(source, "editable-service.py");
		await Bun.write(interpreter, "host interpreter");
		await Bun.write(other, "mutable service");
		await fs.symlink(interpreter, path.join(release, "python"));
		const inventory = await collectReleaseFiles(release, [interpreter]);
		expect(inventory.map(file => ({ kind: file.kind, target: file.target }))).toEqual([
			{ kind: "symlink", target: interpreter },
		]);
		await fs.symlink(other, path.join(release, "service.py"));
		await expect(collectReleaseFiles(release, [interpreter])).rejects.toThrow("service.py");
	});
});

describe("release staging admission", () => {
	test("dirty source is refused before any release directory is created", async () => {
		const { source, release, options } = await fixture();
		const root = spyOn(git.repo, "root").mockResolvedValue(source);
		const status = spyOn(git, "status").mockResolvedValue(" M workflow.ts\n");
		disposers.push(
			() => root.mockRestore(),
			() => status.mockRestore(),
		);
		await expect(stageRelease(options)).rejects.toThrow("clean source checkout");
		await expect(fs.lstat(release)).rejects.toHaveProperty("code", "ENOENT");
	});

	test("existing or overlapping destinations preserve existing content", async () => {
		const { source, release, options } = await fixture();
		const root = spyOn(git.repo, "root").mockResolvedValue(source);
		disposers.push(() => root.mockRestore());
		await Bun.write(path.join(release, "keep"), "existing installation");
		await expect(stageRelease(options)).rejects.toThrow("already exists");
		await expect(stageRelease({ ...options, destination: path.join(source, "release") })).rejects.toThrow(
			"outside the source tree",
		);
		expect(await Bun.file(path.join(release, "keep")).text()).toBe("existing installation");
	});

	test("a Bun version mismatch leaves an incomplete stage without a consumable manifest", async () => {
		const { source, release, options } = await fixture();
		const root = spyOn(git.repo, "root").mockResolvedValue(source);
		const status = spyOn(git, "status").mockResolvedValue("");
		const head = spyOn(git.head, "sha").mockResolvedValue("a".repeat(40));
		const archive = spyOn(git, "archive").mockImplementation(async (_cwd, _commit, output) => {
			await Bun.Archive.write(output, {
				"package.json": JSON.stringify({ packageManager: `bun@${Bun.version}` }),
				"packages/natives/package.json": JSON.stringify({ version: "18.0.6" }),
			});
		});
		disposers.push(
			() => root.mockRestore(),
			() => status.mockRestore(),
			() => head.mockRestore(),
			() => archive.mockRestore(),
		);
		await expect(stageRelease({ ...options, expectedBunVersion: "0.0.0" })).rejects.toThrow("Bun version mismatch");
		expect(await Bun.file(path.join(release, "manifest.json")).exists()).toBe(false);
		expect(await Bun.file(path.join(release, "staging.json")).json()).toMatchObject({ status: "INCOMPLETE" });
		expect(await releaseFileSha256(path.join(release, "bin/bun"))).toBe(await releaseFileSha256(process.execPath));
	});
});

describe("release verification", () => {
	test("bootstrap verification loads without native dependencies and refuses altered native bytes", async () => {
		const { release, digest } = await manifestedFixture();
		const probe = path.join(path.dirname(release), "bootstrap-probe");
		await Bun.write(
			path.join(probe, "session-system/runtime/manifest.ts"),
			Bun.file(path.join(import.meta.dir, "../runtime/manifest.ts")),
		);
		await Bun.write(
			path.join(probe, "packages/coding-agent/src/discovery/contained-path.ts"),
			Bun.file(path.join(import.meta.dir, "../../packages/coding-agent/src/discovery/contained-path.ts")),
		);
		await Bun.write(
			path.join(probe, "verify.ts"),
			'import { verifyRelease } from "./session-system/runtime/manifest"; const manifest = await verifyRelease(process.argv[2], process.argv[3]); process.stdout.write(manifest.source.commit);',
		);
		const env = { PATH: "/usr/bin:/bin", HOME: probe };
		const verified = await $`${process.execPath} ${path.join(probe, "verify.ts")} ${release} ${digest}`
			.cwd(probe)
			.env(env)
			.quiet()
			.text();
		expect(verified).toBe("a".repeat(40));
		await Bun.write(path.join(release, "source/native.node"), "changed invalid native bytes");
		const refused = await $`${process.execPath} ${path.join(probe, "verify.ts")} ${release} ${digest}`
			.cwd(probe)
			.env(env)
			.quiet()
			.nothrow();
		expect(refused.exitCode).not.toBe(0);
		expect(refused.stderr.toString()).toContain("Release inventory changed: source/native.node");
	});

	test("modified runtime bytes and added discovery files reject an otherwise selected manifest", async () => {
		const { release, digest } = await manifestedFixture();
		await verifyRelease(release, digest);
		const runtime = path.join(release, "python/lib/omp_work/__init__.py");
		await Bun.write(runtime, "changed service");
		await expect(verifyRelease(release, digest)).rejects.toThrow("inventory changed");
		await Bun.write(runtime, "installed service");
		await Bun.write(path.join(release, "source/.omp/extensions/unapproved.ts"), "unapproved discovery entry");
		await expect(verifyRelease(release, digest)).rejects.toThrow("added or missing files");
	});

	test("manifest selection and external interpreter drift fail independently of release-local bytes", async () => {
		const { release, interpreter, digest } = await manifestedFixture();
		await expect(verifyRelease(release, "0".repeat(64))).rejects.toThrow("manifest digest");
		await Bun.write(interpreter, "upgraded host Python runtime");
		await expect(verifyRelease(release, digest)).rejects.toThrow("External Python interpreter identity changed");
	});
});
