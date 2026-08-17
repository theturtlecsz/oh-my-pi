/**
 * workflow/git.ts — git internals for the workflow host.
 *
 * commitSessionWork: the Linear backend's one-step /done commit+push (HOME-118).
 * freezeCandidateCommit + pushCandidate: the Work Ledger split (HOME-147) —
 * /summary freezes the owner-approved path set into a local candidate commit,
 * /done pushes the recorded commit and verifies the remote ref.
 *
 * candidateSha256 implements work.omp.dev/v1/candidate-sha256 byte-exactly per
 * contracts/v1/decisions/0004 item 6; golden vectors live in
 * contracts/v1/candidate-hash.json and are pinned by commit-step.test.ts.
 */
import { spawnSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";


/** Run git in cwd; timeoutMs guards network ops (push). `raw` is untrimmed stdout —
 *  porcelain -z parsing needs the leading space of the first `XY path` entry. */
export function runGit(cwd: string, args: string[], timeoutMs = 10_000): { ok: boolean; out: string; raw: string; err: string } {
	const r = spawnSync("git", ["-C", cwd, ...args], { encoding: "utf8", timeout: timeoutMs });
	const raw = r.stdout ?? "";
	return { ok: r.status === 0, out: raw.trim(), raw, err: (r.stderr ?? "").trim() };
}

/** Run git with raw stdout — required where byte fidelity matters (path lists
 *  that must be validated as strict UTF-8 before they bind a candidate). */
function runGitRaw(cwd: string, args: string[], timeoutMs = 10_000): { ok: boolean; stdout: Buffer; err: string } {
	const r = spawnSync("git", ["-C", cwd, ...args], { timeout: timeoutMs });
	return { ok: r.status === 0, stdout: r.stdout ?? Buffer.alloc(0), err: (r.stderr?.toString("utf8") ?? "").trim() };
}

/** Parse `git status --porcelain -z` output → workdir-relative dirty paths.
 *  NUL-separated; entries with X status R or C carry the ORIGINAL path as the
 *  next NUL token — skip it, keep the new path. Untracked (`??`) included. */
export function parsePorcelain(z: string): string[] {
	const tokens = z.split("\0").filter(t => t.length > 0);
	const paths: string[] = [];
	for (let i = 0; i < tokens.length; i++) {
		const entry = tokens[i];
		paths.push(entry.slice(3));
		if (entry[0] === "R" || entry[0] === "C") i++; // next token = original path; not stageable
	}
	return paths;
}

const SECRET_PATTERNS: [string, RegExp][] = [
	["linear-key", /lin_api_[A-Za-z0-9]/],
	["secret-key", /\bsk-[A-Za-z0-9_-]{16,}/],
	["aws-key", /\bAKIA[0-9A-Z]{16}\b/],
	["github-token", /\bgh[pousr]_[A-Za-z0-9]{20,}/],
	["slack-token", /\bxox[baprs]-/],
	["private-key", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
];

/** Scan ADDED lines of a cached diff for credential shapes → matched pattern names ([] = clean). */
export function findSecrets(diff: string): string[] {
	const hits = new Set<string>();
	for (const line of diff.split("\n")) {
		if (!line.startsWith("+") || line.startsWith("+++")) continue;
		for (const [name, re] of SECRET_PATTERNS) if (re.test(line)) hits.add(name);
	}
	return [...hits];
}

/** Canonical candidate hash — decision 0004 item 6. The pinned implementation
 *  (validation + byte-order sort + canonical JSON) lives in the work-client
 *  package, proven byte-identical against contracts/v1/candidate-hash.json. */
export { candidateSha256 } from "@oh-my-pi/pi-work-client";
import { candidateSha256 } from "@oh-my-pi/pi-work-client";

export interface CandidateFreeze {
	root: string;
	paths: string[]; // exact committed file list (diff-tree), unsorted as reported
	commitSha: string;
	candidateSha256: string;
}

export interface FreezeUi {
	confirm(title: string, body: string): Promise<boolean>;
	notify(msg: string, level?: "info" | "warning" | "error"): void;
}

/** The commit's complete file list, exactly as stored. -z gives raw unquoted
 *  bytes; each path is decoded with a fatal UTF-8 decoder — a non-UTF-8 path
 *  name refuses the candidate (decision 0004 item 6). */
function committedPaths(root: string, commitSha: string): string[] {
	const out = runGitRaw(root, ["diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commitSha]);
	if (!out.ok) throw new Error(`git diff-tree failed: ${out.err.split("\n")[0]}`);
	const decoder = new TextDecoder("utf-8", { fatal: true });
	const paths: string[] = [];
	let start = 0;
	for (let i = 0; i <= out.stdout.length; i++) {
		if (i !== out.stdout.length && out.stdout[i] !== 0) continue;
		if (i > start) {
			try {
				paths.push(decoder.decode(out.stdout.subarray(start, i)));
			} catch {
				throw new Error(`candidate refused — non-UTF-8 path name in commit ${commitSha.slice(0, 12)}`);
			}
		}
		start = i + 1;
	}
	return paths;
}

/** /summary freeze (HOME-147): owner confirms the exact proposed path set, only
 *  those paths are staged, added bytes are credential-scanned, one local
 *  candidate commit `session candidate: <key>` is created — carrying a
 *  `Work-Candidate: <planned candidate id>` trailer — and its canonical
 *  candidate hash computed. Returns null (with a notify) on any refusal/empty/
 *  failure — NEVER throws. Pre-existing dirty paths outside the approved set
 *  (and all of packages/, Chris's own lane) stay uncommitted. Retry reuse only
 *  accepts HEAD when the trailer names the CURRENT planned candidate, so a
 *  newer plan attempt never binds a stale commit. */
export async function freezeCandidateCommit(ui: FreezeUi, cwd: string, key: string, candidateId: string): Promise<CandidateFreeze | null> {
	try {
		const top = runGit(cwd, ["rev-parse", "--show-toplevel"]);
		if (!top.ok) {
			ui.notify("no git repo here — cannot freeze a candidate", "warning");
			return null;
		}
		const root = top.out;
		const status = runGit(root, ["status", "--porcelain", "-z"]);
		const all = status.ok ? parsePorcelain(status.raw) : [];
		const excluded = all.filter(p => p.startsWith("packages/")); // Chris's own lane — never staged (HOME-117/118)
		const committable = all.filter(p => !p.startsWith("packages/"));
		const leftAlone = excluded.length ? `left alone: ${excluded.length} file(s) under packages/ (yours)` : "";
		if (committable.length === 0) {
			// Retry after a lost finalize response (HOME-147): the previous
			// /summary may already have created this attempt's candidate commit
			// before the outcome vanished. Reuse that exact commit — never
			// amend, never duplicate (plan §recovery) — but only when the
			// trailer binds it to the CURRENT planned candidate.
			const head = runGit(root, ["log", "-1", "--format=%H%x00%B"]);
			const sep = head.ok ? head.out.indexOf("\x00") : -1;
			const headSha = sep > 0 ? head.out.slice(0, sep) : "";
			const headLines = sep > 0 ? head.out.slice(sep + 1).split("\n") : [];
			if (
				headSha &&
				headLines[0] === `session candidate: ${key}` &&
				headLines.includes(`Work-Candidate: ${candidateId}`)
			) {
				const paths = committedPaths(root, headSha);
				ui.notify(`reusing the existing candidate commit ${headSha.slice(0, 12)} for ${key}`, "info");
				return { root, paths, commitSha: headSha, candidateSha256: candidateSha256(headSha, paths) };
			}
			ui.notify(`nothing to freeze${leftAlone ? ` — ${leftAlone}` : ""}`, "info");
			return null;
		}
		const listed = committable.slice(0, 20).join("\n") + (committable.length > 20 ? `\n+${committable.length - 20} more` : "");
		const body = leftAlone ? `${listed}\n\n${leftAlone}` : listed;
		const yes = await ui.confirm(`Freeze the candidate? (${committable.length} files → session candidate: ${key})`, body);
		if (!yes) return null;
		const add = runGit(root, ["add", "--", ...committable]); // exact paths only — never `git add .`
		if (!add.ok) {
			ui.notify(`freeze failed at staging: ${add.err.split("\n")[0]}`, "error");
			return null;
		}
		const secrets = findSecrets(runGit(root, ["diff", "--cached"]).out);
		if (secrets.length) {
			runGit(root, ["reset", "-q", "--", ...committable]);
			ui.notify(`freeze refused — possible secret in staged diff: ${secrets.join(", ")}. Commit manually if false alarm.`, "error");
			return null;
		}
		const commit = runGit(root, ["commit", "-m", `session candidate: ${key}\n\nWork-Candidate: ${candidateId}`]);
		if (!commit.ok) {
			runGit(root, ["reset", "-q", "--", ...committable]);
			ui.notify(`freeze commit failed: ${commit.err.split("\n")[0] || commit.out.split("\n")[0]}`, "error");
			return null;
		}
		const commitSha = runGit(root, ["rev-parse", "HEAD"]).out;
		const paths = committedPaths(root, commitSha);
		return { root, paths, commitSha, candidateSha256: candidateSha256(commitSha, paths) };
	} catch (e) {
		ui.notify(`candidate freeze failed: ${String(e)}`, "error");
		return null;
	}
}

export interface PushOutcome {
	status: "pushed" | "remote_commit" | "not_pushed";
	remoteUrl?: string;
	remoteRef?: string;
	remoteCommit?: string;
	detail?: string;
}

/** /done push (HOME-147): push, then verify the remote branch ref resolves to the
 *  exact candidate commit. push failed BUT the ref matches → "remote_commit"
 *  (output lost, commit landed) — completion may proceed; otherwise "not_pushed"
 *  and the closeout intent stays pending. NEVER throws. */
export function pushCandidate(root: string, commitSha: string): PushOutcome {
	try {
		const branch = runGit(root, ["rev-parse", "--abbrev-ref", "HEAD"]);
		if (!branch.ok || !branch.out || branch.out === "HEAD") {
			return { status: "not_pushed", detail: "detached HEAD — no branch to push" };
		}
		const remote = runGit(root, ["remote", "get-url", "origin"]);
		if (!remote.ok || !remote.out) return { status: "not_pushed", detail: "no origin remote" };
		const remoteRef = `refs/heads/${branch.out}`;
		const verify = (): string | undefined => {
			const ls = runGit(root, ["ls-remote", "origin", remoteRef], 30_000);
			if (!ls.ok || !ls.out) return undefined;
			return ls.out.split(/\s+/)[0];
		};
		const push = runGit(root, ["push"], 30_000);
		const remoteCommit = verify();
		if (remoteCommit === commitSha) {
			return {
				status: push.ok ? "pushed" : "remote_commit",
				remoteUrl: remote.out,
				remoteRef,
				remoteCommit,
				detail: push.ok ? undefined : `push command failed but remote ref carries the commit: ${push.err.split("\n")[0]}`,
			};
		}
		return {
			status: "not_pushed",
			remoteUrl: remote.out,
			remoteRef,
			remoteCommit,
			detail: push.ok ? `remote ${remoteRef} is ${remoteCommit ?? "absent"}, not ${commitSha.slice(0, 12)}` : push.err.split("\n")[0],
		};
	} catch (e) {
		return { status: "not_pushed", detail: String(e) };
	}
}

/** Linear /done commit step (HOME-118): owner-confirmed exact-path staging,
 *  credential scan, commit `session close: <identifier>`, guarded push. Returns a
 *  one-line notice for pendingNotices, or null when nothing happened. NEVER throws. */
export async function commitSessionWork(ui: FreezeUi, cwd: string, identifier: string): Promise<string | null> {
	try {
		const top = runGit(cwd, ["rev-parse", "--show-toplevel"]);
		if (!top.ok) {
			ui.notify("no git repo here — nothing to commit", "info");
			return null;
		}
		const root = top.out;
		const status = runGit(root, ["status", "--porcelain", "-z"]);
		const all = status.ok ? parsePorcelain(status.raw) : [];
		const excluded = all.filter(p => p.startsWith("packages/")); // Chris's own lane — never staged (HOME-117/118)
		const commitable = all.filter(p => !p.startsWith("packages/"));
		const leftAlone = excluded.length ? `left alone: ${excluded.length} file(s) under packages/ (yours)` : "";
		if (commitable.length === 0) {
			ui.notify(`nothing to commit${leftAlone ? ` — ${leftAlone}` : ""}`, "info");
			return null;
		}
		const listed = commitable.slice(0, 20).join("\n") + (commitable.length > 20 ? `\n+${commitable.length - 20} more` : "");
		const body = leftAlone ? `${listed}\n\n${leftAlone}` : listed;
		const yes = await ui.confirm(`Commit session work? (${commitable.length} files → session close: ${identifier})`, body);
		if (!yes) return null;
		const add = runGit(root, ["add", "--", ...commitable]); // exact paths only — never `git add .`
		if (!add.ok) {
			ui.notify(`commit failed at staging: ${add.err.split("\n")[0]}`, "error");
			return null;
		}
		const secrets = findSecrets(runGit(root, ["diff", "--cached"]).out);
		if (secrets.length) {
			runGit(root, ["reset", "-q", "--", ...commitable]);
			ui.notify(`commit refused — possible secret in staged diff: ${secrets.join(", ")}. Commit manually if false alarm.`, "error");
			return null;
		}
		const commit = runGit(root, ["commit", "-m", `session close: ${identifier}`]);
		if (!commit.ok) {
			runGit(root, ["reset", "-q", "--", ...commitable]);
			ui.notify(`commit failed: ${commit.err.split("\n")[0] || commit.out.split("\n")[0]}`, "error");
			return null;
		}
		const n = commitable.length;
		if (root === join(homedir(), ".claude")) return `[linear] /done committed ${n} file(s) on ${identifier} (not pushed — ~/.claude)`;
		if (!runGit(root, ["remote", "get-url", "origin"]).ok) return `[linear] /done committed ${n} file(s) on ${identifier} (not pushed — no remote)`;
		const push = runGit(root, ["push"], 30_000);
		if (!push.ok) {
			ui.notify(`committed; push failed: ${push.err.split("\n")[0]}`, "warning");
			return `[linear] /done committed ${n} file(s) on ${identifier} (not pushed — push failed)`;
		}
		return `[linear] /done committed and pushed ${n} file(s) for ${identifier}`;
	} catch (e) {
		ui.notify(`/done commit step failed: ${String(e)}`, "error");
		return null;
	}
}
