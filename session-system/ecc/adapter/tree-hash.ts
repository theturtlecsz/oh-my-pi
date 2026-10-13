/**
 * Git tree id of a directory on disk, so a vendored tree can be verified against
 * the upstream `tree` pin in the manifest without invoking git. Matches
 * `git write-tree --prefix=<dir>` for the same files: regular files hash as
 * 100644, or 100755 when any exec bit is set; empty directories are omitted;
 * symlinks and other special files are refused.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";

const EXEC_BITS = 0o111;

function gitObjectId(type: string, body: Uint8Array): Buffer {
	const hasher = new Bun.CryptoHasher("sha1");
	hasher.update(`${type} ${body.byteLength}\0`);
	hasher.update(body);
	return Buffer.from(hasher.digest());
}

/** Returns the tree object id for `dir`, or null when it holds no files. */
async function treeId(dir: string): Promise<Buffer | null> {
	const entries: { sortKey: string; record: Buffer }[] = [];
	for (const dirent of await fs.readdir(dir, { withFileTypes: true })) {
		const full = path.join(dir, dirent.name);
		if (dirent.isSymbolicLink()) throw new Error(`symlink not allowed in hashed tree: ${full}`);
		let mode: string;
		let id: Buffer | null;
		if (dirent.isDirectory()) {
			id = await treeId(full);
			mode = "40000";
		} else if (dirent.isFile()) {
			const stat = await fs.stat(full);
			id = gitObjectId("blob", await fs.readFile(full));
			mode = stat.mode & EXEC_BITS ? "100755" : "100644";
		} else {
			throw new Error(`unsupported file type in hashed tree: ${full}`);
		}
		if (!id) continue;
		// git orders tree entries by raw bytes, with directories compared as `name/`.
		entries.push({
			sortKey: dirent.isDirectory() ? `${dirent.name}/` : dirent.name,
			record: Buffer.concat([Buffer.from(`${mode} ${dirent.name}\0`), id]),
		});
	}
	if (entries.length === 0) return null;
	entries.sort((a, b) => Buffer.compare(Buffer.from(a.sortKey), Buffer.from(b.sortKey)));
	return gitObjectId("tree", Buffer.concat(entries.map(e => e.record)));
}

export async function gitTreeId(dir: string): Promise<string> {
	const id = await treeId(dir);
	if (!id) throw new Error(`no files under ${dir}`);
	return id.toString("hex");
}
