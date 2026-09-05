#!/usr/bin/env bun
// upstream-discovery.ts — weekly upstream release discovery (OMP-229).
//
// Identifies the newest stable (published, non-draft, non-prerelease, final
// x.y.z semver) upstream release above the accepted baseline version and
// resolves its tag to one immutable commit via `git ls-remote` with
// annotated-tag peeling. Never uses a moving branch reference.
//
//   bun scripts/upstream-discovery.ts [--baseline docs/upstream/baseline.json] [--json]
//
// Exit 0 with "up to date" when no newer stable release exists.

import { compareVersions } from "./verify-upstream-handoff.ts";

export interface UpstreamRelease {
	tag_name: string;
	draft: boolean;
	prerelease: boolean;
}

export interface Candidate {
	tag: string;
	version: string;
}

const FINAL_TAG = /^v(\d+\.\d+\.\d+)$/;

/** Pick the newest stable final-semver release strictly above the baseline version. */
export function pickNewestStable(releases: UpstreamRelease[], baselineVersion: string): Candidate | null {
	let best: Candidate | null = null;
	for (const release of releases) {
		if (release.draft || release.prerelease) continue;
		const m = release.tag_name.match(FINAL_TAG);
		if (!m) continue;
		const version = m[1];
		if (compareVersions(version, baselineVersion) <= 0) continue;
		if (!best || compareVersions(version, best.version) > 0) best = { tag: release.tag_name, version };
	}
	return best;
}

/**
 * Resolve a tag to its immutable commit from `git ls-remote --tags` output,
 * preferring the peeled (`^{}`) line so annotated tags resolve to commits.
 */
export function resolveTagCommit(lsRemoteText: string, tag: string): string | null {
	let plain: string | null = null;
	let peeled: string | null = null;
	for (const line of lsRemoteText.split("\n")) {
		const m = line.match(/^([0-9a-f]{40})\trefs\/tags\/(.+?)(\^\{\})?$/);
		if (!m || m[2] !== tag) continue;
		if (m[3]) peeled = m[1];
		else plain = m[1];
	}
	return peeled ?? plain;
}

/** Parse `https://github.com/<owner>/<repo>` into API coordinates. */
export function parseGithubRepo(url: string): { owner: string; repo: string } {
	const m = url.match(/^https:\/\/github\.com\/([^/]+)\/([^/]+?)(?:\.git)?\/?$/);
	if (!m) throw new Error(`unsupported upstream_repo URL: ${url}`);
	return { owner: m[1], repo: m[2] };
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
	let baselinePath = "docs/upstream/baseline.json";
	let json = false;
	const argv = process.argv.slice(2);
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--json") json = true;
		else if (arg === "--baseline") baselinePath = argv[++i] ?? "";
		else {
			console.error(`usage error: unexpected argument ${arg}`);
			process.exit(2);
		}
	}

	const baselineFile = Bun.file(baselinePath);
	if (!(await baselineFile.exists())) {
		console.error(`ERROR: baseline record missing: ${baselinePath}`);
		process.exit(1);
	}
	const baseline = JSON.parse(await baselineFile.text()) as { upstream_repo?: unknown; upstream_version?: unknown };
	if (typeof baseline.upstream_repo !== "string" || typeof baseline.upstream_version !== "string") {
		console.error(`ERROR: ${baselinePath} needs upstream_repo and upstream_version`);
		process.exit(1);
	}
	const { owner, repo } = parseGithubRepo(baseline.upstream_repo);

	const headers: Record<string, string> = { accept: "application/vnd.github+json" };
	const token = process.env.GITHUB_TOKEN ?? process.env.GH_TOKEN;
	if (token) headers.authorization = `Bearer ${token}`;
	const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/releases?per_page=100`, { headers });
	if (!response.ok) {
		console.error(`ERROR: release listing failed: HTTP ${response.status} ${await response.text()}`);
		process.exit(1);
	}
	const releases = (await response.json()) as UpstreamRelease[];
	const candidate = pickNewestStable(releases, baseline.upstream_version);

	if (!candidate) {
		if (json) {
			console.log(JSON.stringify({ baseline_version: baseline.upstream_version, newer: false }));
		} else {
			console.log(`up to date: no stable upstream release above ${baseline.upstream_version}`);
		}
		return;
	}

	const proc = Bun.spawn(
		[
			"git",
			"ls-remote",
			"--tags",
			baseline.upstream_repo,
			`refs/tags/${candidate.tag}`,
			`refs/tags/${candidate.tag}^{}`,
		],
		{ stdout: "pipe", stderr: "pipe" },
	);
	const [exitCode, lsRemoteText, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	if (exitCode !== 0) {
		console.error(`ERROR: git ls-remote failed: ${stderr.trim()}`);
		process.exit(1);
	}
	const commit = resolveTagCommit(lsRemoteText, candidate.tag);
	if (!commit) {
		console.error(`ERROR: tag ${candidate.tag} not found on ${baseline.upstream_repo}`);
		process.exit(1);
	}

	if (json) {
		console.log(
			JSON.stringify({
				baseline_version: baseline.upstream_version,
				newer: true,
				candidate_version: candidate.version,
				candidate_tag: candidate.tag,
				candidate_commit: commit,
			}),
		);
	} else {
		console.log(`candidate: ${candidate.version} (${candidate.tag}) -> ${commit}`);
	}
}

if (import.meta.main) {
	await main();
}
