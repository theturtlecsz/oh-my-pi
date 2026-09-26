import { afterEach, describe, expect, it, mock, spyOn } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { isEnoent } from "@oh-my-pi/pi-utils";
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
	/** Raw contents of this worktree's `HEAD` (empty when unreadable). */
	headContent: string;
	/** Full ref this worktree's HEAD points at (`refs/heads/<name>`), or null when detached. */
	headRef: string | null;
	/** SHA the {@link headRef} resolves to (loose file or `packed-refs`), or null. */
	headRefValue: string | null;
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

const VOLATILE_GIT_FILES = new Set(["FETCH_HEAD", "ORIG_HEAD", "COMMIT_EDITMSG", "index", "gc.log", "gc.pid"]);
const VOLATILE_GIT_TREES = new Set(["logs", "worktrees"]);

/**
 * Identity of the git dirs a snapshot walks. `sharedStore` is true for the
 * common dir of a linked worktree — the primary clone's `.git` — which every
 * sibling worktree and git's automatic gc mutate concurrently.
 */
interface SnapshotGitContext {
	sharedStore: boolean;
}

/**
 * Whether a path relative to a git dir is rewritten by OTHER processes of the
 * same clone. In a linked worktree, commonDir is the primary clone's `.git`:
 * every sibling worktree's `git fetch`/`rebase`/`commit` rewrites FETCH_HEAD,
 * ORIG_HEAD, COMMIT_EDITMSG, index, `logs/`, `worktrees/<name>/`, and `*.lock`.
 * Discovery and baseline never touch those, so snapshotting them only makes the
 * zero-mutation assertion flake when worktrees of one clone run git concurrently.
 *
 * A shared common dir also exposes the whole ref store and gc bookkeeping: a
 * sibling's commit moves its own branch ref, and git's automatic gc packs loose
 * refs into `packed-refs`, rewrites `info/refs`, and writes/removes `gc.pid`.
 * None of that is this worktree's state, so the entire ref store is volatile
 * there. This worktree's own HEAD and current branch ref are captured as
 * explicit snapshot fields instead (see {@link snapshotGit}) — a change to
 * either still fails the check, while a sibling's commit cannot.
 *
 * A stand-alone repo (`sharedStore` false) keeps its entire ref store — the
 * fake-git-dir negative controls rely on that.
 */
function isVolatileGitPath(relPath: string, context: SnapshotGitContext): boolean {
	const rel = relPath.replaceAll("\\", "/");
	if (rel.endsWith(".lock")) return true;
	const top = rel.split("/", 1)[0] ?? rel;
	const base = rel.slice(rel.lastIndexOf("/") + 1);
	if (VOLATILE_GIT_TREES.has(top)) return true;
	if (!rel.includes("/") && VOLATILE_GIT_FILES.has(rel)) return true;
	if (!context.sharedStore) return false;
	// The common dir's own `HEAD` is the primary worktree's, not this one's (this
	// worktree's HEAD lives in its private admin dir and is captured as
	// `headContent`). Loose refs (`refs/**`), the refs-packed file and its
	// temporaries (`packed-refs.new`), and gc's rewritten `info/refs` (plus its
	// `info/refs_<suffix>` rename temporaries) are the rest of the shared ref
	// store; the current branch's value is captured separately by resolving it.
	if (rel === "HEAD") return true;
	if (top === "refs") return true;
	if (base.startsWith("packed-refs")) return true;
	if (top === "info" && base.startsWith("refs")) return true;
	return false;
}

/** Raw contents of `HEAD`, or "" when it cannot be read. */
async function readHeadContent(headPath: string): Promise<string> {
	try {
		return await Bun.file(headPath).text();
	} catch {
		return "";
	}
}

/** Full ref `HEAD` points at (`refs/heads/<name>`), or null when detached. */
function parseHeadRef(headContent: string): string | null {
	return /^ref:\s*(\S+)\s*$/u.exec(headContent.trim())?.[1] ?? null;
}

