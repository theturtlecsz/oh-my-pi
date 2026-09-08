import * as fs from "node:fs/promises";
import * as path from "node:path";
import { isContained } from "../../packages/coding-agent/src/discovery/contained-path";

export interface ReleaseFile {
	path: string;
	sha256: string;
	mode: number;
	kind: "file" | "symlink";
	target?: string;
}

export interface InstalledPythonIdentity {
	version: string;
	executable: string;
	basePrefix: string;
	stdlib: string;
	serviceVersion: string;
	serviceModule: string;
	contractVersion: string;
	contractSha256: string;
	migrationSetSha256: string;
	serviceFingerprint: string;
	editable: boolean;
	distributions: { name: string; version: string }[];
}

/** STAGED records installed bytes, not qualification, promotion, or work authority. */
export interface StagedReleaseManifest {
	schemaVersion: 1;
	status: "STAGED";
	releaseRoot: string;
	createdAt: string;
	platform: string;
	arch: string;
	source: { commit: string; path: "source"; packageManager: string };
	bun: { path: "bin/bun"; version: string; sha256: string };
	python: InstalledPythonIdentity & { path: "python/bin/python"; executableSha256: string };
	uv: { version: string; sha256: string };
	natives: { path: string; version: string; sha256: string }[];
	files: ReleaseFile[];
}

export async function releaseFileSha256(filePath: string): Promise<string> {
	return new Bun.CryptoHasher("sha256").update(await Bun.file(filePath).arrayBuffer()).digest("hex");
}

/** Inventory all installed bytes and links; refuse shared inodes and paths escaping the release. */
export async function collectReleaseFiles(
	releaseRoot: string,
	allowedExternalFiles: readonly string[] = [],
): Promise<ReleaseFile[]> {
	const root = await fs.realpath(releaseRoot);
	const allowed = new Set(await Promise.all(allowedExternalFiles.map(file => fs.realpath(file))));
	const entries: ReleaseFile[] = [];
	async function visit(directory: string): Promise<void> {
		for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
			const absolute = path.join(directory, entry.name);
			const relative = path.relative(root, absolute).split(path.sep).join("/");
			if (relative === "manifest.json") continue;
			const stat = await fs.lstat(absolute);
			if (stat.isDirectory()) {
				await visit(absolute);
			} else if (stat.isSymbolicLink()) {
				const target = await fs.readlink(absolute);
				const resolved = await fs.realpath(absolute);
				if (!isContained(root, resolved) && !allowed.has(resolved)) {
					throw new Error(`Release symlink escapes installation: ${relative} -> ${target}`);
				}
				entries.push({
					path: relative,
					kind: "symlink",
					target,
					mode: stat.mode & 0o777,
					sha256: new Bun.CryptoHasher("sha256").update(target).digest("hex"),
				});
			} else if (stat.isFile()) {
				if (stat.nlink !== 1) throw new Error(`Release file shares an inode: ${relative}`);
				entries.push({
					path: relative,
					kind: "file",
					mode: stat.mode & 0o777,
					sha256: await releaseFileSha256(absolute),
				});
			} else {
				throw new Error(`Unsupported release file type: ${relative}`);
			}
		}
	}
	await visit(root);
	return entries.sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0));
}

/** Verify installed bytes without importing any release code. A caller-supplied digest anchors manifest identity. */
export async function verifyRelease(
	releaseRoot: string,
	expectedManifestSha256?: string,
): Promise<StagedReleaseManifest> {
	const root = await fs.realpath(releaseRoot);
	const manifestPath = path.join(root, "manifest.json");
	const stat = await fs.lstat(manifestPath);
	if (!stat.isFile() || stat.nlink !== 1) throw new Error("Release manifest must be an independent regular file");
	const bytes = await Bun.file(manifestPath).arrayBuffer();
	const digest = new Bun.CryptoHasher("sha256").update(bytes).digest("hex");
	if (expectedManifestSha256 !== undefined && digest !== expectedManifestSha256) {
		throw new Error("Release manifest digest does not match the selected installation");
	}
	const manifest = JSON.parse(new TextDecoder().decode(bytes)) as StagedReleaseManifest;
	if (
		manifest.schemaVersion !== 1 ||
		manifest.status !== "STAGED" ||
		manifest.releaseRoot !== root ||
		manifest.platform !== process.platform ||
		manifest.arch !== process.arch ||
		manifest.source?.path !== "source" ||
		!/^[0-9a-f]{40,64}$/.test(manifest.source.commit) ||
		manifest.bun?.path !== "bin/bun" ||
		manifest.python?.path !== "python/bin/python" ||
		typeof manifest.python.executable !== "string" ||
		!path.isAbsolute(manifest.python.executable) ||
		manifest.python.editable !== false ||
		![manifest.python.contractSha256, manifest.python.migrationSetSha256, manifest.python.serviceFingerprint].every(
			value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value),
		) ||
		typeof manifest.python.serviceModule !== "string" ||
		!isContained(path.join(root, "python"), manifest.python.serviceModule) ||
		!Array.isArray(manifest.files) ||
		!Array.isArray(manifest.natives) ||
		manifest.natives.length === 0
	) {
		throw new Error("Invalid staged release manifest or incompatible host");
	}
	const actualFiles = await collectReleaseFiles(root, [manifest.python.executable]);
	if (actualFiles.length !== manifest.files.length)
		throw new Error("Release inventory changed: added or missing files");
	for (const [index, actual] of actualFiles.entries()) {
		const expected = manifest.files[index];
		if (
			!expected ||
			expected.path !== actual.path ||
			expected.kind !== actual.kind ||
			expected.mode !== actual.mode ||
			expected.sha256 !== actual.sha256 ||
			expected.target !== actual.target
		) {
			throw new Error(`Release inventory changed: ${actual.path}`);
		}
	}
	if ((await releaseFileSha256(path.join(root, manifest.bun.path))) !== manifest.bun.sha256) {
		throw new Error("Release Bun identity does not match its inventory");
	}
	if (
		(await fs.realpath(path.join(root, manifest.python.path))) !== manifest.python.executable ||
		(await releaseFileSha256(manifest.python.executable)) !== manifest.python.executableSha256
	) {
		throw new Error("External Python interpreter identity changed");
	}
	if ((await fs.realpath(manifest.python.serviceModule)) !== manifest.python.serviceModule) {
		throw new Error("Installed service module no longer resolves to its recorded path");
	}
	for (const native of manifest.natives) {
		const file = actualFiles.find(entry => entry.path === native.path);
		if (!file || file.kind !== "file" || file.sha256 !== native.sha256 || typeof native.version !== "string") {
			throw new Error("Native component identity does not match the release inventory");
		}
	}
	return manifest;
}
