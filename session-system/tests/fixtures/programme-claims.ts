import * as fs from "node:fs";
import * as path from "node:path";

export const REPO_ROOT = path.resolve(import.meta.dir, "../../..");

export const SOURCE_DELIVERED_LABEL = "source delivered, not installed-qualified";

/**
 * Read a repository file relative to REPO_ROOT as UTF-8 text.
 */
export async function readRepoText(rel: string): Promise<string> {
	return Bun.file(path.resolve(REPO_ROOT, rel)).text();
}

/**
 * Return relative markdown link targets from `text` that do not exist on disk.
 *
 * Rules:
 * - Scans markdown inline links `[label](target)` and image links `![label](target)`.
 * - Skips external http:// and https:// URLs.
 * - Skips /-absolute paths (e.g., `/etc/hosts`).
 * - Skips #-only links (e.g., `#heading`).
 * - Strips fragment identifiers (`#frag`) and line numbers (`:line` or `:line:col`)
 *   before checking target existence on disk relative to `docRel`'s directory.
 */
export function unresolvedLinks(docRel: string, text: string): string[] {
	const docDir = path.dirname(path.resolve(REPO_ROOT, docRel));
	const missing: string[] = [];
	const seen = new Set<string>();

	const linkPattern = /!?\[(?:[^\]]*)\]\(([^)]+)\)/g;

	for (const match of text.matchAll(linkPattern)) {
		const raw = match[1].trim();
		if (!raw) continue;

		let target: string;
		if (raw.startsWith("<")) {
			const closeIndex = raw.indexOf(">");
			target = closeIndex !== -1 ? raw.slice(1, closeIndex).trim() : raw;
		} else {
			target = raw.split(/\s+/)[0];
		}

		if (!target) continue;

		// Skip external URLs
		if (/^https?:\/\//i.test(target)) continue;

		// Skip absolute paths
		if (target.startsWith("/")) continue;

		// Skip internal anchors
		if (target.startsWith("#")) continue;

		// Strip fragment identifiers
		let targetPath = target.split("#")[0];

		// Strip line and column numbers (:line or :line:col)
		targetPath = targetPath.replace(/:\d+(?::\d+)*$/, "");

		if (!targetPath) continue;

		const resolvedPath = path.resolve(docDir, targetPath);

		if (!fs.existsSync(resolvedPath)) {
			try {
				const decoded = decodeURI(targetPath);
				const decodedResolved = path.resolve(docDir, decoded);
				if (fs.existsSync(decodedResolved)) {
					continue;
				}
			} catch {
				// Treat decode failure as unresolved
			}

			if (!seen.has(target)) {
				seen.add(target);
				missing.push(target);
			}
		}
	}

	return missing;
}
