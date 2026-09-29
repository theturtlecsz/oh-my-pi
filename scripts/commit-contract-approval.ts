#!/usr/bin/env bun
// commit-contract-approval.ts — commit an owner contract approval (OMP-452).
//
// The owner runs `omp-work approve --issue <issue>`, which writes
// `python/omp-work/src/omp_work/contracts/v1/approval.json`; the same change
// must then be committed in a form `scripts/approval-provenance.ts` accepts or
// CI's `check` job fails the pull request. A bare `git commit` from the owner's
// session authors the commit as the session user with an ordinary subject, which
// the provenance check rejects (this is exactly how OMP-405's owner-session
// commit broke a batch). This helper stages the approval artifacts and writes
// the sanctioned commit instead: author and committer are `flood-owner`, so the
// commit does not depend on the session's git identity, and the subject carries
// the `owner step by owner session` marker.
//
// It never mints or rewrites the approval bytes — `omp-work approve` owns that
// file. The helper only stages what is already on disk and commits it.
//
//   bun scripts/commit-contract-approval.ts --issue OMP-405 [--digest <sha256>]
//
// Prints the new commit SHA and exits 0; exits 2 for bad usage and 1 when the
// approval file is absent, a requested digest does not match it, or git fails.

import * as path from "node:path";
import { fileURLToPath } from "node:url";
import { APPROVAL_AUTHOR, APPROVAL_AUTHOR_EMAIL, APPROVAL_PATH } from "./approval-provenance.ts";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CONTRACT_PATH = "packages/work-client/src/contract.ts";

export interface ContractApprovalCommitOptions {
	/** Absolute repository root the commit is created in. */
	cwd: string;
	/** Work key the approval names, e.g. `OMP-405`. */
	issue: string;
	/** Expected `contract_sha256`, verified against the staged approval file. */
	digest?: string;
}

/** Paths the approval commit stages; other working-tree changes stay untouched. */
export const APPROVAL_COMMIT_PATHS = [APPROVAL_PATH, CONTRACT_PATH] as const;

/** Author and committer for the approval commit. Git requires both, and the provenance check reads the author. */
const APPROVAL_GIT_IDENTITY = {
	GIT_AUTHOR_NAME: APPROVAL_AUTHOR,
	GIT_AUTHOR_EMAIL: APPROVAL_AUTHOR_EMAIL,
	GIT_COMMITTER_NAME: APPROVAL_AUTHOR,
	GIT_COMMITTER_EMAIL: APPROVAL_AUTHOR_EMAIL,
} as const;

export class ContractApprovalError extends Error {}

async function git(cwd: string, args: string[], env?: Record<string, string>): Promise<string> {
	const proc = Bun.spawn(["git", ...args], {
		cwd,
		stdout: "pipe",
		stderr: "pipe",
		env: env ? { ...process.env, ...env } : process.env,
	});
	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	if (exitCode !== 0) throw new ContractApprovalError(`git ${args.join(" ")} failed: ${stderr.trim()}`);
	return stdout;
}

/** Commit the owner's on-disk approval with the provenance-compliant identity. */
export async function commitContractApproval(options: ContractApprovalCommitOptions): Promise<string> {
	const { cwd, issue, digest } = options;
	if (!/^[A-Za-z][A-Za-z0-9]*-\d+$/.test(issue)) {
		throw new ContractApprovalError(`issue is not a work key: ${issue}`);
	}

	const approvalPath = path.join(cwd, APPROVAL_PATH);
	if (!(await Bun.file(approvalPath).exists())) {
		throw new ContractApprovalError(`approval file is missing: ${APPROVAL_PATH} — run \`omp-work approve\` first`);
	}
	if (digest) {
		const raw = (await Bun.file(approvalPath).json()) as { contract_sha256?: unknown };
		if (raw.contract_sha256 !== digest) {
			throw new ContractApprovalError(
				`approval file contract_sha256 ${String(raw.contract_sha256)} does not match ${digest}`,
			);
		}
	}

	await git(cwd, ["add", "--", ...APPROVAL_COMMIT_PATHS]);
	const staged = await git(cwd, ["diff", "--cached", "--name-only"]);
	if (!staged.split("\n").includes(APPROVAL_PATH)) {
		throw new ContractApprovalError(`staging produced no ${APPROVAL_PATH} change`);
	}

	const subject = digest
		? `chore(contract): owner step by owner session — approve v1 digest ${digest} (${issue})`
		: `chore(contract): owner step by owner session — approve the v1 contract (${issue})`;
	await git(cwd, ["commit", "-m", subject, "--", ...APPROVAL_COMMIT_PATHS], { ...APPROVAL_GIT_IDENTITY });
	return (await git(cwd, ["rev-parse", "HEAD"])).trim();
}

async function main(): Promise<void> {
	let issue: string | undefined;
	let digest: string | undefined;
	const argv = process.argv.slice(2);
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--issue") issue = argv[++i];
		else if (arg === "--digest") digest = argv[++i];
		else {
			console.error(`usage error: unexpected argument ${arg}`);
			process.exit(2);
		}
	}
	if (!issue) {
		console.error("usage error: --issue <work-key> is required");
		process.exit(2);
	}

	try {
		const sha = await commitContractApproval({ cwd: REPO_ROOT, issue, digest });
		console.log(sha);
	} catch (err) {
		console.error(err instanceof ContractApprovalError ? err.message : String(err));
		process.exitCode = 1;
	}
}

if (import.meta.main) {
	await main();
}
