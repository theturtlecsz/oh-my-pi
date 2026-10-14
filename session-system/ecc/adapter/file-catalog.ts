/** Complete, deterministic classification of the pinned upstream mirror. */
import { listMirrorFiles, readMirrorFile, validateManifest } from "./catalog";
import { sha256Hex } from "./hash";
import type { Disposition, Manifest } from "./types";

export interface FileCatalogRecord {
	path: string;
	sha256: string;
	classification: Disposition;
	reason: string;
	assetId: string | null;
}

export interface FileCatalog {
	schemaVersion: 1;
	upstream: { commit: string; tree: string };
	files: FileCatalogRecord[];
}

export async function buildFileCatalog(eccRoot: string, manifest: Manifest): Promise<FileCatalog> {
	validateManifest(manifest);
	const rules = [...manifest.dispositionRules].sort((a, b) => b.prefix.length - a.prefix.length);
	const files: FileCatalogRecord[] = [];
	for (const file of await listMirrorFiles(eccRoot)) {
		const claimants = manifest.assets.filter(asset => {
			const root = asset.upstream.path.replace(/\/$/, "");
			return file === root || file.startsWith(`${root}/`);
		});
		if (claimants.length > 1) {
			throw new Error(`multiple assets claim upstream file ${file}: ${claimants.map(asset => asset.id).join(", ")}`);
		}
		const asset = claimants[0];
		const disposition = asset ?? rules.find(rule => file.startsWith(rule.prefix));
		if (!disposition) throw new Error(`unclassified upstream file ${file}`);
		files.push({
			path: file,
			sha256: sha256Hex(await readMirrorFile(eccRoot, file)),
			classification: disposition.disposition,
			reason: disposition.reason,
			assetId: asset?.id ?? null,
		});
	}
	return {
		schemaVersion: 1,
		upstream: { commit: manifest.upstream.commit, tree: manifest.upstream.tree },
		files,
	};
}
