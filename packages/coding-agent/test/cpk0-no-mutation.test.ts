import { afterEach, describe, expect, it, mock, spyOn } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { measureBaseline } from "../../../scripts/cpk0-baseline";
import { generateInventory } from "../../../scripts/cpk0-inventory";
import * as git from "../src/utils/git";

const repoRoot = path.resolve(import.meta.dir, "../../..");

export interface GitFileEntry {
	path: string;
	size: number;
	mtimeMs: number;
}

export interface GitSnapshot {
	entries: GitFileEntry[];
	objectsCount: number;
	inventorySha256: string;
	baselineSha256: string;
}

async function hashFile(filePath: string): Promise<string> {
	try {
		const bytes = await Bun.file(filePath).bytes();
		const hasher = new Bun.CryptoHasher("sha256");
		hasher.update(bytes);
		return hasher.digest("hex");
	} catch {
		return "";
	}
}

const VOLATILE_GIT_FILES = new Set(["FETCH_HEAD", "ORIG_HEAD", "COMMIT_EDITMSG", "index", "gc.log"]);
const VOLATILE_GIT_TREES = new Set(["logs", "worktrees"]);

/**
 * Whether a path relative to a git dir is rewritten by OTHER processes of the
 * same clone. In a linked worktree, commonDir is the primary clone's `.git`:
 * every sibling worktree's `git fetch`/`rebase`/`commit` rewrites FETCH_HEAD,
 * ORIG_HEAD, COMMIT_EDITMSG, index, `logs/`, `worktrees/<name>/`, and `*.lock`.
 * Discovery and baseline never touch those, so snapshotting them only makes the
 * zero-mutation assertion flake when worktrees of one clone run git concurrently.
 */
function isVolatileGitPath(relPath: string): boolean {
	const rel = relPath.replaceAll("\\", "/");
	if (rel.endsWith(".lock")) return true;
	const top = rel.split("/", 1)[0] ?? rel;
	if (VOLATILE_GIT_TREES.has(top)) return true;
	return !rel.includes("/") && VOLATILE_GIT_FILES.has(rel);
}

/**
 * Snapshot git directory metadata:
 * Walks gitDir and commonDir with Bun.Glob("**\/*") (dot: true), skipping objects/**,
 * recording sorted { path, size, mtimeMs }, counting objects/** entries separately,
 * and hashing surface-inventory.json and baseline.json.
 *
 * Volatile shared-clone paths (see {@link isVolatileGitPath}) are skipped so that
 * concurrent worktrees of the same clone cannot change the snapshot. Stable shared
 * state — HEAD, config, refs/, packed-refs, hooks/, info/ — stays in the snapshot,
 * so a real mutation to a branch or HEAD is still detected.
 *
 * Does not invoke git status to prevent index refreshes or diffs.
 */
export async function snapshotGit(gitDir: string, commonDir: string, root: string = repoRoot): Promise<GitSnapshot> {
	const entries: GitFileEntry[] = [];
	let objectsCount = 0;
	const seen = new Set<string>();

	const dirs = Array.from(new Set([path.resolve(gitDir), path.resolve(commonDir)]));
	for (const dir of dirs) {
		const glob = new Bun.Glob("**/*");
		for (const rel of glob.scanSync({ cwd: dir, dot: true })) {
			const normalizedRel = rel.replaceAll("\\", "/");
			if (normalizedRel.startsWith("objects/") || normalizedRel === "objects") {
				objectsCount++;
				continue;
			}
			if (isVolatileGitPath(normalizedRel)) continue;
			const fullPath = path.resolve(dir, rel);
			if (seen.has(fullPath)) continue;
			seen.add(fullPath);

			const stat = await fs.stat(fullPath);
			entries.push({
				path: fullPath,
				size: stat.size,
				mtimeMs: stat.mtimeMs,
			});
		}
	}

	entries.sort((a, b) => a.path.localeCompare(b.path));

	const inventoryPath = path.join(root, "docs/cpk0/surface-inventory.json");
	const baselinePath = path.join(root, "docs/cpk0/baseline.json");

	return {
		entries,
		objectsCount,
		inventorySha256: await hashFile(inventoryPath),
		baselineSha256: await hashFile(baselinePath),
	};
}

