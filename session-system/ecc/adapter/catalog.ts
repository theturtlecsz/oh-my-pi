/**
 * Manifest loading and validation.
 *
 * The manifest is the on-disk contract between the pinned mirror and the
 * installed output. Loading validates structural invariants that a silent
 * error would otherwise only surface as a broken install: unique ids, unique
 * targets, installable assets that carry a mirrored upstream file, and packs
 * that only reference declared assets.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { type Manifest, type ManifestAsset, NON_INSTALLABLE_DISPOSITIONS } from "./types";

export class ManifestError extends Error {
	constructor(message: string) {
		super(message);
		this.name = "ManifestError";
	}
}

export const ECC_ROOT = path.resolve(import.meta.dir, "..");
export const MIRROR_ROOT = path.join(ECC_ROOT, "mirror");

/** Assets that install to a destination (i.e. not reference/deferred/excluded). */
export function isInstallable(asset: ManifestAsset): boolean {
	return !NON_INSTALLABLE_DISPOSITIONS.includes(asset.disposition);
}

/** Assert the manifest is internally coherent before any transform runs. */
export function validateManifest(manifest: Manifest): void {
	if (manifest.schemaVersion !== 1) {
		throw new ManifestError(`manifest schemaVersion must be 1, got ${manifest.schemaVersion}`);
	}
	const ids = new Set<string>();
	for (const asset of manifest.assets) {
		if (ids.has(asset.id)) throw new ManifestError(`duplicate asset id '${asset.id}'`);
		ids.add(asset.id);
	}
	// Targets must be unique across installable assets, so two packs can never
	// silently overwrite each other's file.
	const targets = new Map<string, string>();
	for (const asset of manifest.assets.filter(isInstallable)) {
		const key = asset.target.path.replace(/^~\//, "");
		const owner = targets.get(key);
		if (owner) throw new ManifestError(`target collision: '${asset.id}' and '${owner}' both write '${key}'`);
		targets.set(key, asset.id);
	}
	for (const pack of manifest.packs) {
		const seen = new Set<string>();
		for (const assetId of pack.assetIds) {
			if (!ids.has(assetId)) throw new ManifestError(`pack '${pack.id}' references unknown asset '${assetId}'`);
			if (seen.has(assetId)) throw new ManifestError(`pack '${pack.id}' lists asset '${assetId}' twice`);
			seen.add(assetId);
		}
	}
	for (const rule of manifest.dispositionRules) {
		if (rule.prefix.includes("..") || rule.prefix.startsWith("/")) {
			throw new ManifestError(`disposition rule prefix must be relative, got '${rule.prefix}'`);
		}
	}
}

export async function loadManifest(eccRoot: string = ECC_ROOT): Promise<Manifest> {
	const manifestPath = path.join(eccRoot, "manifest.json");
	let parsed: unknown;
	try {
		parsed = JSON.parse(await Bun.file(manifestPath).text());
	} catch (err) {
		throw new ManifestError(`cannot read manifest at ${manifestPath}: ${err instanceof Error ? err.message : err}`);
	}
	const manifest = parsed as Manifest;
	validateManifest(manifest);
	return manifest;
}

/** Read a mirrored upstream file by its upstream path, verifying nothing yet. */
export async function readMirrorFile(eccRoot: string, upstreamPath: string): Promise<Uint8Array> {
	const full = path.join(eccRoot, "mirror", upstreamPath);
	const normalized = path.relative(path.join(eccRoot, "mirror"), full);
	if (normalized.startsWith("..") || path.isAbsolute(normalized)) {
		throw new ManifestError(`upstream path '${upstreamPath}' escapes the mirror root`);
	}
	try {
		return new Uint8Array(await Bun.file(full).arrayBuffer());
	} catch (err) {
		throw new ManifestError(
			`mirror file missing for '${upstreamPath}': ${err instanceof Error ? err.message : String(err)}`,
		);
	}
}

/** The absolute destination for an asset target under an install root. */
export function destinationPath(root: string, asset: ManifestAsset): string {
	return path.join(root, asset.target.path.replace(/^~\//, ""));
}

/**
 * The mirrored file an asset's primary content lives in. Skill assets name a
 * directory upstream; their content is that directory's SKILL.md.
 */
export function upstreamFilePath(eccRoot: string, asset: ManifestAsset): string {
	const relative =
		asset.adaptation.kind === "skill-namespaced"
			? path.posix.join(asset.upstream.path, "SKILL.md")
			: asset.upstream.path;
	return path.join(eccRoot, "mirror", relative);
}

/** Read an asset's primary mirrored content. */
export async function readAssetSource(eccRoot: string, asset: ManifestAsset): Promise<Uint8Array> {
	const full = upstreamFilePath(eccRoot, asset);
	const relative = path.relative(path.join(eccRoot, "mirror"), full);
	if (relative.startsWith("..") || path.isAbsolute(relative)) {
		throw new ManifestError(`asset '${asset.id}' path escapes the mirror root`);
	}
	try {
		return new Uint8Array(await Bun.file(full).arrayBuffer());
	} catch (err) {
		throw new ManifestError(
			`mirror file missing for '${asset.id}' (${relative}): ${err instanceof Error ? err.message : String(err)}`,
		);
	}
}

/** Every installable asset selected by at least one declared pack. */
export function installablePackedAssets(manifest: Manifest): ManifestAsset[] {
	const byId = new Map(manifest.assets.map(asset => [asset.id, asset]));
	const selected = new Set(manifest.packs.flatMap(pack => pack.assetIds));
	return [...selected]
		.map(id => byId.get(id))
		.filter((asset): asset is ManifestAsset => asset !== undefined && isInstallable(asset))
		.sort((a, b) => a.id.localeCompare(b.id));
}

/** Every asset for one pack, in pack order. */
export function assetsForPack(manifest: Manifest, packId: string): ManifestAsset[] {
	const pack = manifest.packs.find(entry => entry.id === packId);
	if (!pack) throw new ManifestError(`unknown pack '${packId}'`);
	const byId = new Map(manifest.assets.map(asset => [asset.id, asset]));
	return pack.assetIds
		.map(id => byId.get(id))
		.filter((asset): asset is ManifestAsset => asset !== undefined)
		.sort((a, b) => a.id.localeCompare(b.id));
}

/** List every mirrored upstream path, sorted, for drift reconciliation. */
export async function listMirrorFiles(eccRoot: string = ECC_ROOT): Promise<string[]> {
	const root = path.join(eccRoot, "mirror");
	const found: string[] = [];
	const walk = async (dir: string): Promise<void> => {
		const entries = await fs.readdir(dir, { withFileTypes: true });
		for (const entry of entries) {
			const full = path.join(dir, entry.name);
			if (entry.isDirectory()) await walk(full);
			else if (entry.isFile()) found.push(path.relative(root, full));
		}
	};
	await walk(root);
	return found.sort();
}
