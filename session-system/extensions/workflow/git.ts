/**
 * workflow/git.ts — git internals for the workflow host.
 *
 * freezeCandidateCommit + pushCandidate: the Work Ledger split (HOME-147, HOME-149) —
 * /summary freezes the owner-approved path set into a local candidate commit,
 * /done pushes the recorded commit and verifies the remote ref.
 *
 * candidateSha256 implements work.omp.dev/v1/candidate-sha256 byte-exactly per
 * contracts/v1/decisions/0004 item 6; golden vectors live in
 * contracts/v1/candidate-hash.json and are pinned by commit-step.test.ts.
 */
import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, readFileSync, statSync } from "node:fs";
import { isAbsolute, join as joinPath } from "node:path";
/** Run git in cwd; timeoutMs guards network ops (push). `raw` is untrimmed stdout —
 *  porcelain -z parsing needs the leading space of the first `XY path` entry. */
export function runGit(cwd: string, args: string[], timeoutMs = 10_000): { ok: boolean; out: string; raw: string; err: string } {
	const r = spawnSync("git", ["-C", cwd, ...args], { encoding: "utf8", timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024 });
	const raw = r.stdout ?? "";
	const err = (r.stderr ?? (r.error ? r.error.message : "")).trim();
	return { ok: r.status === 0 && !r.error, out: raw.trim(), raw, err };
}

export function inProgressGitOp(cwd: string): boolean {
	try {
		const top = runGit(cwd, ["rev-parse", "--git-dir"]);
		if (!top.ok) return false;
		const gitDir = isAbsolute(top.out) ? top.out : joinPath(cwd, top.out);
		const markers = [
			"MERGE_HEAD",
			"REBASE_HEAD",
			"rebase-merge",
			"rebase-apply",
			"CHERRY_PICK_HEAD",
			"REVERT_HEAD",
			"BISECT_LOG",
		];
		return markers.some(m => existsSync(joinPath(gitDir, m)));
	} catch {
		return false;
	}
}

/** Run git with raw stdout — required where byte fidelity matters (path lists
 *  that must be validated as strict UTF-8 before they bind a candidate). */