/** First 40-hex column of a `packed-refs` file for `ref`, or null. */
function parsePackedRef(packedRefs: string, ref: string): string | null {
	for (const line of packedRefs.split("\n")) {
		if (line.startsWith("#") || line.startsWith("^")) continue;
		const [sha, name] = line.trim().split(/\s+/u);
		if (name === ref && sha && /^[0-9a-f]{40}$/iu.test(sha)) return sha;
	}
	return null;
}

/**
 * Resolve `headRef` to a SHA the way git does — the loose file under `refs/`
 * first, then `packed-refs` — without running a git subprocess. Returns null
 * when detached or unresolvable.
 */
async function resolveRefValue(gitDir: string, commonDir: string, headRef: string | null): Promise<string | null> {
	if (!headRef) return null;
	const read = async (file: string): Promise<string | null> => {
		try {
			const text = (await Bun.file(file).text()).trim();
			return /^[0-9a-f]{40}$/iu.test(text) ? text : null;
		} catch {
			return null;
		}
	};
	const loose = await read(path.join(gitDir, headRef));
	if (loose) return loose;
	const fromCommon = await read(path.join(commonDir, headRef));
	if (fromCommon) return fromCommon;
	try {
		return parsePackedRef(await Bun.file(path.join(commonDir, "packed-refs")).text(), headRef);
	} catch {
		return null;
	}
}

/**
 * Snapshot git directory metadata:
 * Walks gitDir and commonDir with Bun.Glob("**\/*") (dot: true), skipping objects/**,
 * recording sorted { path, size, mtimeMs }, counting objects/** entries separately,
 * and hashing surface-inventory.json and baseline.json.
 *
 * Volatile shared-clone paths (see {@link isVolatileGitPath}) are skipped so that
 * concurrent worktrees of the same clone cannot change the snapshot. The shared
 * object store and ref store of a linked worktree's common dir are not walked
 * either: siblings add loose objects, git's automatic gc packs and prunes them,
 * packs loose refs into `packed-refs`, and rewrites `info/refs`. This worktree's
 * own HEAD content and current branch ref value ARE compared (as explicit
 * fields), so a real mutation to either still fails the check. A repo no other
 * process touches (gitDir === commonDir) still walks its objects and refs, so a
 * branch or object mutation there is detected too.
 *
 * Files that vanish mid-walk (git's atomic renames) are tolerated: the snapshot
 * captures a directory that other processes may be rewriting, and a file absent
 * from both ends of a comparison is not a mutation by discovery or baseline.
 *
 * Does not invoke git status to prevent index refreshes or diffs.
 */
