/**
 * Upstream change detection for pinned ECC packs.
 *
 * Compares a candidate upstream checkout against the pinned mirror under
 * session-system/ecc/mirror for all consumed assets (installable packed assets
 * and their helper files). Reports changed/missing files, new files in consumed
 * skill directories, and overlay conflicts.
 *
 * Usage:
 *   bun session-system/ecc/adapter/upstream-check.ts --candidate-root <dir> --commit <sha> [--ecc-root <dir>]
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { ECC_ROOT, installablePackedAssets, loadManifest, type ManifestAsset } from "./catalog";
import { sha256Hex } from "./hash";

export type UpstreamCheckFileStatus = "unchanged" | "changed" | "missing";

export interface UpstreamCheckFile {
	assetId: string;
	path: string;
	status: UpstreamCheckFileStatus;
	fromSha256: string;
	toSha256: string | null;
}

export interface UpstreamCheckResult {
	baseCommit: string;
	candidateCommit: string;
	files: UpstreamCheckFile[];
	newFiles: string[];
	overlayConflicts: string[];
	changed: boolean;
}

export interface UpstreamCheckOptions {
	eccRoot?: string;
	candidateRoot: string;
	candidateCommit: string;
}

interface ConsumedFileItem {
	assetId: string;
	path: string;
	isPrimary: boolean;
	hasOverlay: boolean;
}

function getPrimaryPath(asset: ManifestAsset): string {
	return asset.adaptation.kind === "skill-namespaced"
		? path.posix.join(asset.upstream.path, "SKILL.md")
		: asset.upstream.path;
}

async function readBytesIfExists(filePath: string): Promise<Uint8Array | null> {
	try {
		return new Uint8Array(await Bun.file(filePath).arrayBuffer());
	} catch (err) {
		if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return null;
		throw err;
	}
}

async function collectNewFilesInSkill(
	candidateRoot: string,
	asset: ManifestAsset,
): Promise<string[]> {
	const skillDir = path.join(candidateRoot, asset.upstream.path);
	try {
		const stat = await fs.stat(skillDir);
		if (!stat.isDirectory()) return [];
	} catch (err) {
		if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return [];
		throw err;
	}

	const declaredDeps = new Set(
		asset.dependencies.map(d => path.posix.normalize(d.replaceAll("\\", "/"))),
	);
	const newFiles: string[] = [];

	const walk = async (currentDir: string): Promise<void> => {
		const entries = await fs.readdir(currentDir, { withFileTypes: true });
		for (const entry of entries) {
			const full = path.join(currentDir, entry.name);
			if (entry.isDirectory()) {
				await walk(full);
			} else if (entry.isFile()) {
				const relToSkill = path.posix.normalize(
					path.relative(skillDir, full).replaceAll("\\", "/"),
				);
				if (relToSkill !== "SKILL.md" && !declaredDeps.has(relToSkill)) {
					const relToCandidate = path.posix.join(asset.upstream.path, relToSkill);
					newFiles.push(relToCandidate);
				}
			}
		}
	};

	await walk(skillDir);
	return newFiles;
}

export async function checkUpstream(options: UpstreamCheckOptions): Promise<UpstreamCheckResult> {
	if (!options.candidateRoot) {
		throw new Error("candidateRoot is required");
	}
	if (!options.candidateCommit) {
		throw new Error("candidateCommit is required");
	}

	const eccRoot = path.resolve(options.eccRoot ?? ECC_ROOT);
	const candidateRoot = path.resolve(options.candidateRoot);
	const candidateCommit = options.candidateCommit;

	const manifest = await loadManifest(eccRoot);
	const baseCommit = manifest.upstream.commit;
	const assets = installablePackedAssets(manifest);

	const consumedItems: ConsumedFileItem[] = [];
	for (const asset of assets) {
		const hasOverlay = Boolean(asset.adaptation.overlay);
		const primaryPath = getPrimaryPath(asset);
		consumedItems.push({
			assetId: asset.id,
			path: primaryPath,
			isPrimary: true,
			hasOverlay,
		});

		if (asset.adaptation.kind === "skill-namespaced") {
			for (const dep of asset.dependencies) {
				const helperPath = path.posix.join(
					asset.upstream.path,
					path.posix.normalize(dep.replaceAll("\\", "/")),
				);
				consumedItems.push({
					assetId: asset.id,
					path: helperPath,
					isPrimary: false,
					hasOverlay,
				});
			}
		}
	}

	const files: UpstreamCheckFile[] = [];
	const primaryFilesByAssetId = new Map<string, UpstreamCheckFile>();

	for (const item of consumedItems) {
		const mirrorFullPath = path.join(eccRoot, "mirror", item.path);
		const candidateFullPath = path.join(candidateRoot, item.path);

		const mirrorBytes = await readBytesIfExists(mirrorFullPath);
		if (mirrorBytes === null) {
			throw new Error(`mirror file missing for '${item.path}' in '${eccRoot}/mirror'`);
		}
		const fromSha256 = sha256Hex(mirrorBytes);

		const candidateBytes = await readBytesIfExists(candidateFullPath);
		let status: UpstreamCheckFileStatus;
		let toSha256: string | null;

		if (candidateBytes === null) {
			status = "missing";
			toSha256 = null;
		} else {
			toSha256 = sha256Hex(candidateBytes);
			status = toSha256 === fromSha256 ? "unchanged" : "changed";
		}

		const checkFile: UpstreamCheckFile = {
			assetId: item.assetId,
			path: item.path,
			status,
			fromSha256,
			toSha256,
		};
		files.push(checkFile);

		if (item.isPrimary) {
			primaryFilesByAssetId.set(item.assetId, checkFile);
		}
	}

	files.sort((a, b) => a.path.localeCompare(b.path));

	const newFiles: string[] = [];
	for (const asset of assets) {
		if (asset.adaptation.kind === "skill-namespaced") {
			const skillNewFiles = await collectNewFilesInSkill(candidateRoot, asset);
			newFiles.push(...skillNewFiles);
		}
	}
	newFiles.sort((a, b) => a.localeCompare(b));

	const overlayConflicts: string[] = [];
	for (const asset of assets) {
		if (asset.adaptation.overlay) {
			const primaryFile = primaryFilesByAssetId.get(asset.id);
			if (primaryFile && primaryFile.status !== "unchanged") {
				overlayConflicts.push(asset.id);
			}
		}
	}
	overlayConflicts.sort((a, b) => a.localeCompare(b));

	const changed = files.some(f => f.status !== "unchanged") || newFiles.length > 0;

	return {
		baseCommit,
		candidateCommit,
		files,
		newFiles,
		overlayConflicts,
		changed,
	};
}

export interface UpstreamCheckCliArgs {
	candidateRoot: string;
	candidateCommit: string;
	eccRoot: string;
}

function takeOption(argv: string[], index: number, name: string): { value: string; next: number } {
	const arg = argv[index] ?? "";
	const prefix = `${name}=`;
	if (arg.startsWith(prefix)) {
		const value = arg.slice(prefix.length);
		if (value.length === 0) throw new Error(`${name} requires a value`);
		return { value, next: index };
	}
	const value = argv[index + 1];
	if (!value || value.startsWith("-")) throw new Error(`${name} requires a value`);
	return { value, next: index + 1 };
}

export function parseUpstreamCheckCliArgs(argv: string[]): UpstreamCheckCliArgs {
	let candidateRoot: string | undefined;
	let candidateCommit: string | undefined;
	let eccRoot = ECC_ROOT;

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i] ?? "";
		if (arg === "--candidate-root" || arg.startsWith("--candidate-root=")) {
			const taken = takeOption(argv, i, "--candidate-root");
			candidateRoot = taken.value;
			i = taken.next;
			continue;
		}
		if (arg === "--commit" || arg.startsWith("--commit=")) {
			const taken = takeOption(argv, i, "--commit");
			candidateCommit = taken.value;
			i = taken.next;
			continue;
		}
		if (arg === "--ecc-root" || arg.startsWith("--ecc-root=")) {
			const taken = takeOption(argv, i, "--ecc-root");
			eccRoot = taken.value;
			i = taken.next;
			continue;
		}
		if (arg.startsWith("-")) throw new Error(`unknown argument ${arg}`);
		throw new Error(`unexpected argument ${arg}`);
	}

	if (!candidateRoot) {
		throw new Error("--candidate-root requires a directory");
	}
	if (!candidateCommit) {
		throw new Error("--commit requires a commit hash");
	}

	return {
		candidateRoot: path.resolve(candidateRoot),
		candidateCommit,
		eccRoot: path.resolve(eccRoot),
	};
}

async function main(): Promise<void> {
	try {
		const args = parseUpstreamCheckCliArgs(process.argv.slice(2));
		const result = await checkUpstream(args);
		process.stdout.write(`${JSON.stringify(result)}\n`);
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		process.stdout.write(`${JSON.stringify({ error: message })}\n`);
		process.exitCode = 1;
	}
}

if (import.meta.main) {
	await main();
}
