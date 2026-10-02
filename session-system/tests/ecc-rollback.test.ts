import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { install, update } from "../ecc/adapter/apply";
import { assetsForPack, ECC_ROOT, loadManifest } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { readLock } from "../ecc/adapter/lock";
import { verifyBuildLock } from "../ecc/adapter/pin";
import { repin } from "../ecc/adapter/repin";
import type { Manifest, ManifestAsset, UpdateReview } from "../ecc/adapter/types";
import { type UpstreamCheckResult, checkUpstream } from "../ecc/adapter/upstream-check";

const repoRoot = path.resolve(import.meta.dir, "../..");
const cliScript = path.join(repoRoot, "session-system/ecc/adapter/cli.ts");
const mirrorRoot = path.join(ECC_ROOT, "mirror");

const CANDIDATE_COMMIT = "c".repeat(40);
const CANDIDATE_TREE = "d".repeat(40);
const RESEARCH_OPS_EDIT = "<!-- rollback drill test edit 1 -->\n";
const EVALUATE_PY_EDIT = "# rollback drill test edit 2\n";

const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
});

async function makeTempDir(prefix: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

async function makeEccCopy(): Promise<string> {
	const parent = await makeTempDir("ecc-rollback-");
	const root = path.join(parent, "ecc");
	await fs.cp(ECC_ROOT, root, { recursive: true });
	return root;
}

async function makeProject(): Promise<string> {
	const dir = await makeTempDir("ecc-rollback-project-");
	await fs.mkdir(path.join(dir, ".git"), { recursive: true });
	return dir;
}

async function makeCandidate(): Promise<string> {
	const dir = await makeTempDir("ecc-rollback-candidate-");
	await fs.cp(mirrorRoot, dir, { recursive: true });
	const f1 = path.join(dir, "skills/research-ops/SKILL.md");
	await Bun.write(f1, `${await Bun.file(f1).text()}\n${RESEARCH_OPS_EDIT}`);
	const f2 = path.join(dir, "skills/agent-self-evaluation/scripts/evaluate.py");
	await Bun.write(f2, `${await Bun.file(f2).text()}\n${EVALUATE_PY_EDIT}`);
	return dir;
}

function projectAssets(manifest: Manifest): ManifestAsset[] {
	const byId = new Map<string, ManifestAsset>();
	for (const packId of ["ecc-engineering", "ecc-research"]) {
		for (const asset of assetsForPack(manifest, packId)) {
			byId.set(asset.id, asset);
		}
	}
	return [...byId.values()].sort((a, b) => a.id.localeCompare(b.id));
}

async function byteSnapshot(dir: string): Promise<Map<string, Uint8Array>> {
	const files = new Map<string, Uint8Array>();
	const walk = async (current: string): Promise<void> => {
		for (const entry of await fs.readdir(current, { withFileTypes: true }).catch(() => [])) {
			const full = path.join(current, entry.name);
			if (entry.isDirectory()) await walk(full);
			else if (entry.isFile()) {
				const bytes = new Uint8Array(await Bun.file(full).arrayBuffer());
				files.set(path.relative(dir, full), bytes);
			}
		}
	};
	await walk(dir);
	return files;
}

async function pruneEmptyDirs(root: string): Promise<void> {
	const walk = async (current: string): Promise<boolean> => {
		let isEmpty = true;
		for (const entry of await fs.readdir(current, { withFileTypes: true }).catch(() => [])) {
			const full = path.join(current, entry.name);
			if (entry.isDirectory()) {
				const childEmpty = await walk(full);
				if (childEmpty) {
					await fs.rmdir(full).catch(() => {});
				} else {
					isEmpty = false;
				}
			} else {
				isEmpty = false;
			}
		}
		return isEmpty;
	};
	await walk(root);
}

async function restoreSnapshot(dir: string, snapshot: Map<string, Uint8Array>): Promise<void> {
	const walk = async (current: string): Promise<void> => {
		for (const entry of await fs.readdir(current, { withFileTypes: true }).catch(() => [])) {
			const full = path.join(current, entry.name);
			if (entry.isDirectory()) {
				await walk(full);
			} else if (entry.isFile()) {
				const rel = path.relative(dir, full);
				if (!snapshot.has(rel)) await fs.rm(full, { force: true });
			}
		}
	};
	await walk(dir);
	await pruneEmptyDirs(dir);
	for (const [rel, bytes] of snapshot.entries()) {
		await Bun.write(path.join(dir, rel), bytes);
	}
}

function expectSnapshotsEqual(actual: Map<string, Uint8Array>, expected: Map<string, Uint8Array>): void {
	const actualPaths = [...actual.keys()].sort();
	const expectedPaths = [...expected.keys()].sort();
	expect(actualPaths).toEqual(expectedPaths);
	for (const p of expectedPaths) {
		const actBytes = actual.get(p)!;
		const expBytes = expected.get(p)!;
		expect(sha256Hex(actBytes)).toBe(sha256Hex(expBytes));
		expect(actBytes).toEqual(expBytes);
	}
}

function makeAcceptedReview(report: UpstreamCheckResult): UpdateReview {
	return {
		schemaVersion: 1,
		baseCommit: report.baseCommit,
		candidate: {
			commit: CANDIDATE_COMMIT,
			tree: CANDIDATE_TREE,
			version: "2.2.2",
			describe: "v2.2.2-1-gcccccccc",
		},
		changes: report.files
			.filter(file => file.status === "changed")
			.map(file => ({
				assetId: file.assetId,
				path: file.path,
				fromSha256: file.fromSha256,
				toSha256: file.toSha256 as string,
			})),
		newFiles: [...report.newFiles],
		reviewer: "owner",
		reviewedAt: "2026-10-02T00:00:00Z",
		decision: "accept",
		notes: "reviewed upstream change",
	};
}

async function writeReview(eccRoot: string, review: unknown): Promise<string> {
	const full = path.join(eccRoot, "reviews", "update.json");
	await fs.mkdir(path.dirname(full), { recursive: true });
	await Bun.write(full, `${JSON.stringify(review, null, 2)}\n`);
	return full;
}

async function runCli(args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
	const proc = Bun.spawn(["bun", cliScript, ...args], {
		cwd: repoRoot,
		stdin: "ignore",
		stdout: "pipe",
		stderr: "pipe",
	});
	const [stdout, stderr, code] = await Promise.all([
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
		proc.exited,
	]);
	return { code, stdout, stderr };
}

describe("ECC pack update rollback drill", () => {
	test("reverts ECC root to pre-update snapshot and rolls back project tree to P_A byte for byte", async () => {
		// 1. eccRoot = temp copy of ECC_ROOT; snapshot A = every file path and byte under it.
		const eccRoot = await makeEccCopy();
		const snapshotA = await byteSnapshot(eccRoot);

		// 2. project = temp dir with .git/; install ecc-engineering and ecc-research packs (adapter/apply.ts install, catalog.ts assetsForPack); snapshot P_A of the project tree.
		const projectRoot = await makeProject();
		const initialManifest = await loadManifest(eccRoot);
		const initialAssets = projectAssets(initialManifest);
		const installResult = await install({ eccRoot, projectRoot }, initialManifest, initialAssets);
		expect(installResult.status).toBe("installed");
		const snapshotP_A = await byteSnapshot(projectRoot);
		const initialLockBytes = new Uint8Array(
			await Bun.file(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json")).arrayBuffer(),
		);

		// 3. Candidate = temp copy of mirror/ with skills/research-ops/SKILL.md and skills/agent-self-evaluation/scripts/evaluate.py edited.
		// Write an accepted review that matches checkUpstream (adapter/upstream-check.ts) and run repin (adapter/repin.ts).
		const candidateRoot = await makeCandidate();
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		expect(report.changed).toBe(true);
		expect(report.files.some(f => f.status === "missing")).toBe(false);
		expect(report.overlayConflicts).toEqual([]);
		const reviewPath = await writeReview(eccRoot, makeAcceptedReview(report));
		const repinResult = await repin({ eccRoot, candidateRoot, review: reviewPath });
		expect(repinResult.candidateCommit).toBe(CANDIDATE_COMMIT);
		expect(repinResult.changedFiles.sort()).toEqual([
			"skills/agent-self-evaluation/scripts/evaluate.py",
			"skills/research-ops/SKILL.md",
		]);

		// 4. update the project (apply.ts update): the two changed files differ from P_A; the project lock upstream.commit is the candidate commit.
		const updatedManifest = await loadManifest(eccRoot);
		const updatedAssets = projectAssets(updatedManifest);
		const updateResult = await update({ eccRoot, projectRoot }, updatedManifest, updatedAssets);
		expect(updateResult.status).toBe("updated");

		const researchOpsRel = path.join(".omp", "skills", "ecc-research-ops", "SKILL.md");
		const evaluatePyRel = path.join(".omp", "skills", "ecc-agent-self-evaluation", "scripts", "evaluate.py");

		const researchOpsPostUpdate = new Uint8Array(
			await Bun.file(path.join(projectRoot, researchOpsRel)).arrayBuffer(),
		);
		const evaluatePyPostUpdate = new Uint8Array(
			await Bun.file(path.join(projectRoot, evaluatePyRel)).arrayBuffer(),
		);

		expect(researchOpsPostUpdate).not.toEqual(snapshotP_A.get(researchOpsRel)!);
		expect(evaluatePyPostUpdate).not.toEqual(snapshotP_A.get(evaluatePyRel)!);

		const candidateLock = await readLock(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json"));
		expect(candidateLock?.upstream.commit).toBe(CANDIDATE_COMMIT);

		// 5. Rollback: replace eccRoot with snapshot A (delete files not in A, restore bytes); then update the project again.
		await restoreSnapshot(eccRoot, snapshotA);
		expectSnapshotsEqual(await byteSnapshot(eccRoot), snapshotA);

		// Done when:
		// - after step 5, verifyBuildLock (adapter/pin.ts) passes and checkUpstream with the step-3 candidate again reports both edited files as changed (the pin is back at A).
		await expect(verifyBuildLock(eccRoot)).resolves.toBeUndefined();

		const postRollbackReport = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		expect(postRollbackReport.changed).toBe(true);
		expect(postRollbackReport.baseCommit).toBe(report.baseCommit);
		const changedReportFiles = postRollbackReport.files.filter(f => f.status === "changed");
		expect(changedReportFiles.map(f => f.path).sort()).toEqual([
			"skills/agent-self-evaluation/scripts/evaluate.py",
			"skills/research-ops/SKILL.md",
		]);

		// update the project again
		const rolledBackManifest = await loadManifest(eccRoot);
		const rolledBackAssets = projectAssets(rolledBackManifest);
		const rollbackUpdateResult = await update({ eccRoot, projectRoot }, rolledBackManifest, rolledBackAssets);
		expect(rollbackUpdateResult.status).toBe("updated");
		expect(rollbackUpdateResult.written.sort()).toEqual([evaluatePyRel, researchOpsRel].sort());

		// - the project tree equals P_A byte for byte, and its .omp/ecc/adapted.lock.json equals the one written by the first install.
		const snapshotPostRollback = await byteSnapshot(projectRoot);
		expectSnapshotsEqual(snapshotPostRollback, snapshotP_A);

		const rolledBackLockBytes = new Uint8Array(
			await Bun.file(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json")).arrayBuffer(),
		);
		expect(rolledBackLockBytes).toEqual(initialLockBytes);

		// - a second rollback update reports status "unchanged".
		const secondRollbackUpdate = await update({ eccRoot, projectRoot }, rolledBackManifest, rolledBackAssets);
		expect(secondRollbackUpdate.status).toBe("unchanged");
		expect(secondRollbackUpdate.written).toEqual([]);
	});

	test("reproduces rollback drill end-to-end using cli.ts update", async () => {
		// 1. eccRoot copy and snapshot A
		const eccRoot = await makeEccCopy();
		const snapshotA = await byteSnapshot(eccRoot);

		// 2. project with initial install
		const projectRoot = await makeProject();
		const initialManifest = await loadManifest(eccRoot);
		const initialAssets = projectAssets(initialManifest);
		const installResult = await install({ eccRoot, projectRoot }, initialManifest, initialAssets);
		expect(installResult.status).toBe("installed");
		const snapshotP_A = await byteSnapshot(projectRoot);
		const initialLockBytes = new Uint8Array(
			await Bun.file(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json")).arrayBuffer(),
		);

		// 3. candidate with the two edited files and repin
		const candidateRoot = await makeCandidate();
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		const reviewPath = await writeReview(eccRoot, makeAcceptedReview(report));
		await repin({ eccRoot, candidateRoot, review: reviewPath });

		// 4. CLI update
		const updateCli = await runCli(["update", "--project", projectRoot, "--ecc-root", eccRoot]);
		expect(updateCli.code).toBe(0);
		const updateBody = JSON.parse(updateCli.stdout) as { status: string; written: string[] };
		expect(updateBody.status).toBe("updated");

		const candidateLock = await readLock(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json"));
		expect(candidateLock?.upstream.commit).toBe(CANDIDATE_COMMIT);

		// 5. Rollback eccRoot to snapshot A
		await restoreSnapshot(eccRoot, snapshotA);
		await expect(verifyBuildLock(eccRoot)).resolves.toBeUndefined();

		// CLI rollback update
		const rollbackCli = await runCli(["update", "--project", projectRoot, "--ecc-root", eccRoot]);
		expect(rollbackCli.code).toBe(0);
		const rollbackBody = JSON.parse(rollbackCli.stdout) as { status: string; written: string[] };
		expect(rollbackBody.status).toBe("updated");

		// project tree equals P_A byte for byte and lock equals first install
		const snapshotPostRollback = await byteSnapshot(projectRoot);
		expectSnapshotsEqual(snapshotPostRollback, snapshotP_A);

		const rolledBackLockBytes = new Uint8Array(
			await Bun.file(path.join(projectRoot, ".omp", "ecc", "adapted.lock.json")).arrayBuffer(),
		);
		expect(rolledBackLockBytes).toEqual(initialLockBytes);

		// second CLI rollback update reports unchanged
		const secondCli = await runCli(["update", "--project", projectRoot, "--ecc-root", eccRoot]);
		expect(secondCli.code).toBe(0);
		const secondBody = JSON.parse(secondCli.stdout) as { status: string; written: string[] };
		expect(secondBody.status).toBe("unchanged");
		expect(secondBody.written).toEqual([]);
	});
});
