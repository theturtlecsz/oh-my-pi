import * as crypto from "node:crypto";
import * as fs from "node:fs/promises";
import * as path from "node:path";

function isEnoent(error: unknown): boolean {
	return (
		typeof error === "object" &&
		error !== null &&
		"code" in error &&
		(error as { code: unknown }).code === "ENOENT"
	);
}

async function walk(sourceDir: string, targetDir: string): Promise<void> {
	const entries = await fs.readdir(sourceDir, { withFileTypes: true });
	for (const entry of entries) {
		const srcPath = path.join(sourceDir, entry.name);
		const dstPath = path.join(targetDir, entry.name);

		if (entry.isDirectory()) {
			await fs.mkdir(dstPath);
			await walk(srcPath, dstPath);
		} else if (entry.isSymbolicLink()) {
			const linkText = await fs.readlink(srcPath);
			await fs.symlink(linkText, dstPath);
		} else {
			try {
				await fs.link(srcPath, dstPath);
			} catch (error) {
				const code = (error as NodeJS.ErrnoException).code;
				if (code === "EXDEV" || code === "EPERM" || code === "EMLINK") {
					await fs.copyFile(srcPath, dstPath);
				} else {
					throw error;
				}
			}
		}
	}
}

/**
 * Clones an installed dependency tree (e.g. node_modules) into a target path
 * using hardlinks for files, identical verbatim link targets for symlinks,
 * and recursive directories, without network or blocking the event loop.
 */
export async function cloneDependencyTree(source: string, target: string): Promise<void> {
	const cleanTarget = target.length > 1 ? target.replace(/[/\\]+$/, "") : target;
	let targetExists = false;
	try {
		await fs.lstat(cleanTarget);
		targetExists = true;
	} catch (error) {
		if (!isEnoent(error)) {
			throw error;
		}
	}

	if (targetExists) {
		throw new Error(`Target already exists: ${cleanTarget}`);
	}

	const cleanSource = source.length > 1 ? source.replace(/[/\\]+$/, "") : source;
	const partialTarget = `${cleanTarget}.partial-${crypto.randomUUID()}`;

	try {
		await fs.mkdir(partialTarget, { recursive: true });
		await walk(cleanSource, partialTarget);
		await fs.rename(partialTarget, cleanTarget);
	} catch (error) {
		await fs.rm(partialTarget, { recursive: true, force: true }).catch(() => {});
		throw error;
	}
}
