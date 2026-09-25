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

/**
 * Snapshot git directory metadata:
 * Walks gitDir and commonDir with Bun.Glob("**\/*") (dot: true), skipping objects/**,
 * recording sorted { path, size, mtimeMs }, counting objects/** entries separately,
 * and hashing surface-inventory.json and baseline.json.
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
});
