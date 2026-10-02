#!/usr/bin/env bun
// check-deploy-copy.ts — refuse-or-report guard for flood's deploy copies (OMP-534).
//
// flood's deploy-omp.sh runs after every GitHub merge and moves two worktrees to
// origin/main: $DEPLOY (the copy the WorkService and the owner's omp run from)
// and $OWNER (~/oh-my-pi). A tracked local edit in either copy survives
// `git checkout --detach <target>`, so the copy keeps a stale file after a
// "successful" deploy and stops matching main. The old script only compared
// HEAD, and only inspected the owner copy — an edit in $DEPLOY at the target
// revision was never even looked at. An edited copy then silently stopped
// moving (OMP-534: the 2026-10-02 relevance review).
//
// This guard reports the truth for each copy: whether HEAD equals the target,
// and every tracked local edit. Generated `*/uv.lock` edits are reported
// separately because the deploy discards them by design. It exits 1 when any
// copy is stale (HEAD != target) or has a non-generated edit, so the caller
// refuses instead of staying silently old.
//
//   bun scripts/check-deploy-copy.ts [--target <rev>] <worktree> [<worktree>...]
//
// Prints `<path> <verdict>: <detail>` per copy; exits 1 for a stale or dirty
// copy and 2 for an unknown revision or bad usage.

import { $ } from "bun";

export const DEFAULT_TARGET = "origin/main";

export interface DeployCopyStatus {
	path: string;
	head: string;
	target: string;
	atTarget: boolean;
	/** Porcelain lines for tracked edits that survive no deploy step. */
	edits: string[];
	/** Porcelain lines for generated lock edits the deploy discards. */
	generatedEdits: string[];
}

export type DeployCopyVerdict = "ok" | "stale" | "dirty" | "stale+dirty";

export interface DeployCopyGuardOptions {
	copies: string[];
	target?: string;
}

/** A requested revision or worktree did not resolve (CLI exit 2). */
export class UnknownRevisionError extends Error {}

interface GitResult {
	exitCode: number;
	out: string;
}

async function git(cwd: string, args: string[]): Promise<GitResult> {
	const proc = await $`git ${args}`.cwd(cwd).quiet().nothrow();
	return { exitCode: proc.exitCode, out: proc.text() };
}

/** Split a porcelain status list into generated lock edits and everything else. */
export function splitEdits(porcelain: string): { edits: string[]; generatedEdits: string[] } {
	const edits: string[] = [];
	const generatedEdits: string[] = [];
	for (const line of porcelain.split("\n")) {
		if (line.length < 4) continue;
		const path = line.slice(3);
		if (/(^|\/)uv\.lock$/.test(path)) generatedEdits.push(line);
		else edits.push(line);
	}
	return { edits, generatedEdits };
}

/**
 * Report the deploy state of every copy. Throws `UnknownRevisionError` when a
 * copy is not a git worktree or the target does not resolve inside it.
 */
export async function checkDeployCopy(options: DeployCopyGuardOptions): Promise<DeployCopyStatus[]> {
	const target = options.target ?? DEFAULT_TARGET;
	const statuses: DeployCopyStatus[] = [];
	for (const copy of options.copies) {
		const headResult = await git(copy, ["rev-parse", "HEAD"]);
		const head = headResult.out.trim();
		if (headResult.exitCode !== 0 || !head) throw new UnknownRevisionError(`${copy}: not a git worktree`);

		const targetResult = await git(copy, ["rev-parse", "--verify", "--quiet", `${target}^{commit}`]);
		const targetSha = targetResult.out.trim();
		if (targetResult.exitCode !== 0 || !targetSha) {
			throw new UnknownRevisionError(`${copy}: unknown target ${target}`);
		}

		const statusResult = await git(copy, ["status", "--porcelain", "--untracked-files=no"]);
		if (statusResult.exitCode !== 0) throw new UnknownRevisionError(`${copy}: git status failed`);

		const { edits, generatedEdits } = splitEdits(statusResult.out);
		statuses.push({ path: copy, head, target: targetSha, atTarget: head === targetSha, edits, generatedEdits });
	}
	return statuses;
}

export function classifyCopy(status: DeployCopyStatus): DeployCopyVerdict {
	const dirty = status.edits.length > 0;
	if (dirty && !status.atTarget) return "stale+dirty";
	if (dirty) return "dirty";
	if (!status.atTarget) return "stale";
	return "ok";
}

export function formatStatus(status: DeployCopyStatus): string {
	const parts = [`${classifyCopy(status)}:`, `HEAD ${status.head.slice(0, 12)}`];
	if (!status.atTarget) parts.push(`target ${status.target.slice(0, 12)}`);
	if (status.edits.length > 0) {
		parts.push(`${status.edits.length} local edit(s): ${status.edits.map(line => line.slice(3)).join(", ")}`);
	}
	if (status.generatedEdits.length > 0) {
		parts.push(
			`${status.generatedEdits.length} discardable generated edit(s): ${status.generatedEdits.map(line => line.slice(3)).join(", ")}`,
		);
	}
	return `${status.path} ${parts.join(" ")}`;
}

async function main(): Promise<void> {
	let target = DEFAULT_TARGET;
	const copies: string[] = [];
	const argv = process.argv.slice(2);
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--target") {
			target = argv[++i] ?? "";
			if (!target) {
				console.error("usage error: --target needs a value");
				process.exit(2);
			}
		} else if (arg.startsWith("-")) {
			console.error(`usage error: unexpected argument ${arg}`);
			process.exit(2);
		} else {
			copies.push(arg);
		}
	}
	if (copies.length === 0) {
		console.error("usage error: at least one worktree path is required");
		process.exit(2);
	}

	let statuses: DeployCopyStatus[];
	try {
		statuses = await checkDeployCopy({ copies, target });
	} catch (err) {
		if (err instanceof UnknownRevisionError) {
			console.error(`ERROR: ${err.message}`);
			process.exit(2);
		}
		throw err;
	}

	for (const status of statuses) console.log(`deploy-copy-guard: ${formatStatus(status)}`);
	if (statuses.some(status => classifyCopy(status) !== "ok")) process.exit(1);
}

if (import.meta.main) {
	await main();
}