export async function snapshotGit(gitDir: string, commonDir: string, root: string = repoRoot): Promise<GitSnapshot> {
	const entries: GitFileEntry[] = [];
	let objectsCount = 0;
	const seen = new Set<string>();

	const privateDir = path.resolve(gitDir);
	const commonStoreDir = path.resolve(commonDir);
	const headContent = await readHeadContent(path.join(privateDir, "HEAD"));
	const headRef = parseHeadRef(headContent);
	const headRefValue = await resolveRefValue(privateDir, commonStoreDir, headRef);

	const dirs = Array.from(new Set([privateDir, commonStoreDir]));
	for (const dir of dirs) {
		const context: SnapshotGitContext = { sharedStore: dir !== privateDir };
		const glob = new Bun.Glob("**/*");
		for (const rel of glob.scanSync({ cwd: dir, dot: true })) {
			const normalizedRel = rel.replaceAll("\\", "/");
			if (normalizedRel.startsWith("objects/") || normalizedRel === "objects") {
				if (!context.sharedStore) objectsCount++;
				continue;
			}
			if (isVolatileGitPath(normalizedRel, context)) continue;
			const fullPath = path.resolve(dir, rel);
			if (seen.has(fullPath)) continue;
			seen.add(fullPath);

			// git renames files (info/refs, packed-refs) under us; a file gone by the
			// time we stat it is not part of this snapshot.
			const stat = await fs.stat(fullPath).catch((err: unknown) => {
				if (isEnoent(err)) return null;
				throw err;
			});
			if (!stat) continue;
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
		headContent,
		headRef,
		headRefValue,
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

	it("negative control: detects a new or removed object in a stand-alone git dir", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "cpk0-no-mutation-objects-"));
		try {
			await Bun.write(path.join(tempDir, "HEAD"), "ref: refs/heads/main\n");
			await Bun.write(path.join(tempDir, "objects/aa/first"), "object bytes");

			const snap1 = await snapshotGit(tempDir, tempDir);

			await Bun.write(path.join(tempDir, "objects/bb/second"), "another object");
			const snap2 = await snapshotGit(tempDir, tempDir);
			expect(snap2.objectsCount).toBe(snap1.objectsCount + 1);
			expect(snap2).not.toEqual(snap1);

			await fs.rm(path.join(tempDir, "objects/bb/second"));
			const snap3 = await snapshotGit(tempDir, tempDir);
			expect(snap3.objectsCount).toBe(snap1.objectsCount);
			expect(snap3).not.toEqual(snap2);
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});

	it("treats a sibling worktree's ref churn in the common dir as volatile but keeps this worktree's branch ref", async () => {
		const root = await fs.mkdtemp(path.join(os.tmpdir(), "cpk0-no-mutation-linked-"));
		const commonDir = path.join(root, ".git");
		const gitDir = path.join(commonDir, "worktrees", "OMP-343");
		try {
			// Shared store: HEAD of the primary clone, refs for many branches, packed refs.
			await Bun.write(path.join(commonDir, "HEAD"), "ref: refs/heads/main\n");
			const branchSha = "1111111111111111111111111111111111111111\n";
			await Bun.write(path.join(commonDir, "refs/heads/main"), "1111111111111111111111111111111111111111\n");
			await Bun.write(path.join(commonDir, "refs/heads/OMP-343"), branchSha);
			await Bun.write(path.join(commonDir, "refs/heads/sibling"), branchSha);
			await Bun.write(path.join(commonDir, "packed-refs"), "# pack-refs with: peeled fully-peeled sorted\n");
			await Bun.write(path.join(commonDir, "info/refs"), `${branchSha}\trefs/heads/main\n`);
			await Bun.write(path.join(commonDir, "objects/aa/first"), "object bytes");
			await Bun.write(path.join(commonDir, "config"), "[core]\n\trepositoryformatversion = 0\n");
			// This worktree's private admin dir.
			await Bun.write(path.join(gitDir, "HEAD"), "ref: refs/heads/OMP-343\n");
			await Bun.write(path.join(gitDir, "commondir"), "../..\n");
			await Bun.write(path.join(gitDir, "index"), "index bytes");

			const snapBase = await snapshotGit(gitDir, commonDir);

			await Bun.sleep(20);
			// A sibling worktree commits (loose object + its own ref) and git's automatic
			// gc packs every loose ref into packed-refs and rewrites info/refs.
			await Bun.write(path.join(commonDir, "objects/bb/second"), "sibling object");
			await Bun.write(path.join(commonDir, "refs/heads/sibling"), "2222222222222222222222222222222222222222\n");
			await Bun.write(path.join(commonDir, "refs/heads/brand-new"), "3333333333333333333333333333333333333333\n");
			await Bun.write(
				path.join(commonDir, "packed-refs"),
				"4444444444444444444444444444444444444444 refs/heads/main\n",
			);
			await Bun.write(
				path.join(commonDir, "info/refs"),
				"4444444444444444444444444444444444444444\trefs/heads/main\n",
			);

			const snapChurn = await snapshotGit(gitDir, commonDir);
			expect(snapChurn).toEqual(snapBase);

			// This worktree's own branch ref still fails the check when it moves.
			await Bun.sleep(20);
			await Bun.write(path.join(commonDir, "refs/heads/OMP-343"), "5555555555555555555555555555555555555555\n");
			const snapBranch = await snapshotGit(gitDir, commonDir);
			expect(snapBranch).not.toEqual(snapChurn);

			// And so does this worktree's own HEAD.
			await Bun.sleep(20);
			await Bun.write(path.join(gitDir, "HEAD"), "ref: refs/heads/other\n");
			const snapHead = await snapshotGit(gitDir, commonDir);
			expect(snapHead).not.toEqual(snapBranch);
		} finally {
			await fs.rm(root, { recursive: true, force: true });
		}
	});
});