describe("CPK-0 zero mutation verification (OMP-204-s05)", () => {
	afterEach(() => {
		mock.restore();
	});

	it("discovery and baseline paths perform zero Git, network, or source mutations", async () => {
		const repo = await git.repo.resolve(repoRoot);
		expect(repo).not.toBeNull();
		const { gitDir, commonDir } = repo!;

		const snapshotA = await snapshotGit(gitDir, commonDir);
		const fetchSpy = spyOn(globalThis, "fetch");

		generateInventory(repoRoot);
		await measureBaseline(1);

		const snapshotB = await snapshotGit(gitDir, commonDir);

		expect(fetchSpy).toHaveBeenCalledTimes(0);
		expect(snapshotA).toEqual(snapshotB);
	}, 120000);

	it("negative control: detects modification to git repository files in a fake git dir", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "cpk0-no-mutation-fake-git-"));
		try {
			await Bun.write(path.join(tempDir, "HEAD"), "ref: refs/heads/main\n");
			await Bun.write(path.join(tempDir, "config"), "[core]\n\trepositoryformatversion = 0\n");
			await Bun.write(path.join(tempDir, "refs/heads/x"), "1111111111111111111111111111111111111111\n");

			const snap1 = await snapshotGit(tempDir, tempDir);

			await Bun.sleep(20);
			await Bun.write(path.join(tempDir, "refs/heads/x"), "2222222222222222222222222222222222222222\n");

			const snap2 = await snapshotGit(tempDir, tempDir);

			expect(snap1).not.toEqual(snap2);
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});

	it("ignores volatile paths a concurrent worktree of the same clone rewrites", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "cpk0-no-mutation-volatile-"));
		try {
			await Bun.write(path.join(tempDir, "HEAD"), "ref: refs/heads/main\n");
			await Bun.write(path.join(tempDir, "config"), "[core]\n\trepositoryformatversion = 0\n");
			await Bun.write(path.join(tempDir, "refs/heads/main"), "1111111111111111111111111111111111111111\n");
			await Bun.write(path.join(tempDir, "packed-refs"), "# pack-refs with: peeled\n");

			const snap1 = await snapshotGit(tempDir, tempDir);

			await Bun.sleep(20);
			// Paths a concurrent fetch/rebase/commit in a sibling worktree rewrites.
			await Bun.write(path.join(tempDir, "FETCH_HEAD"), "2222\t\tbranch 'main' of local\n");
			await Bun.write(path.join(tempDir, "ORIG_HEAD"), "3333333333333333333333333333333333333333\n");
			await Bun.write(path.join(tempDir, "COMMIT_EDITMSG"), "sibling commit\n");
			await Bun.write(path.join(tempDir, "index"), "sibling index bytes");
			await Bun.write(path.join(tempDir, "gc.log"), "sibling gc\n");
			await Bun.write(path.join(tempDir, "index.lock"), "lock");
			await Bun.write(path.join(tempDir, "logs/HEAD"), "sibling reflog\n");
			await Bun.write(path.join(tempDir, "worktrees/x/index"), "sibling worktree index");

			const snap2 = await snapshotGit(tempDir, tempDir);

			expect(snap1).toEqual(snap2);
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});

	it("negative control: detects a HEAD change in the snapshotted set", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "cpk0-no-mutation-head-"));
		try {
			await Bun.write(path.join(tempDir, "HEAD"), "ref: refs/heads/main\n");
			await Bun.write(path.join(tempDir, "refs/heads/main"), "1111111111111111111111111111111111111111\n");

			const snap1 = await snapshotGit(tempDir, tempDir);

			await Bun.sleep(20);
			await Bun.write(path.join(tempDir, "HEAD"), "ref: refs/heads/other\n");

			const snap2 = await snapshotGit(tempDir, tempDir);

			expect(snap1).not.toEqual(snap2);
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});
});
