#!/usr/bin/env bun
// upstream-discovery.ts — weekly upstream release discovery (OMP-229).
//
// Identifies the newest stable (published, non-draft, non-prerelease, final
// x.y.z semver) upstream release above the accepted baseline version and
// resolves its tag to one immutable commit via `git ls-remote` with
// annotated-tag peeling. Never uses a moving branch reference.
//
//   bun scripts/upstream-discovery.ts [--baseline docs/upstream-baseline.json] [--releases <path>] [--ls-remote <path>] [--json]
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

export interface DiscoveryOptions {
	baselinePath?: string;
	releasesPath?: string;
	lsRemotePath?: string;
}

export interface DiscoveryResult {
	baseline_version: string;
	newer: boolean;
	candidate_version?: string;
	candidate_tag?: string;
	candidate_commit?: string;
}

export async function discoverUpstream(options: DiscoveryOptions = {}): Promise<DiscoveryResult> {
	const baselinePath = options.baselinePath || "docs/upstream-baseline.json";
	let baselineText: string;
	try {
		baselineText = await Bun.file(baselinePath).text();
	} catch {
		throw new Error(`baseline record missing: ${baselinePath}`);
	}

	let baseline: { upstream_repo?: unknown; upstream_version?: unknown };
	try {
		baseline = JSON.parse(baselineText) as { upstream_repo?: unknown; upstream_version?: unknown };
	} catch {
		throw new Error(`baseline record invalid JSON: ${baselinePath}`);
	}
	if (typeof baseline.upstream_repo !== "string" || typeof baseline.upstream_version !== "string") {
		throw new Error(`${baselinePath} needs upstream_repo and upstream_version`);
	}
	const { owner, repo } = parseGithubRepo(baseline.upstream_repo);

	let releases: UpstreamRelease[];
	if (options.releasesPath) {
		try {
			releases = (await Bun.file(options.releasesPath).json()) as UpstreamRelease[];
		} catch {
			throw new Error(`releases fixture missing: ${options.releasesPath}`);
		}
	} else {
		const headers: Record<string, string> = { accept: "application/vnd.github+json" };
		const token = process.env.GITHUB_TOKEN ?? process.env.GH_TOKEN;
		if (token) headers.authorization = `Bearer ${token}`;
		const response = await fetch(`https://api.github.com/repos/${owner}/${repo}/releases?per_page=100`, { headers });
		if (!response.ok) {
			throw new Error(`release listing failed: HTTP ${response.status} ${await response.text()}`);
		}
		releases = (await response.json()) as UpstreamRelease[];
	}

	const candidate = pickNewestStable(releases, baseline.upstream_version);
	if (!candidate) {
		return {
			baseline_version: baseline.upstream_version,
			newer: false,
		};
	}

	let lsRemoteText: string;
	if (options.lsRemotePath) {
		try {
			lsRemoteText = await Bun.file(options.lsRemotePath).text();
		} catch {
			throw new Error(`ls-remote fixture missing: ${options.lsRemotePath}`);
		}
	} else {
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
		const [exitCode, lsRemoteOutput, stderr] = await Promise.all([
			proc.exited,
			new Response(proc.stdout).text(),
			new Response(proc.stderr).text(),
		]);
		if (exitCode !== 0) {
			throw new Error(`git ls-remote failed: ${stderr.trim()}`);
		}
		lsRemoteText = lsRemoteOutput;
	}

	const commit = resolveTagCommit(lsRemoteText, candidate.tag);
	if (!commit) {
		throw new Error(`tag ${candidate.tag} not found on ${baseline.upstream_repo}`);
	}

	return {
		baseline_version: baseline.upstream_version,
		newer: true,
		candidate_version: candidate.version,
		candidate_tag: candidate.tag,
		candidate_commit: commit,
	};
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
	const argv = process.argv.slice(2);
	let baselinePath = "docs/upstream-baseline.json";
	let json = false;
	let releasesPath = "";
	let lsRemotePath = "";

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--json") json = true;
		else if (arg === "--baseline") baselinePath = argv[++i] ?? "";
		else if (arg === "--releases") releasesPath = argv[++i] ?? "";
		else if (arg === "--ls-remote") lsRemotePath = argv[++i] ?? "";
		else {
			console.error(`usage error: unexpected argument ${arg}`);
			process.exit(2);
		}
	}

	try {
		const result = await discoverUpstream({
			baselinePath,
			releasesPath: releasesPath || undefined,
			lsRemotePath: lsRemotePath || undefined,
		});

		if (json) {
			console.log(JSON.stringify(result));
		} else if (result.newer) {
			console.log(`candidate: ${result.candidate_version} (${result.candidate_tag}) -> ${result.candidate_commit}`);
		} else {
			console.log(`up to date: no stable upstream release above ${result.baseline_version}`);
		}
	} catch (err) {
		console.error(`ERROR: ${err instanceof Error ? err.message : String(err)}`);
		process.exit(1);
	}
}

if (import.meta.main) {
	await main();
}
