#!/usr/bin/env bun
// approval-provenance.ts — owner-approval provenance check (OMP-295).
//
// The Work contract approval (`python/omp-work/src/omp_work/contracts/v1/
// approval.json`) is the owner's hash attestation of the v1 contract digest.
// Agents and automation must never mint or rewrite it: only commits authored by
// `flood-owner` whose subject carries an owner marker (`owner step by flood` or
// `flood rebase_repair`) may touch the file. This check walks `<base>..<head>`,
// skips those approval commits, and reports every other commit that introduces
// or changes approval.json — directly for ordinary commits, or, for merge
// commits, only when the merged blob differs from every parent (a conflict
// resolution that rewrote the attestation).
//
//   bun scripts/approval-provenance.ts --base <rev> [--head <rev>]
//
// Prints `<sha12> <author> <subject>` per violating commit and exits 1; exits 0
// with no violation and 2 for an unknown revision or bad usage.

import { $ } from "bun";

export const APPROVAL_PATH = "python/omp-work/src/omp_work/contracts/v1/approval.json";
export const APPROVAL_AUTHOR = "flood-owner";
export const APPROVAL_SUBJECT_MARKERS = ["owner step by flood", "flood rebase_repair"] as const;

export interface ApprovalViolation {
	sha: string;
	author: string;
	subject: string;
}

export interface ApprovalProvenanceOptions {
	cwd: string;
	base: string;
	head?: string;
}

/** A requested revision did not resolve to a commit (CLI exit 2). */
export class UnknownRevisionError extends Error {}

interface CommitInfo {
	sha: string;
	parents: string[];
	author: string;
	subject: string;
}

async function git(cwd: string, args: string[]): Promise<{ exitCode: number; out: string }> {
	const proc = await $`git ${args}`.cwd(cwd).quiet().nothrow();
	return { exitCode: proc.exitCode, out: proc.text() };
}

async function resolveCommit(cwd: string, rev: string): Promise<string> {
	const { exitCode, out } = await git(cwd, ["rev-parse", "--verify", "--quiet", `${rev}^{commit}`]);
	const sha = out.trim();
	if (exitCode !== 0 || !sha) throw new UnknownRevisionError(`unknown revision: ${rev}`);
	return sha;
}

/** Blob of `<rev>:<path>`, or "" when the path is absent in that revision. */
async function blobAt(cwd: string, rev: string, path: string): Promise<string> {
	const { exitCode, out } = await git(cwd, ["rev-parse", "--verify", "--quiet", `${rev}:${path}`]);
	return exitCode === 0 ? out.trim() : "";
}

export function isApprovalCommit(author: string, subject: string): boolean {
	return author === APPROVAL_AUTHOR && APPROVAL_SUBJECT_MARKERS.some(marker => subject.includes(marker));
}

function parseLog(text: string): CommitInfo[] {
	const commits: CommitInfo[] = [];
	for (const line of text.split("\n")) {
		if (!line) continue;
		const [sha, parents, author, ...subjectParts] = line.split("\u0000");
		commits.push({ sha, parents: parents ? parents.split(" ") : [], author, subject: subjectParts.join("\u0000") });
	}
	return commits;
}

/** Paths changed by a single commit versus its first parent (or the empty tree at a root). */
async function changedFiles(cwd: string, rev: string): Promise<string[]> {
	const { out } = await git(cwd, [
		"diff-tree",
		"-r",
		"--no-renames",
		"--root",
		"--name-only",
		"-z",
		"--no-commit-id",
		rev,
	]);
	return out.split("\u0000").filter(Boolean);
}

/**
 * Report every non-owner-marker commit in `<base>..<head>` that introduces or
 * changes approval.json. Throws `UnknownRevisionError` when a revision does not
 * resolve to a commit.
 */
export async function checkApprovalProvenance(options: ApprovalProvenanceOptions): Promise<ApprovalViolation[]> {
	const { cwd } = options;
	const head = options.head ?? "HEAD";
	const baseSha = await resolveCommit(cwd, options.base);
	const headSha = await resolveCommit(cwd, head);

	const { exitCode, out } = await git(cwd, ["log", "--format=%H%x00%P%x00%an%x00%s", `${baseSha}..${headSha}`]);
	if (exitCode !== 0) throw new Error(`git log ${baseSha}..${headSha} failed`);

	// Approval commits are the sanctioned owner attestations; they are never violations.
	const commits = parseLog(out).filter(commit => !isApprovalCommit(commit.author, commit.subject));

	const violations: ApprovalViolation[] = [];
	for (const commit of commits) {
		if (commit.parents.length >= 2) {
			// A merge is a violation only when it rewrote the attestation: its blob
			// matches no parent, so the conflict resolution did not keep either side.
			const blob = await blobAt(cwd, commit.sha, APPROVAL_PATH);
			let matchesParent = false;
			for (const parent of commit.parents) {
				if ((await blobAt(cwd, parent, APPROVAL_PATH)) === blob) {
					matchesParent = true;
					break;
				}
			}
			if (!matchesParent) {
				violations.push({ sha: commit.sha, author: commit.author, subject: commit.subject });
			}
			continue;
		}
		if ((await changedFiles(cwd, commit.sha)).includes(APPROVAL_PATH)) {
			violations.push({ sha: commit.sha, author: commit.author, subject: commit.subject });
		}
	}
	return violations;
}

async function main(): Promise<void> {
	let base: string | undefined;
	let head = "HEAD";
	const argv = process.argv.slice(2);
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--base") base = argv[++i];
		else if (arg === "--head") head = argv[++i] ?? "HEAD";
		else {
			console.error(`usage error: unexpected argument ${arg}`);
			process.exit(2);
		}
	}
	if (!base) {
		console.error("usage error: --base <rev> is required");
		process.exit(2);
	}

	let violations: ApprovalViolation[];
	try {
		violations = await checkApprovalProvenance({ cwd: process.cwd(), base, head });
	} catch (err) {
		if (err instanceof UnknownRevisionError) {
			console.error(`ERROR: ${err.message}`);
			process.exit(2);
		}
		throw err;
	}

	for (const violation of violations) {
		console.log(`${violation.sha.slice(0, 12)} ${violation.author} ${violation.subject}`);
	}
	if (violations.length) process.exit(1);
}

if (import.meta.main) {
	await main();
}
