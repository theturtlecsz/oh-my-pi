/**
 * Build adapted.lock.json from the pinned mirror.
 *
 * This is the offline build step: it transforms the selected, installable
 * assets for the declared packs and records each asset's upstream and adapted
 * sha256 plus its owned files. The lock is committed alongside the mirror, so a
 * later run can verify that the mirror still reproduces the recorded output
 * without fetching upstream.
 *
 * Usage: bun session-system/ecc/adapter/emit-lock.ts
 */
import * as path from "node:path";
import { buildPlan } from "./apply";
import { ECC_ROOT, installablePackedAssets, loadManifest } from "./catalog";
import { lockFromPlan, lockPathFor, serializeLock } from "./lock";

const manifest = await loadManifest(ECC_ROOT);
const assets = installablePackedAssets(manifest);
const plan = await buildPlan({ eccRoot: ECC_ROOT, projectRoot: ECC_ROOT }, manifest, assets);
const lock = lockFromPlan(plan, manifest.upstream, manifest.transformVersion);
const lockPath = lockPathFor(ECC_ROOT);
await Bun.write(lockPath, serializeLock(lock));
process.stdout.write(
	`wrote ${path.relative(ECC_ROOT, lockPath)}: ${lock.assets.length} assets, ${plan.files.length} files\n`,
);
