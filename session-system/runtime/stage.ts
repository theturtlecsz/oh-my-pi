import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { parseArgs } from "node:util";
import { isEnoent } from "@oh-my-pi/pi-utils";
import { $ } from "bun";
import { isContained } from "../../packages/coding-agent/src/discovery/contained-path";
import * as git from "../../packages/coding-agent/src/utils/git";
import {
	collectReleaseFiles,
	type InstalledPythonIdentity,
	releaseFileSha256,
	type StagedReleaseManifest,
} from "./manifest";
import pythonIdentityProgram from "./python-identity.py" with { type: "text" };

export * from "./manifest";

export interface StageReleaseOptions {
	sourceRoot: string;
	destination: string;
	/** Exact expected version of the supplied Bun executable, including explicit overrides of packageManager. */
	expectedBunVersion: string;
	bunPath: string;
	uvPath: string;
	/** Existing host interpreter. Its executable and external stdlib location are recorded explicitly. */
	pythonPath: string;
	/** Native addons are supplied explicitly, copied, and checked against the archived package version. */
	nativeAddonPaths: readonly string[];
}

async function assertFreshDestination(sourceRoot: string, requested: string): Promise<string> {
	if (!path.isAbsolute(requested)) throw new Error("Release destination must be an explicit absolute path");
	const destination = path.resolve(requested);
	const parent = await fs.realpath(path.dirname(destination));
	const canonical = path.join(parent, path.basename(destination));
	if (
		canonical !== destination ||
		canonical === path.parse(canonical).root ||
		canonical === os.homedir() ||
		isContained(sourceRoot, canonical) ||
		isContained(canonical, sourceRoot)
	) {
		throw new Error("Release destination must be outside the source tree, without symlinked parent directories");
	}
	try {
		await fs.lstat(canonical);
	} catch (error) {
		if (isEnoent(error)) return canonical;
		throw error;
	}
	throw new Error(`Release destination already exists: ${canonical}`);
}

function buildEnvironment(releaseRoot: string): Record<string, string> {
	const scratch = path.join(releaseRoot, ".stage-build");
	return {
		PATH: `${path.join(releaseRoot, "bin")}${path.delimiter}/usr/bin${path.delimiter}/bin`,
		HOME: path.join(scratch, "home"),
		XDG_CONFIG_HOME: path.join(scratch, "config"),
		XDG_CACHE_HOME: path.join(scratch, "cache"),
		TMPDIR: path.join(scratch, "tmp"),
		LANG: "C.UTF-8",
		CI: "1",
		BUN_INSTALL_CACHE_DIR: path.join(scratch, "bun-cache"),
		UV_CACHE_DIR: path.join(scratch, "uv-cache"),
		UV_PROJECT_ENVIRONMENT: path.join(releaseRoot, "python"),
		UV_PYTHON_DOWNLOADS: "never",
		PYTHONDONTWRITEBYTECODE: "1",
	};
}

async function copyExecutable(source: string, destination: string): Promise<void> {
	const resolved = await fs.realpath(source);
	const stat = await fs.stat(resolved);
	if (!stat.isFile() || !(stat.mode & 0o111)) throw new Error(`Expected executable file: ${source}`);
	await Bun.write(destination, Bun.file(resolved));
	await fs.chmod(destination, 0o755);
}

