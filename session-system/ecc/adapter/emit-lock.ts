/**
 * Build adapted.lock.json from the pinned mirror.
 *
 * This is the offline build step: it transforms the selected, installable
 * assets for the declared packs and records each asset's upstream and adapted
 * sha256 plus its owned files, the manifest-bytes pin, and — when the update
 * carries an owner review — the review file's path and content hash. The lock
 * is committed alongside the mirror, so a later run can verify that the mirror
 * still reproduces the recorded output without fetching upstream, and that the
 * manifest and any reviewed record have not drifted.
 *
 * The previous lock's review pin is preserved while its upstream commit still
 * matches the manifest's; an update that moves to a new candidate starts with
 * no review attached until the owner records one.
 *
 * Usage:
 *   bun session-system/ecc/adapter/emit-lock.ts [--ecc-root <dir>]          # write
 *   bun session-system/ecc/adapter/emit-lock.ts [--ecc-root <dir>] --check  # verify
 */
import * as path from "node:path";
import { ECC_ROOT, loadManifest } from "./catalog";
import { lockPathFor, readLock } from "./lock";
import { type BuildLock, verifyBuildLock, writeBuildLock } from "./pin";

function parseEccRoot(argv: string[]): string {
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i] ?? "";
		if (arg === "--ecc-root") {
			const value = argv[i + 1];
			if (!value || value.startsWith("-")) throw new Error("--ecc-root requires a value");
			return path.resolve(value);
		}
		if (arg.startsWith("--ecc-root=")) {
			const value = arg.slice("--ecc-root=".length);
			if (!value) throw new Error("--ecc-root requires a value");
			return path.resolve(value);
		}
	}
	return ECC_ROOT;
}

const argv = process.argv.slice(2);
const check = argv.includes("--check");

try {
	const eccRoot = parseEccRoot(argv);
	if (check) {
		await verifyBuildLock(eccRoot);
		process.stdout.write(`verified ${path.relative(eccRoot, lockPathFor(eccRoot))}\n`);
	} else {
		const manifest = await loadManifest(eccRoot);
		const existing = (await readLock(lockPathFor(eccRoot))) as BuildLock | null;
		const review = existing && existing.upstream.commit === manifest.upstream.commit ? existing.review : null;
		const lock = await writeBuildLock(eccRoot, review ?? null);
		const lockPath = lockPathFor(eccRoot);
		process.stdout.write(
			`wrote ${path.relative(eccRoot, lockPath)}: ${lock.assets.length} assets, review ${lock.review ? lock.review.path : "none"}\n`,
		);
	}
} catch (err) {
	process.stderr.write(`${err instanceof Error ? err.message : String(err)}\n`);
	process.exitCode = 1;
}
