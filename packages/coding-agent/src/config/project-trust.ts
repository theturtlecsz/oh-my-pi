import * as fs from "node:fs";
import * as path from "node:path";

import * as vcs from "@oh-my-pi/pi-natives/vcs";
import {
	getAgentDir,
	isEnoent,
	isRecord,
	logger,
	normalizePathForComparison,
	relativePathWithinNormalizedRoot,
	tryParseJson,
} from "@oh-my-pi/pi-utils";

const TRUSTED_PROJECTS_FILENAME = "trusted-projects.json";

/** Path of the trusted-project list inside an agent config directory. */
export function trustedProjectsPath(agentDir: string = getAgentDir()): string {
	return path.join(agentDir, TRUSTED_PROJECTS_FILENAME);
}

/**
 * Read the trusted-project allowlist (`<agentDir>/trusted-projects.json`,
 * `{"version":1,"paths":[abs dirs]}`). Missing or malformed input yields an
 * empty list rather than throwing: an unusable list must fail closed, since an
 * untrusted workspace is the safe default. Relative and duplicate entries are
 * dropped; every kept entry is normalized (symlinks resolved) for comparison.
 */
export function readTrustedProjectPaths(agentDir: string = getAgentDir()): string[] {
	let content: string;
	try {
		content = fs.readFileSync(trustedProjectsPath(agentDir), "utf-8");
	} catch (err) {
		if (!isEnoent(err)) logger.warn("Failed to read trusted projects", { error: err });
		return [];
	}
	const parsed = tryParseJson(content);
	if (!isRecord(parsed) || !Array.isArray(parsed.paths)) {
		logger.warn("Trusted projects file is malformed");
		return [];
	}
	const seen = new Set<string>();
	const paths: string[] = [];
	for (const entry of parsed.paths) {
		if (typeof entry !== "string" || !path.isAbsolute(entry)) continue;
		const normalized = normalizePathForComparison(entry);
		if (seen.has(normalized)) continue;
		seen.add(normalized);
		paths.push(normalized);
	}
	return paths;
}

function isUnderNormalizedRoot(normalizedRoot: string, normalizedCandidate: string): boolean {
	return relativePathWithinNormalizedRoot(normalizedRoot, normalizedCandidate) !== null;
}

function gitInfoOrNull(dir: string) {
	try {
		return vcs.gitInfo(dir);
	} catch {
		return null;
	}
}

/**
 * Whether `cwd` is trusted: equal to or nested under a listed path, or a linked
 * git worktree whose main checkout (the parent of its shared `commonDir`) is
 * listed. The check realpaths both sides, so a symlinked path or worktree
 * resolves to its canonical location before comparison.
 */
export function isProjectPathTrusted(cwd: string, agentDir: string = getAgentDir()): boolean {
	const trusted = readTrustedProjectPaths(agentDir);
	if (trusted.length === 0) return false;

	const normalizedCwd = normalizePathForComparison(cwd);
	if (trusted.some(root => isUnderNormalizedRoot(root, normalizedCwd))) return true;

	const info = gitInfoOrNull(cwd);
	if (!info) return false;
	const mainCheckout = normalizePathForComparison(path.dirname(info.commonDir));
	return trusted.some(root => isUnderNormalizedRoot(root, mainCheckout));
}