/** Stage a clean HEAD at its final installation path. Never installs links, starts services, or activates it. */
export async function stageRelease(options: StageReleaseOptions): Promise<StagedReleaseManifest> {
	if (process.platform === "win32") throw new Error("Source release staging currently requires a POSIX host");
	if (!/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(options.expectedBunVersion)) {
		throw new Error("An exact expected Bun version is required");
	}
	if (options.nativeAddonPaths.length === 0) throw new Error("At least one explicit native addon is required");
	const sourceRoot = await fs.realpath(options.sourceRoot);
	if ((await git.repo.root(sourceRoot)) !== sourceRoot) throw new Error("Source must be a Git repository root");
	const destination = await assertFreshDestination(sourceRoot, options.destination);
	if ((await git.status(sourceRoot, { untrackedFiles: "all" })).trim()) {
		throw new Error("Release staging requires a clean source checkout, including untracked files");
	}
	const commit = await git.head.sha(sourceRoot);
	if (!commit) throw new Error("Source HEAD has no commit");
	const uvPath = await fs.realpath(options.uvPath);
	const pythonPath = await fs.realpath(options.pythonPath);
	if (isContained(sourceRoot, pythonPath)) {
		throw new Error("Host Python interpreter must be independent of the candidate source checkout");
	}
	await fs.mkdir(destination);
	await Bun.write(path.join(destination, "staging.json"), `${JSON.stringify({ status: "INCOMPLETE", commit })}\n`);
	const env = buildEnvironment(destination);
	for (const directory of [env.HOME, env.XDG_CONFIG_HOME, env.XDG_CACHE_HOME, env.TMPDIR]) {
		await fs.mkdir(directory, { recursive: true });
	}
	const source = path.join(destination, "source");
	const archivePath = path.join(destination, ".stage-build", "source.tar");
	await git.archive(sourceRoot, commit, archivePath);
	await new Bun.Archive(await Bun.file(archivePath).arrayBuffer()).extract(source);
	// Validate tracked symlinks before any dependency/build tool can follow them.
	await collectReleaseFiles(source);
	const rootPackage = (await Bun.file(path.join(source, "package.json")).json()) as { packageManager: string };
	const nativePackage = (await Bun.file(path.join(source, "packages/natives/package.json")).json()) as {
		version: string;
	};
	if (typeof rootPackage.packageManager !== "string" || typeof nativePackage.version !== "string") {
		throw new Error("Archived package manifests lack runtime version identities");
	}
	const bunPath = path.join(destination, "bin/bun");
	await copyExecutable(options.bunPath, bunPath);
	const bunVersion = (await $`${bunPath} --version`.cwd(destination).env(env).quiet().text()).trim();
	if (bunVersion !== options.expectedBunVersion) {
		throw new Error(`Bun version mismatch: expected ${options.expectedBunVersion}, found ${bunVersion}`);
	}
	await fs.symlink("bun", path.join(destination, "bin/bunx"));
	await copyExecutable(path.join(source, "session-system/runtime/omp.sh"), path.join(destination, "bin/omp"));
	const natives: StagedReleaseManifest["natives"] = [];
	for (const input of options.nativeAddonPaths) {
		const name = path.basename(input);
		if (!/^pi_natives\.[\w-]+\.node$/.test(name)) throw new Error(`Unsupported native addon filename: ${name}`);
		const relative = `source/packages/natives/native/${name}`;
		if (natives.some(native => native.path === relative)) throw new Error(`Duplicate native addon: ${name}`);
		const output = path.join(destination, relative);
		await Bun.write(output, Bun.file(await fs.realpath(input)));
		const version = (
			await $`${bunPath} ${path.join(source, "scripts/install-tests/native-version.ts")} ${output}`
				.cwd(destination)
				.env(env)
				.quiet()
				.text()
		).trim();
		if (version !== nativePackage.version) {
			throw new Error(`Native version mismatch for ${name}: expected ${nativePackage.version}, found ${version}`);
		}
		natives.push({ path: relative, version, sha256: await releaseFileSha256(output) });
	}
	await $`${bunPath} install --frozen-lockfile --ignore-scripts --backend=copyfile --no-progress`
		.cwd(source)
		.env(env)
		.quiet();
	await $`${bunPath} run gen:tool-views`.cwd(source).env(env).quiet();
	const uvVersion = (await $`${uvPath} --version`.cwd(destination).env(env).quiet().text()).trim();
	// Source-mode stats otherwise compiles into its package directory on first
	// use. Embed its dashboard now so runtime extraction stays in private TMPDIR.
	await $`${bunPath} run gen:stats`.cwd(source).env(env).quiet();
	await $`${uvPath} sync --project ${path.join(source, "python/omp-work")} --python ${pythonPath} --frozen --no-editable --no-dev --link-mode copy --no-python-downloads --no-progress`
		.cwd(destination)
		.env(env)
		.quiet();
	const installedPython = path.join(destination, "python/bin/python");
	const probe = path.join(destination, ".stage-build/python-identity.py");
	await Bun.write(probe, pythonIdentityProgram);
	const identity = JSON.parse(
		await $`${installedPython} -I -B ${probe}`.cwd(destination).env(env).quiet().text(),
	) as InstalledPythonIdentity;
	const project = Bun.TOML.parse(await Bun.file(path.join(source, "python/omp-work/pyproject.toml")).text()) as {
		project: { version: string };
	};
	if (identity.serviceVersion !== project.project.version)
		throw new Error("Installed WorkService version differs from source");
	if (identity.editable || !isContained(path.join(destination, "python"), identity.serviceModule)) {
		throw new Error(`Service import is not a release-local noneditable installation: ${identity.serviceModule}`);
	}
	if (identity.executable !== pythonPath)
		throw new Error("Installed Python resolved to an unexpected host interpreter");
	if (isContained(sourceRoot, identity.basePrefix) || isContained(sourceRoot, identity.stdlib)) {
		throw new Error("Host Python standard library must be independent of the candidate source checkout");
	}
	await fs.rm(path.join(destination, ".stage-build"), { recursive: true });
	await fs.unlink(path.join(destination, "staging.json"));
	const manifest: StagedReleaseManifest = {
		schemaVersion: 1,
		status: "STAGED",
		releaseRoot: destination,
		createdAt: new Date().toISOString(),
		platform: process.platform,
		arch: process.arch,
		source: { commit, path: "source", packageManager: rootPackage.packageManager },
		bun: { path: "bin/bun", version: bunVersion, sha256: await releaseFileSha256(bunPath) },
		python: { ...identity, path: "python/bin/python", executableSha256: await releaseFileSha256(pythonPath) },
		uv: { version: uvVersion, sha256: await releaseFileSha256(uvPath) },
		natives,
		files: await collectReleaseFiles(destination, [pythonPath]),
	};
	await Bun.write(path.join(destination, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
	return manifest;
}

if (import.meta.main) {
	const { values } = parseArgs({
		args: process.argv.slice(2),
		options: {
			source: { type: "string" },
			destination: { type: "string" },
			bun: { type: "string" },
			"bun-version": { type: "string" },
			uv: { type: "string" },
			python: { type: "string" },
			native: { type: "string", multiple: true },
		},
		strict: true,
	});
	if (!values.source || !values.destination || !values.bun || !values["bun-version"] || !values.uv || !values.python) {
		throw new Error(
			"Usage: stage.ts --source /checkout --destination /releases/id --bun /bin/bun --bun-version X.Y.Z --uv /bin/uv --python /bin/python3 --native /addons/pi_natives.PLATFORM.node [--native ...]",
		);
	}
	const manifest = await stageRelease({
		sourceRoot: values.source,
		destination: values.destination,
		bunPath: values.bun,
		expectedBunVersion: values["bun-version"],
		uvPath: values.uv,
		pythonPath: values.python,
		nativeAddonPaths: values.native ?? [],
	});
	process.stdout.write(
		`${JSON.stringify({
			status: manifest.status,
			releaseRoot: manifest.releaseRoot,
			sourceCommit: manifest.source.commit,
			manifestSha256: await releaseFileSha256(path.join(manifest.releaseRoot, "manifest.json")),
		})}\n`,
	);
}