function runGitRaw(cwd: string, args: string[], timeoutMs = 10_000): { ok: boolean; stdout: Buffer; err: string } {
	const r = spawnSync("git", ["-C", cwd, ...args], { timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024 });
	const err = (r.stderr?.toString("utf8") ?? (r.error ? r.error.message : "")).trim();
	return { ok: r.status === 0 && !r.error, stdout: r.stdout ?? Buffer.alloc(0), err };
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

/** Dirty paths at this instant, relative to repository root. Untracked
 *  directories are enumerated per file (-uall) so freeze buckets and
 *  pre-session captures always name exact files, never collapsible dirs. */
export function dirtyPaths(cwd: string): string[] {
	const top = runGit(cwd, ["rev-parse", "--show-toplevel"]);
	if (!top.ok) return [];
	const status = runGit(top.out, ["status", "--porcelain", "-z", "--untracked-files=all"]);
	return status.ok ? parsePorcelain(status.raw) : [];
}

/** Current HEAD commit at cwd's repository root, or null outside a repo. */
export function headCommit(cwd: string): string | null {
	const head = runGit(cwd, ["rev-parse", "HEAD"]);
	return head.ok && /^[0-9a-f]{40,64}$/.test(head.out) ? head.out : null;
}

/** First commit before a candidate's implementation range. */
export function parentCommit(cwd: string, commit: string): string | null {
	const parent = runGit(cwd, ["rev-parse", `${commit}^`]);
	return parent.ok && /^[0-9a-f]{40,64}$/.test(parent.out) ? parent.out : null;
}

export type CandidateDriftShape = "unchanged" | "fixes-on-top" | "unrelated";

/** Relationship between the current HEAD and a reviewed candidate commit. */
export function candidateDrift(cwd: string, candidateCommit: string | null | undefined): { shape: CandidateDriftShape; head: string | null } {
	const head = headCommit(cwd);
	if (!head || !candidateCommit) {
		return { shape: "unrelated", head: head ?? null };
	}
	if (head === candidateCommit) {
		return { shape: "unchanged", head };
	}
	const ancestor = runGit(cwd, ["merge-base", "--is-ancestor", candidateCommit, head]);
	if (ancestor.ok) {
		return { shape: "fixes-on-top", head };
	}
	return { shape: "unrelated", head };
}


/** SHA-256 of the canonical `git diff --binary --full-index <start>..<final> --`
 *  byte stream — the range-diff manifest hash sealed into the close attempt
 *  (OMP-47). Must match the auditor's git-range-sha256 mode contract exactly
 *  (OMP-96). Null when the range cannot be produced (unknown commits, no repo). */
export function rangeDiffSha256(cwd: string, startCommit: string, finalCommit: string): string | null {
	const top = runGit(cwd, ["rev-parse", "--show-toplevel"]);
	if (!top.ok) return null;
	const diff = spawnSync("git", ["-C", top.out, "diff", "--binary", "--full-index", `${startCommit}..${finalCommit}`, "--"], { timeout: 30_000, maxBuffer: 256 * 1024 * 1024 });
	if (diff.status !== 0 || !diff.stdout) return null;
	return new Bun.CryptoHasher("sha256").update(diff.stdout).digest("hex");
}

const SECRET_PATTERNS: [string, RegExp][] = [
	["linear-key", /\blin_api_[A-Za-z0-9]{16,}\b/],
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

export function validateExecutionPath(path: string, root?: string): { valid: boolean; error?: string } {
	if (!path || typeof path !== "string" || path.trim() !== path) return { valid: false, error: "path is empty or has surrounding whitespace" };
	if (path !== path.normalize("NFC")) return { valid: false, error: `path must be Unicode NFC normalized: ${path}` };
	if (path.startsWith("/") || path.startsWith("\\") || /^[a-zA-Z]:/.test(path)) return { valid: false, error: `path must be repository-relative: ${path}` };
	if (path.startsWith("./") || path.endsWith("/") || path.includes("\\") || path.includes("//")) return { valid: false, error: `path must use POSIX relative separators without redundant slashes or ./: ${path}` };
	const segments = path.split("/");
	if (segments.some(segment => segment === "." || segment === ".." || segment === "")) return { valid: false, error: `path must not contain . or .. segments: ${path}` };
	if (/[\x00-\x1f\x7f]/.test(path)) return { valid: false, error: `path contains control characters: ${path}` };
	if (/[*?[\]{}]/.test(path)) return { valid: false, error: `path contains glob metacharacters: ${path}` };

	if (root) {
		let current = root;
		for (let i = 0; i < segments.length; i++) {
			const seg = segments[i];
			const subpath = segments.slice(0, i + 1).join("/");
			current = joinPath(current, seg);

			// Check git index for submodule (mode 160000)
			const stageCheck = runGit(root, ["ls-files", "--stage", subpath]);
			if (stageCheck.ok && stageCheck.out.startsWith("160000")) {
				return { valid: false, error: `path component is a git submodule: ${seg} in ${path}` };
			}

			try {
				const lst = lstatSync(current);
				if (lst.isSymbolicLink()) {
					return { valid: false, error: `path component is a symbolic link: ${seg} in ${path}` };
				}
				if (i < segments.length - 1) {
					if (!lst.isDirectory()) {
						return { valid: false, error: `path parent component is not a directory: ${seg} in ${path}` };
					}
					if (existsSync(joinPath(current, ".git"))) {
						return { valid: false, error: `path component is a git submodule: ${seg} in ${path}` };
					}
				}
				if (i === segments.length - 1 && lst.isDirectory()) {
					return { valid: false, error: `path is a directory, not a file: ${path}` };
				}
				if (i === segments.length - 1 && lst.size >= 50_000_000) {
					return { valid: false, error: `path is >= 50MB: ${path}` };
				}
			} catch (e: unknown) {
				if (e && typeof e === "object" && "code" in e && e.code === "ENOENT") {
					if (i < segments.length - 1) {
						return { valid: false, error: `parent directory does not exist for new file: ${path}` };
					}
					const parentSegments = segments.slice(0, -1);
					const parentDir = joinPath(root, ...parentSegments);
					try {
						const parentLst = lstatSync(parentDir);
						if (!parentLst.isDirectory() || parentLst.isSymbolicLink()) {
							return { valid: false, error: `parent directory is invalid or symlink for new file: ${path}` };
						}
						// The repository root's own .git is never a submodule marker;
						// only nested parent directories carrying .git are embedded repos.
						if (parentSegments.length > 0 && existsSync(joinPath(parentDir, ".git"))) {
							return { valid: false, error: `parent directory is a git submodule: ${path}` };
						}
					} catch {
						return { valid: false, error: `parent directory missing for new file: ${path}` };
					}
					break;
				}
				return { valid: false, error: `could not stat path component: ${String(e)}` };
			}
		}
	}

	return { valid: true };
}

export function validateExecutionPaths(paths: readonly string[], root?: string): { valid: boolean; error?: string; normalized: string[] } {
	if (!paths.length) return { valid: false, error: "paths array must not be empty", normalized: [] };
	const normalized: string[] = [];
	const seenExact = new Set<string>();
	const seenCaseFold = new Set<string>();
	const seenNfc = new Set<string>();
	const utf8 = new TextEncoder();
	for (const p of paths) {
		const check = validateExecutionPath(p, root);
		if (!check.valid) return { valid: false, error: check.error, normalized: [] };
		if (seenExact.has(p)) return { valid: false, error: `duplicate path in plan: ${p}`, normalized: [] };
		const caseFold = p.toLowerCase();
		if (seenCaseFold.has(caseFold)) return { valid: false, error: `case collision in paths: ${p}`, normalized: [] };
		const nfc = p.normalize("NFC");
		if (seenNfc.has(nfc)) return { valid: false, error: `canonical Unicode collision in paths: ${p}`, normalized: [] };
		seenExact.add(p);
		seenCaseFold.add(caseFold);
		seenNfc.add(nfc);
		normalized.push(p);
	}
	normalized.sort((a, b) => {
		const x = utf8.encode(a);
		const y = utf8.encode(b);
		const n = Math.min(x.length, y.length);
		for (let i = 0; i < n; i++) if (x[i] !== y[i]) return x[i] - y[i];
		return x.length - y.length;
	});
	return { valid: true, normalized };
}
import { candidateSha256 } from "@oh-my-pi/pi-work-client";

export interface CandidateFreeze {
	root: string;
	paths: string[]; // exact committed file list (diff-tree), unsorted as reported
	commitSha: string;
	candidateSha256: string;
}

/** A freeze that produced no candidate: `declined` = owner said no,
 *  `nothing` = no bytes to bind, `failed` = mechanical/safety refusal.
 *  The reason has already been notified when this is returned (OMP-57:
 *  no silent outcomes). */
export interface FreezeRefusal {
	refused: "declined" | "nothing" | "failed";
	reason: string;
}
export type FreezeOutcome = CandidateFreeze | FreezeRefusal;

export interface FreezeUi {
	confirm(title: string, body: string): Promise<boolean>;
	notify(msg: string, level?: "info" | "warning" | "error"): void;
}

/** The commit's complete file list, exactly as stored. -z gives raw unquoted
 *  bytes; each path is decoded with a fatal UTF-8 decoder — a non-UTF-8 path
 *  name refuses the candidate (decision 0004 item 6). */
function committedPaths(root: string, commitSha: string): string[] {
	const out = runGitRaw(root, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "-z", commitSha]);
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

/** Complete path list for owner confirmation — every path, control characters
 *  escaped so a crafted filename cannot spoof the dialog (Sol review, OMP-57). */
function listPaths(paths: readonly string[]): string {
	return paths.map(p => p.replace(/[\u0000-\u001f\u007f]/g, c => JSON.stringify(c).slice(1, -1))).join("\n");
}

/** /summary freeze (HOME-147, OMP-57): owner confirms exact path sets, only
 *  those paths are staged (literal pathspecs), added bytes are
 *  credential-scanned fail-closed, one local candidate commit
 *  `session candidate: <key>` is created — carrying a
 *  `Work-Candidate: <planned candidate id>` trailer — and its canonical
 *  candidate hash computed. Pre-session dirty paths and packages/ (Chris's
 *  own lane) are never swept silently AND never dropped silently: each
 *  nonempty group is offered behind its own explicit opt-in confirm showing
 *  the complete path list (OMP-57 — a build session that dies leaves the
 *  whole candidate as pre-session dirt, and an approved plan may
 *  legitimately touch packages/). When nothing ends up committable, the
 *  owner may adopt current HEAD instead; its SHA binds the already-committed
 *  work. Every refused branch notifies and returns a typed refusal — a
 *  silent null is a defect (OMP-57). Retry reuse only accepts a marker
 *  commit whose trailer names the current planned candidate. */
export async function freezeCandidateCommit(
	ui: FreezeUi,
	cwd: string,
	key: string,
	candidateId: string,
	preExistingDirtyPaths: readonly string[] = [],
	options: { mode?: "manual" | "execution"; sealedPaths?: readonly string[] } = {},
): Promise<FreezeOutcome> {
	const refuse = (refused: FreezeRefusal["refused"], reason: string, level: "info" | "warning" | "error" = "warning"): FreezeRefusal => {
		ui.notify(reason, level);
		return { refused, reason };
	};
	try {
		const top = runGit(cwd, ["rev-parse", "--show-toplevel"]);
		if (!top.ok) return refuse("failed", "no git repo here — cannot freeze a candidate");
		const root = top.out;
		const status = runGit(root, ["status", "--porcelain", "-z", "--untracked-files=all"]);
		if (!status.ok) return refuse("failed", `freeze failed — could not enumerate the working tree: ${status.err.split("\n")[0]}`, "error");
		const all = parsePorcelain(status.raw);

		if (options.mode === "execution") {
			const sealed = new Set(options.sealedPaths ?? []);
			if (!sealed.size) return refuse("failed", "execution freeze requires non-empty sealed paths", "error");
			const staged = runGit(root, ["diff", "--cached", "--name-only"]);
			if (!staged.ok || staged.out !== "") return refuse("failed", "execution freeze refused: pre-staged entries in index", "error");
			const unsealed = all.filter(p => !sealed.has(p));
			if (unsealed.length > 0) return refuse("failed", `execution freeze refused: unsealed modified paths (${unsealed.join(", ")})`, "error");
			for (const p of sealed) {
				try {
					const full = joinPath(root, p);
					const st = statSync(full);
					if (st.size >= 50_000_000) return refuse("failed", `execution freeze refused: file >= 50MB (${p})`, "error");
				} catch { /* deleted file is fine */ }
			}
			const committable = all.filter(p => sealed.has(p));
			if (committable.length === 0) {
				const head = runGit(root, ["log", "-1", "--format=%H%x00%B"]);
				const sep = head.ok ? head.out.indexOf("\x00") : -1;
				const headSha = sep > 0 ? head.out.slice(0, sep) : "";
				const headLines = sep > 0 ? head.out.slice(sep + 1).split("\n") : [];
				if (!headSha) return refuse("failed", "execution freeze refused: no HEAD commit to bind as candidate", "error");
				// Re-freeze idempotency: current HEAD already is this candidate.
				if (headLines[0] === `session candidate: ${key}` && headLines.includes(`Work-Candidate: ${candidateId}`)) {
					const paths = committedPaths(root, headSha);
					return { root, paths, commitSha: headSha, candidateSha256: candidateSha256(headSha, paths) };
				}
				// OMP-188: already-delivered baseline. No sealed path carries
				// changes, so bind current HEAD as the execution candidate instead
				// of stranding the grant behind refuse:"nothing" — the audit stays
				// the completion arbiter for whether the baseline satisfies the
				// sealed criteria.
				ui.notify(`execution freeze: sealed paths are unchanged — binding baseline HEAD ${headSha.slice(0, 12)} as the candidate (already-delivered path; the audit decides completion)`, "info");
				const paths = committedPaths(root, headSha);
				return { root, paths, commitSha: headSha, candidateSha256: candidateSha256(headSha, paths) };
			}
			for (const p of committable) {
				const full = joinPath(root, p);
				try {
					const buf = readFileSync(full);
					if (buf.includes(0)) {
						return refuse("failed", `execution freeze refused: binary file staged (${p})`, "error");
					}
				} catch (err) {
					return refuse("failed", `execution freeze refused: unreadable file for credential scan (${p}): ${String(err)}`, "error");
				}
			}
			const add = runGit(root, ["--literal-pathspecs", "add", "--", ...committable]);
			if (!add.ok) return refuse("failed", `execution freeze staging failed: ${add.err}`, "error");
			const cached = runGit(root, ["diff", "--cached"]);
			if (!cached.ok) {
				return refuse("failed", `execution freeze diff unreadable: ${cached.err}`, "error");
			}
			const secrets = findSecrets(cached.out);
			if (secrets.length) {
				return refuse("failed", `execution freeze secret detected: ${secrets.join(", ")}`, "error");
			}
			const scannedTree = runGit(root, ["write-tree"]);
			if (!scannedTree.ok) {
				return refuse("failed", `execution freeze write-tree failed: ${scannedTree.err}`, "error");
			}
			const preCommitHead = runGit(root, ["rev-parse", "HEAD"]).out;
			const commitTree = runGit(root, ["commit-tree", scannedTree.out, "-p", preCommitHead, "-m", `session candidate: ${key}\n\nWork-Candidate: ${candidateId}`]);
			if (!commitTree.ok) {
				return refuse("failed", `execution freeze commit-tree failed: ${commitTree.err}`, "error");
			}
			const commitSha = commitTree.out;
			const updateRef = runGit(root, ["update-ref", "HEAD", commitSha, preCommitHead]);
			if (!updateRef.ok) {
				return refuse("failed", `execution freeze update-ref failed: ${updateRef.err}`, "error");
			}
			const afterStatus = runGit(root, ["status", "--porcelain", "-z", "--untracked-files=all"]);
			if (!afterStatus.ok || parsePorcelain(afterStatus.raw).length > 0) {
				return refuse("failed", "execution freeze repository not clean after candidate commit", "error");
			}
			const paths = committedPaths(root, commitSha);
			return { root, paths, commitSha, candidateSha256: candidateSha256(commitSha, paths) };
		}

		const preExisting = new Set(preExistingDirtyPaths);
		const lanePaths = all.filter(p => p.startsWith("packages/"));
		const inheritedPaths = all.filter(p => !p.startsWith("packages/") && preExisting.has(p));
		const committable = all.filter(p => !p.startsWith("packages/") && !preExisting.has(p));
		const leftAloneGroups: string[] = [];
		// A group-level yes binds every listed path — unusually large files
		// (stray dumps, build artifacts) must be LOUD in the dialog, not one
		// line among many (Sol review: 4.2 GB core dump sat in an inherited set).
		const flagLarge = (paths: readonly string[]): string => {
			const big: string[] = [];
			for (const p of paths) {
				try {
					const size = statSync(joinPath(root, p)).size;
					if (size >= 50_000_000) big.push(`${p} (${Math.round(size / 1_000_000)} MB)`);
				} catch { /* deleted or unreadable — nothing to flag */ }
			}
			return big.length ? `\n\nWARNING — unusually large file(s) in this group, check they belong to ${key}:\n${listPaths(big)}` : "";
		};
		if (inheritedPaths.length > 0) {
			const yes = await ui.confirm(
				`Include ${inheritedPaths.length} pre-session file(s) in the ${key} candidate?`,
				`These were already modified when this session started — include them ONLY if they are ${key}'s work from an earlier session:\n${listPaths(inheritedPaths)}${flagLarge(inheritedPaths)}`,
			);
			if (yes) committable.push(...inheritedPaths);
			else leftAloneGroups.push(`${inheritedPaths.length} pre-session file(s)`);
		}
		if (lanePaths.length > 0) {
			const yes = await ui.confirm(
				`Include ${lanePaths.length} file(s) under packages/ in the ${key} candidate?`,
				`packages/ is normally your own lane — include ONLY if the approved plan for ${key} touches it:\n${listPaths(lanePaths)}${flagLarge(lanePaths)}`,
			);
			if (yes) committable.push(...lanePaths);
			else leftAloneGroups.push(`${lanePaths.length} file(s) under packages/`);
		}
		const leftAlone = leftAloneGroups.length ? `left alone: ${leftAloneGroups.join(", ")}` : "";
		if (committable.length === 0) {
			// Retry after a lost finalize response (HOME-147): the previous
			// /summary may already have created this attempt's candidate commit.
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
			if (!headSha) return refuse("nothing", `nothing to freeze${leftAlone ? ` — ${leftAlone}` : ""}`, "info");
			const paths = committedPaths(root, headSha);
			const details = [
				`current HEAD: ${headSha.slice(0, 12)} ${headLines[0] ?? ""}`,
				leftAlone ? `${leftAlone} — these dirty paths will NOT be part of the candidate` : "",
			].filter(Boolean).join("\n\n");
			const yes = await ui.confirm(`Use current HEAD as the candidate for ${key}?`, details);
			if (!yes) return refuse("declined", `freeze declined — current HEAD not adopted as the ${key} candidate; /summary stays blocked`);
			ui.notify(`using current HEAD ${headSha.slice(0, 12)} as the candidate for ${key}`, "info");
			return { root, paths, commitSha: headSha, candidateSha256: candidateSha256(headSha, paths) };
		}
		// The commit records the WHOLE index: refuse when entries are already
		// staged outside this freeze, or unconfirmed bytes would bind into the
		// audited candidate (Sol review).
		const staged = runGit(root, ["diff", "--cached", "--name-only"]);
		if (!staged.ok) return refuse("failed", `freeze failed — could not inspect the index: ${staged.err.split("\n")[0]}`, "error");
		if (staged.out !== "") return refuse("failed", "freeze refused — the index already has staged entries; commit or unstage them first, then rerun /summary", "error");
		const body = leftAlone ? `${listPaths(committable)}\n\n${leftAlone}` : listPaths(committable);
		const yes = await ui.confirm(`Freeze the candidate? (${committable.length} files → session candidate: ${key})`, body);
		if (!yes) return refuse("declined", `freeze declined — no candidate commit created for ${key}; /summary stays blocked`);
		// --literal-pathspecs: a filename containing glob characters stages itself only.
		const add = runGit(root, ["--literal-pathspecs", "add", "--", ...committable]);
		if (!add.ok) return refuse("failed", `freeze failed at staging: ${add.err.split("\n")[0]}`, "error");
		const cached = runGit(root, ["diff", "--cached"]);
		if (!cached.ok) {
			// Fail CLOSED: an unreadable diff must never skip the credential scan.
			runGit(root, ["--literal-pathspecs", "reset", "-q", "--", ...committable]);
			return refuse("failed", `freeze refused — could not read the staged diff for the credential scan: ${cached.err.split("\n")[0]}`, "error");
		}
		const secrets = findSecrets(cached.out);
		if (secrets.length) {
			runGit(root, ["--literal-pathspecs", "reset", "-q", "--", ...committable]);
			return refuse("failed", `freeze refused — possible secret in staged diff: ${secrets.join(", ")}. Commit manually if false alarm.`, "error");
		}
		// Bind the scan to the exact staged tree: `write-tree` records the index
		// the scan just approved; the committed tree must be THAT object, or a
		// commit-time hook mutated/staged bytes after the scan (same-path content
		// swaps included — a path-set check alone cannot see those; Sol review).
		const scannedTree = runGit(root, ["write-tree"]);
		if (!scannedTree.ok) {
			runGit(root, ["--literal-pathspecs", "reset", "-q", "--", ...committable]);
			return refuse("failed", `freeze refused — could not record the scanned tree: ${scannedTree.err.split("\n")[0]}`, "error");
		}
		const preCommitHead = runGit(root, ["rev-parse", "HEAD"]).out;
		const commit = runGit(root, ["commit", "-m", `session candidate: ${key}\n\nWork-Candidate: ${candidateId}`]);
		if (!commit.ok) {
			runGit(root, ["--literal-pathspecs", "reset", "-q", "--", ...committable]);
			return refuse("failed", `freeze commit failed: ${commit.err.split("\n")[0] || commit.out.split("\n")[0]}`, "error");
		}
		const commitSha = runGit(root, ["rev-parse", "HEAD"]).out;
		const committedTree = runGit(root, ["rev-parse", "HEAD^{tree}"]);
		// The poisoned marker commit MUST NOT survive a refusal: a later
		// /summary's retry-reuse branch trusts marker+trailer alone, so a
		// surviving mismatch would bind sight-unseen on retry. A mixed reset to
		// the pre-commit HEAD removes the commit and its staging while
		// preserving every worktree byte.
		if (!committedTree.ok || committedTree.out !== scannedTree.out) {
			const rollback = runGit(root, ["reset", "-q", "--mixed", preCommitHead]);
			if (!rollback.ok) {
				return refuse("failed", `freeze aborted — commit ${commitSha.slice(0, 12)} does not match the scanned tree AND rollback failed (${rollback.err.split("\n")[0]}); reset to ${preCommitHead.slice(0, 12)} manually before rerunning /summary`, "error");
			}
			return refuse("failed", "freeze aborted — the committed tree differs from the scanned tree (a hook or concurrent process changed staged bytes after the credential scan); the commit was rolled back, worktree bytes preserved. Fix the interference and rerun /summary", "error");
		}
		const paths = committedPaths(root, commitSha);
		return { root, paths, commitSha, candidateSha256: candidateSha256(commitSha, paths) };
	} catch (e) {
		return refuse("failed", `candidate freeze failed: ${String(e)}`, "error");
	}
}

export interface PushOutcome {
	status: "pushed" | "remote_commit" | "contained" | "not_pushed";
	remoteUrl?: string;
	remoteRef?: string;
	remoteCommit?: string;
	priorTip?: string | null;
	detail?: string;
}

/** Candidate push used at /summary freeze and repeated by /done. Reads the
 *  remote tip first: tip equals the frozen commit → "remote_commit" (no push);
 *  tip provably contains it on the same branch → "contained" (no push, the
 *  branch is never rewound); otherwise push the exact commit and verify the
 *  remote ref resolves to it. Divergence and unverifiable containment fail
 *  closed as "not_pushed" naming both commits. NEVER throws. */
export function currentSymbolicRef(root: string): string | undefined {
	const branch = runGit(root, ["symbolic-ref", "--quiet", "HEAD"]);
	if (!branch.ok || !branch.out) return undefined;
	const ref = branch.out.trim();
	return ref.startsWith("refs/heads/") ? ref : undefined;
}

export function pushCandidate(root: string, commitSha: string, expectedBaseline?: string): PushOutcome {
	try {
		const remoteRef = currentSymbolicRef(root);
		if (!remoteRef) {
			return { status: "not_pushed", detail: "detached HEAD — no branch to push" };
		}
		const branchName = remoteRef.slice("refs/heads/".length);
		const remote = runGit(root, ["remote", "get-url", "origin"]);
		if (!remote.ok || !remote.out) return { status: "not_pushed", detail: "no origin remote" };
		const verify = (): string | undefined => {
			const ls = runGit(root, ["ls-remote", "origin", remoteRef], 30_000);
			if (!ls.ok || !ls.out) return undefined;
			return ls.out.split(/\s+/)[0];
		};
		const preTip = verify();
		if (preTip === commitSha) {
			const resolvedPrior = expectedBaseline ?? parentCommit(root, commitSha) ?? preTip;
			return { status: "remote_commit", remoteUrl: remote.out, remoteRef, remoteCommit: preTip, priorTip: resolvedPrior, detail: "already at remote tip" };
		}
		if (preTip) {
			// OMP-99: a newer same-branch tip that contains the candidate is push
			// proof — never rewind the branch to forge tip-equality. Fetch first so
			// ancestry is checked against the live remote, not stale local refs; a
			// failed fetch means containment is unverifiable and the close fails
			// closed rather than guessing from stale objects.
			const fetch = runGit(root, ["fetch", "origin", remoteRef], 30_000);
			if (!fetch.ok) {
				return {
					status: "not_pushed",
					remoteUrl: remote.out,
					remoteRef,
					remoteCommit: preTip,
					detail: `containment unverifiable — fetch failed: ${fetch.err.split("\n")[0]}`,
				};
			}
			const ancestor = runGit(root, ["merge-base", "--is-ancestor", commitSha, preTip]);
			if (ancestor.ok) {
				return {
					status: "contained",
					remoteUrl: remote.out,
					remoteRef,
					remoteCommit: preTip,
					priorTip: preTip,
					detail: `containment: ${preTip} contains ${commitSha} on ${branchName} (merge-base --is-ancestor exit 0, verified ${new Date().toISOString()})`,
				};
			}
		}
		const push = runGit(root, ["push", "origin", `${commitSha}:${remoteRef}`], 30_000);
		const remoteCommit = verify();
		if (remoteCommit === commitSha) {
			return {
				status: push.ok ? "pushed" : "remote_commit",
				remoteUrl: remote.out,
				remoteRef,
				remoteCommit,
				priorTip: preTip ?? null,
				detail: push.ok ? undefined : `push command failed but remote ref carries the commit: ${push.err.split("\n")[0]}`,
			};
		}
		return {
			status: "not_pushed",
			remoteUrl: remote.out,
			remoteRef,
			remoteCommit,
			detail: `remote ${remoteRef} is ${remoteCommit ?? "absent"}, not ${commitSha} and does not contain it${push.ok ? "" : ` (${push.err.split("\n")[0]})`}`,
		};
	} catch (e) {
		return { status: "not_pushed", detail: String(e) };
	}
}
