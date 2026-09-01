/**
 * HOME-118: /done commits and pushes the session's work. Contracts proven here
 * against the exported commit-step helpers (no Linear network involved):
 *  - porcelain -z parsing keeps rename NEW paths only (staging the old path
 *    would error or commit the wrong file);
 *  - the credential scan fires on ADDED lines only (a secret on a removed line
 *    must not false-abort; a secret on an added line must never reach the
 *    public fork);
 *  - the integration path stages exact paths (literal pathspecs); pre-session
 *    and packages/* paths are separate owner-confirmed opt-in groups (OMP-57)
 *    — declined groups stay uncommitted, accepted groups freeze;
 *  - a secret hit aborts the commit and restores the tree (no staged residue);
 *  - owner decline leaves the repo untouched and returns a typed refusal.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";
import { candidateSha256 } from "@oh-my-pi/pi-work-client";
import { candidateDrift, dirtyPaths, findSecrets, freezeCandidateCommit, type GhPrRunner, parentCommit, parsePorcelain, pushCandidate, rangeDiffSha256, validateExecutionPaths, verifyMergeConfirmation } from "../extensions/workflow/git";

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-commit-step-"));
afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

let repoSeq = 0;
const makeRepo = (): string => {
	const repo = path.join(tempRoot, `repo-${repoSeq++}`);
	fs.mkdirSync(repo, { recursive: true });
	for (const args of [
		["init", "-q"],
		["config", "user.email", "test@example.com"],
		["config", "user.name", "Test"],
	])
		Bun.spawnSync(["git", ...args], { cwd: repo });
	fs.writeFileSync(path.join(repo, "seed.txt"), "seed\n");
	Bun.spawnSync(["git", "add", "--", "seed.txt"], { cwd: repo });
	Bun.spawnSync(["git", "commit", "-q", "-m", "seed"], { cwd: repo });
	return repo;
};

const git = (repo: string, ...args: string[]): string =>
	Bun.spawnSync(["git", ...args], { cwd: repo }).stdout.toString().trim();

interface FakeUi {
	confirm(title: string, body: string): Promise<boolean>;
	notify(msg: string, level?: "info" | "warning" | "error"): void;
	notices: string[];
	confirms: { title: string; body: string }[];
}
const makeUi = (answer: boolean | ((title: string) => boolean)): FakeUi => {
	const ui: FakeUi = {
		notices: [],
		confirms: [],
		confirm: async (title, body) => {
			ui.confirms.push({ title, body });
			return typeof answer === "function" ? answer(title) : answer;
		},
		notify: (msg, level) => ui.notices.push(`${level ?? "info"}: ${msg}`),
	};
	return ui;
};
const frozenPaths = (outcome: Awaited<ReturnType<typeof freezeCandidateCommit>>): string[] | undefined =>
	"refused" in outcome ? undefined : outcome.paths;

describe("candidate freeze and push", () => {
	test("parsePorcelain keeps rename new path, drops original", () => {
		const z = "M  a.txt\0?? b.txt\0R  new.txt\0old.txt\0";
		expect(parsePorcelain(z)).toEqual(["a.txt", "b.txt", "new.txt"]);
	});

	test("parentCommit anchors the full implementation range", () => {
		const repo = makeRepo();
		const first = git(repo, "rev-parse", "HEAD");
		expect(parentCommit(repo, first)).toBeNull();
		fs.writeFileSync(path.join(repo, "next.txt"), "next\n");
		git(repo, "add", "--", "next.txt");
		git(repo, "commit", "-m", "next");
		const second = git(repo, "rev-parse", "HEAD");
		expect(parentCommit(repo, second)).toBe(first);
	});

	test("findSecrets ignores its own short fixtures but catches credential-length added values", () => {
		expect(findSecrets("+const pattern = /lin_api_[A-Za-z0-9]/;")).toEqual([]);
		const linear = "lin_" + "api_0123456789abcdef";
		expect(findSecrets(`+const k = '${linear}';`)).toEqual(["linear-key"]);
		expect(findSecrets(`-const k = '${linear}';\n+const k = env.KEY;`)).toEqual([]);
		expect(findSecrets("+++ b/file.ts\n+const x = 1;")).toEqual([]);
	});
	test("pre-session files stay uncommitted when the owner declines the inherited group", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "owner.txt"), "owner setting\n");
		const preExisting = dirtyPaths(repo);
		fs.writeFileSync(path.join(repo, "session.txt"), "session work\n");
		const ui = makeUi(title => !title.includes("pre-session"));

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1", preExisting);

		expect(frozenPaths(frozen)).toEqual(["session.txt"]);
		expect(git(repo, "show", "--name-only", "--format=", "HEAD")).toBe("session.txt");
		expect(fs.readFileSync(path.join(repo, "owner.txt"), "utf8")).toBe("owner setting\n");
		expect(git(repo, "status", "--porcelain")).toBe("?? owner.txt");
		expect(ui.confirms[0].title).toContain("pre-session");
		expect(ui.confirms[0].body).toContain("owner.txt");
		expect(ui.confirms[1].body).toContain("left alone: 1 pre-session file(s)");
	});

	test("inherited files freeze into the candidate on explicit owner yes (OMP-57 cross-session close)", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "built-earlier.txt"), "candidate work from a dead session\n");
		fs.writeFileSync(path.join(repo, "also-earlier.txt"), "more of it\n");
		const preExisting = dirtyPaths(repo);
		const ui = makeUi(true);

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1", preExisting);

		expect(frozenPaths(frozen)?.sort()).toEqual(["also-earlier.txt", "built-earlier.txt"]);
		expect(git(repo, "show", "--name-only", "--format=", "HEAD").split("\n").sort()).toEqual(["also-earlier.txt", "built-earlier.txt"]);
		expect(git(repo, "status", "--porcelain")).toBe("");
		expect(ui.confirms[0].title).toContain("2 pre-session file(s)");
	});

	test("packages/ paths stay out when the owner declines the lane group; lookalike prefixes commit (OMP-38 re-probe)", async () => {
		const repo = makeRepo();
		fs.mkdirSync(path.join(repo, "packages/sub"), { recursive: true });
		fs.writeFileSync(path.join(repo, "packages/sub/lib.ts"), "chris lane\n");
		fs.writeFileSync(path.join(repo, "packages-extra.txt"), "session work\n");
		fs.writeFileSync(path.join(repo, "session.txt"), "session work\n");
		const ui = makeUi(title => !title.includes("packages/"));

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");

		// prefix match is path-segment exact: packages/ offered separately, packages-extra.txt committed
		expect(frozenPaths(frozen)).toEqual(["packages-extra.txt", "session.txt"]);
		expect(git(repo, "show", "--name-only", "--format=", "HEAD").split("\n").sort()).toEqual(["packages-extra.txt", "session.txt"]);
		expect(git(repo, "status", "--porcelain")).toBe("?? packages/");
		expect(ui.confirms[0].title).toContain("packages/");
		expect(ui.confirms[1].body).toContain("left alone: 1 file(s) under packages/");
	});

	test("packages/ paths freeze on explicit owner yes (plan-scope lane work, OMP-57)", async () => {
		const repo = makeRepo();
		fs.mkdirSync(path.join(repo, "packages/sub"), { recursive: true });
		fs.writeFileSync(path.join(repo, "packages/sub/lib.ts"), "plan-scope lane work\n");
		fs.writeFileSync(path.join(repo, "session.txt"), "session work\n");
		const ui = makeUi(true);

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");

		expect(frozenPaths(frozen)?.sort()).toEqual(["packages/sub/lib.ts", "session.txt"]);
		expect(git(repo, "status", "--porcelain")).toBe("");
	});

	test("candidate freeze adopts current HEAD when inherited dirt is declined", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "session.txt"), "committed work\n");
		Bun.spawnSync(["git", "add", "--", "session.txt"], { cwd: repo });
		Bun.spawnSync(["git", "commit", "-q", "-m", "work"], { cwd: repo });
		const head = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "owner.txt"), "owner setting\n");
		const ui = makeUi(title => !title.includes("pre-session"));

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1", dirtyPaths(repo));

		expect("refused" in frozen ? undefined : frozen.commitSha).toBe(head);
		expect(frozenPaths(frozen)).toEqual(["session.txt"]);
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("2");
		expect(git(repo, "status", "--porcelain")).toBe("?? owner.txt");
		expect(ui.confirms[1].title).toContain("Use current HEAD");
		expect(ui.confirms[1].body).toContain("NOT be part of the candidate");
	});

	test("secret in staged diff aborts freeze and restores the tree", async () => {
		const repo = makeRepo();
		const linear = "lin_" + "api_FAKE0123456789ABCDEF";
		fs.writeFileSync(path.join(repo, "config.ts"), `const key = '${linear}';\n`);
		const ui = makeUi(true);
		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");
		expect("refused" in frozen && frozen.refused).toBe("failed");
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1"); // seed only
		expect(git(repo, "diff", "--cached", "--name-only")).toBe(""); // nothing left staged
		expect(ui.notices.some(n => n.startsWith("error: freeze refused — possible secret") && n.includes("linear-key"))).toBe(true);
	});

	test("pre-staged index entries refuse the freeze (no unconfirmed bytes in the candidate)", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "sneaky.txt"), "already staged\n");
		Bun.spawnSync(["git", "add", "--", "sneaky.txt"], { cwd: repo });
		fs.writeFileSync(path.join(repo, "session.txt"), "session work\n");
		const ui = makeUi(true);
		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");
		expect("refused" in frozen && frozen.refused).toBe("failed");
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1");
		expect(ui.notices.some(n => n.includes("already has staged entries"))).toBe(true);
	});

	test("glob characters in a filename stage that file only (literal pathspecs)", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "a*.txt"), "glob-named session file\n");
		fs.writeFileSync(path.join(repo, "ab.txt"), "owner file the glob must not sweep\n");
		const preExisting = ["ab.txt"];
		const ui = makeUi(title => !title.includes("pre-session"));
		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1", preExisting);
		expect(frozenPaths(frozen)).toEqual(["a*.txt"]);
		expect(git(repo, "status", "--porcelain")).toBe("?? ab.txt");
	});

	test("unreadable working tree refuses the freeze instead of adopting HEAD (fail closed)", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, ".git/index"), "garbage — not an index\n");
		const ui = makeUi(true);
		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");
		expect("refused" in frozen && frozen.refused).toBe("failed");
		expect(ui.confirms).toEqual([]); // never reached an adoption or freeze prompt
		expect(ui.notices.some(n => n.includes("could not enumerate the working tree"))).toBe(true);
	});

	test("hook-injected paths roll the marker commit back; retry can never bind them (Sol review)", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "session.txt"), "session work\n");
		fs.writeFileSync(path.join(repo, "injected.txt"), "bytes a hook sneaks in\n");
		const hook = path.join(repo, ".git/hooks/pre-commit");
		fs.writeFileSync(hook, "#!/bin/sh\ngit add -- injected.txt\n");
		fs.chmodSync(hook, 0o755);
		const ui = makeUi(title => !title.includes("pre-session"));

		const first = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1", ["injected.txt"]);

		expect("refused" in first && first.refused).toBe("failed");
		expect(ui.notices.some(n => n.includes("rolled back"))).toBe(true);
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1"); // poisoned marker removed
		expect(fs.readFileSync(path.join(repo, "session.txt"), "utf8")).toBe("session work\n"); // worktree preserved

		// Retry: the marker is gone, so the reuse branch cannot bind the injected
		// path; the normal path re-runs and refuses again for the same reason.
		const retryUi = makeUi(title => !title.includes("pre-session"));
		const second = await freezeCandidateCommit(retryUi, repo, "HOME-1", "candidate-1", ["injected.txt"]);
		expect("refused" in second && second.refused).toBe("failed");
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1");
		expect(git(repo, "log", "--all", "--name-only", "--format=")).not.toContain("injected.txt");
	});

	test("hook mutating an approved file's content after the scan rolls the commit back (tree OID binding)", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "session.txt"), "clean scanned content\n");
		const hook = path.join(repo, ".git/hooks/pre-commit");
		// Same path set, different bytes: re-stages secret content at the
		// already-approved path — invisible to any path-set comparison.
		const linear = "lin_" + "api_SNEAKED0123456789ABCDEF";
		fs.writeFileSync(hook, `#!/bin/sh\nprintf "const key = '${linear}';\\n" > session.txt\ngit add -- session.txt\n`);
		fs.chmodSync(hook, 0o755);
		const ui = makeUi(true);

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");

		expect("refused" in frozen && frozen.refused).toBe("failed");
		expect(ui.notices.some(n => n.includes("differs from the scanned tree"))).toBe(true);
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1"); // poisoned marker removed
		expect(git(repo, "log", "--all", "-p", "--format=")).not.toContain(linear);
	});

	test("oversized files in an opt-in group are flagged loudly in the confirm body", async () => {
		const repo = makeRepo();
		const fd = fs.openSync(path.join(repo, "stray.dump"), "w");
		fs.ftruncateSync(fd, 60_000_000); // sparse — no real disk cost
		fs.closeSync(fd);
		fs.writeFileSync(path.join(repo, "session.txt"), "session work\n");
		const ui = makeUi(title => !title.includes("pre-session")); // decline the flagged group

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1", ["stray.dump"]);

		expect(ui.confirms[0].body).toContain("WARNING — unusually large file(s)");
		expect(ui.confirms[0].body).toContain("stray.dump (60 MB)");
		expect(frozenPaths(frozen)).toEqual(["session.txt"]);
		expect(git(repo, "show", "--name-only", "--format=", "HEAD")).toBe("session.txt");
	});

	test("owner decline leaves repo untouched and returns a typed, notified refusal", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "a.txt"), "work\n");
		const ui = makeUi(false);
		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");
		expect("refused" in frozen && frozen.refused).toBe("declined");
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1");
		expect(git(repo, "diff", "--cached", "--name-only")).toBe("");
		expect(ui.notices.some(n => n.startsWith("warning: freeze declined"))).toBe(true);
	});

	test("pushCandidate pushes the exact frozen commit and repeated checks stay idempotent", () => {
		const repo = makeRepo();
		const remote = path.join(tempRoot, `remote-${repoSeq}.git`);
		Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
		git(repo, "remote", "add", "origin", remote);
		const branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD");
		const frozen = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "later.txt"), "later\n");
		git(repo, "add", "--", "later.txt");
		git(repo, "commit", "-q", "-m", "later");

		const first = pushCandidate(repo, frozen);
		expect(first).toMatchObject({ status: "pushed", remoteCommit: frozen });
		expect(git(remote, "rev-parse", `refs/heads/${branch}`)).toBe(frozen);

		const second = pushCandidate(repo, frozen);
		expect(second.status).not.toBe("not_pushed");
		expect(second.remoteCommit).toBe(frozen);
	});

	test("pushCandidate proves containment when a newer same-branch tip contains the frozen commit — no push runs", () => {
		const repo = makeRepo();
		const remote = path.join(tempRoot, `remote-${repoSeq}.git`);
		Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
		git(repo, "remote", "add", "origin", remote);
		const branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD");
		const frozen = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "later.txt"), "later\n");
		git(repo, "add", "--", "later.txt");
		git(repo, "commit", "-q", "-m", "later");
		const later = git(repo, "rev-parse", "HEAD");
		git(repo, "push", "-q", "origin", `HEAD:refs/heads/${branch}`);
		const before = git(remote, "rev-parse", `refs/heads/${branch}`);
		expect(before).toBe(later);

		const outcome = pushCandidate(repo, frozen);

		expect(outcome).toMatchObject({ status: "contained", remoteCommit: later });
		expect(outcome.detail).toContain(`containment: ${later} contains ${frozen}`);
		expect(outcome.detail).toContain("merge-base --is-ancestor exit 0");
		// The branch was never touched — no backward push, tip byte-identical.
		expect(git(remote, "rev-parse", `refs/heads/${branch}`)).toBe(before);
	});

	test("pushCandidate fails closed on a diverged remote, naming both commits, remote untouched", () => {
		const repo = makeRepo();
		const remote = path.join(tempRoot, `remote-${repoSeq}.git`);
		Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
		git(repo, "remote", "add", "origin", remote);
		const branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD");
		const frozen = git(repo, "rev-parse", "HEAD");
		// Unrelated history force-pushed onto the branch: tip neither equals nor
		// contains the candidate. Amend the other repo's root so the two seed
		// commits (identical tree/message/second) cannot share a SHA.
		const other = makeRepo();
		git(other, "commit", "-q", "--amend", "-m", "unrelated-root");
		fs.writeFileSync(path.join(other, "unrelated.txt"), "unrelated\n");
		git(other, "add", "--", "unrelated.txt");
		git(other, "commit", "-q", "-m", "unrelated");
		expect(git(other, "rev-list", "--max-parents=0", "HEAD")).not.toBe(frozen);
		git(other, "remote", "add", "origin", remote);
		git(other, "push", "-q", "--force", "origin", `HEAD:refs/heads/${branch}`);
		const unrelated = git(remote, "rev-parse", `refs/heads/${branch}`);

		const outcome = pushCandidate(repo, frozen);

		expect(outcome.status).toBe("not_pushed");
		expect(outcome.detail).toContain(unrelated);
		expect(outcome.detail).toContain(frozen);
		expect(outcome.detail).toContain("does not contain it");
		expect(git(remote, "rev-parse", `refs/heads/${branch}`)).toBe(unrelated);
	});

	test("pushCandidate reports not_pushed when no remote", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const outcome = pushCandidate(repo, head);
		expect(outcome.status).toBe("not_pushed");
	});

	test("pushCandidate pushes to explicit targetRemoteRef when provided (OMP-212)", () => {
		const repo = makeRepo();
		const remote = path.join(tempRoot, `remote-${repoSeq}.git`);
		Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
		git(repo, "remote", "add", "origin", remote);
		const frozen = git(repo, "rev-parse", "HEAD");
		const targetRef = "refs/heads/execution/omp-212";

		const outcome = pushCandidate(repo, frozen, undefined, targetRef);
		expect(outcome).toMatchObject({
			status: "pushed",
			remoteRef: targetRef,
			remoteCommit: frozen,
		});
		expect(git(remote, "rev-parse", targetRef)).toBe(frozen);
	});

	test("pushCandidate preserves expectedBaseline as priorTip when pushing to new targetRemoteRef (OMP-212)", () => {
		const repo = makeRepo();
		const remote = path.join(tempRoot, `remote-${repoSeq}.git`);
		Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
		git(repo, "remote", "add", "origin", remote);
		const baseline = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "cand.txt"), "cand\n");
		git(repo, "add", "--", "cand.txt");
		git(repo, "commit", "-q", "-m", "cand");
		const candidate = git(repo, "rev-parse", "HEAD");
		const targetRef = "refs/heads/execution/omp-212";

		const outcome = pushCandidate(repo, candidate, baseline, targetRef);
		expect(outcome).toMatchObject({
			status: "pushed",
			remoteRef: targetRef,
			remoteCommit: candidate,
			priorTip: baseline,
		});
		expect(git(remote, "rev-parse", targetRef)).toBe(candidate);
	});

	test("pushCandidate proves containment on explicit targetRemoteRef when newer tip contains commit (OMP-212)", () => {
		const repo = makeRepo();
		const remote = path.join(tempRoot, `remote-${repoSeq}.git`);
		Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
		git(repo, "remote", "add", "origin", remote);
		const frozen = git(repo, "rev-parse", "HEAD");
		const targetRef = "refs/heads/execution/omp-212";

		fs.writeFileSync(path.join(repo, "later.txt"), "later\n");
		git(repo, "add", "--", "later.txt");
		git(repo, "commit", "-q", "-m", "later");
		const later = git(repo, "rev-parse", "HEAD");
		git(repo, "push", "-q", "origin", `HEAD:${targetRef}`);

		const outcome = pushCandidate(repo, frozen, undefined, targetRef);
		expect(outcome).toMatchObject({
			status: "contained",
			remoteRef: targetRef,
			remoteCommit: later,
		});
		expect(outcome.detail).toContain(`containment: ${later} contains ${frozen}`);
	});
});

describe("execution freeze (OMP-188)", () => {
	test("validateExecutionPaths accepts empty array", () => {
		const res = validateExecutionPaths([]);
		expect(res.valid).toBe(true);
		expect(res.normalized).toEqual([]);
	});

	test("binds baseline HEAD when sealed paths are empty and HEAD equals the grant baseline", async () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const ui = makeUi(true);
		const outcome = await freezeCandidateCommit(ui, repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: [],
			expectedBaseline: head,
		});
		if ("refused" in outcome) throw new Error(`expected baseline bind, got refusal: ${outcome.reason}`);
		expect(outcome.commitSha).toBe(head);
		expect(outcome.paths).toEqual(["seed.txt"]);
		expect(outcome.candidateSha256).toBe(candidateSha256(head, ["seed.txt"]));
		expect(git(repo, "rev-parse", "HEAD")).toBe(head);
		expect(ui.notices.some(n => n.includes("binding grant baseline HEAD"))).toBe(true);
	});

	test("refuses when sealed paths are empty and working tree is dirty (names unsealed path)", async () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "dirty.txt"), "unsealed change\n");
		const ui = makeUi(true);
		const outcome = await freezeCandidateCommit(ui, repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: [],
			expectedBaseline: head,
		});
		expect("refused" in outcome && outcome.refused).toBe("failed");
		expect("refused" in outcome ? outcome.reason : "").toContain("unsealed modified paths (dirty.txt)");
		expect(git(repo, "rev-parse", "HEAD")).toBe(head);
	});

	test("binds baseline HEAD when sealed paths carry no changes and HEAD equals the grant baseline", async () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const ui = makeUi(true);
		const outcome = await freezeCandidateCommit(ui, repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: ["seed.txt"],
			expectedBaseline: head,
		});
		if ("refused" in outcome) throw new Error(`expected baseline bind, got refusal: ${outcome.reason}`);
		expect(outcome.commitSha).toBe(head);
		expect(outcome.paths).toEqual(["seed.txt"]);
		expect(outcome.candidateSha256).toBe(candidateSha256(head, ["seed.txt"]));
		expect(git(repo, "rev-parse", "HEAD")).toBe(head); // no new commit
		expect(ui.notices.some(n => n.includes("binding grant baseline HEAD"))).toBe(true);
	});

	test("refuses clean sealed paths without a verified grant baseline (fail closed)", async () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const outcome = await freezeCandidateCommit(makeUi(true), repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: ["seed.txt"],
		});
		expect("refused" in outcome && outcome.refused).toBe("failed");
		expect("refused" in outcome ? outcome.reason : "").toContain("has no verified grant baseline");
		expect(git(repo, "rev-parse", "HEAD")).toBe(head); // repo untouched
	});

	test("refuses a clean HEAD that moved off the grant baseline (foreign commit never adopted)", async () => {
		const repo = makeRepo();
		const baseline = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "foreign.txt"), "unrelated post-grant work\n");
		Bun.spawnSync(["git", "add", "--", "foreign.txt"], { cwd: repo });
		Bun.spawnSync(["git", "commit", "-q", "-m", "foreign clean commit"], { cwd: repo });
		const movedHead = git(repo, "rev-parse", "HEAD");
		expect(movedHead).not.toBe(baseline);
		const outcome = await freezeCandidateCommit(makeUi(true), repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: ["seed.txt"],
			expectedBaseline: baseline,
		});
		expect("refused" in outcome && outcome.refused).toBe("failed");
		expect("refused" in outcome ? outcome.reason : "").toContain("differs from grant baseline");
		expect(git(repo, "rev-parse", "HEAD")).toBe(movedHead); // repo untouched
	});

	test("refuses failed when the repo has no HEAD commit", async () => {
		const repo = path.join(tempRoot, `repo-${repoSeq++}`);
		fs.mkdirSync(repo, { recursive: true });
		for (const args of [["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "T"]])
			Bun.spawnSync(["git", ...args], { cwd: repo });
		const outcome = await freezeCandidateCommit(makeUi(true), repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: ["seed.txt"],
		});
		expect("refused" in outcome && outcome.refused).toBe("failed");
		expect("refused" in outcome ? outcome.reason : "").toContain("no HEAD commit");
	});

	test("dirty sealed path still freezes a fresh marker candidate commit", async () => {
		const repo = makeRepo();
		const baseline = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "feat.ts"), "export const feat = true;\n");
		const outcome = await freezeCandidateCommit(makeUi(true), repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: ["feat.ts"],
		});
		if ("refused" in outcome) throw new Error(`expected freeze, got refusal: ${outcome.reason}`);
		expect(outcome.commitSha).not.toBe(baseline);
		expect(outcome.paths).toEqual(["feat.ts"]);
		expect(git(repo, "log", "-1", "--format=%s")).toBe("session candidate: OMP-1");
	});

	test("re-freeze on the marker HEAD returns the same commit (idempotent)", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "feat.ts"), "export const feat = true;\n");
		const first = await freezeCandidateCommit(makeUi(true), repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: ["feat.ts"],
		});
		if ("refused" in first) throw new Error(`first freeze refused: ${first.reason}`);
		const second = await freezeCandidateCommit(makeUi(true), repo, "OMP-1", "cand-1", [], {
			mode: "execution",
			sealedPaths: ["feat.ts"],
		});
		if ("refused" in second) throw new Error(`re-freeze refused: ${second.reason}`);
		expect(second.commitSha).toBe(first.commitSha);
		expect(second.candidateSha256).toBe(first.candidateSha256);
	});
});

describe("rangeDiffSha256", () => {
	// OMP-96: the sealed manifest digest must hash the canonical
	// `git diff --binary --full-index <start>..<final> --` byte stream — the
	// exact invocation the auditor's git-range-sha256 mode reconstructs. A
	// binary file (NUL bytes) makes --binary load-bearing: without it git
	// emits "Binary files differ" with an abbreviated index line. git defines
	// --binary as implying --full-index, so a dropped --full-index is byte-
	// invisible; the PATH-shim argv log below pins the exact invocation, so
	// removing ANY canonical token from the producer's argv fails this test.
	test("hashes the canonical --binary --full-index stream via the exact canonical argv", () => {
		const repo = makeRepo();
		const start = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "blob.bin"), Buffer.from([0x00, 0x01, 0xff, 0x00, 0x42, 0x00]));
		Bun.spawnSync(["git", "add", "--", "blob.bin"], { cwd: repo });
		Bun.spawnSync(["git", "commit", "-q", "-m", "binary blob"], { cwd: repo });
		const final = git(repo, "rev-parse", "HEAD");

		const digest = (...flags: string[]): string => {
			const p = Bun.spawnSync(["git", "-C", repo, "diff", ...flags, `${start}..${final}`, "--"]);
			expect(p.exitCode).toBe(0);
			return new Bun.CryptoHasher("sha256").update(p.stdout).digest("hex");
		};
		const canonical = digest("--binary", "--full-index");
		const plain = digest();
		const missingBinary = digest("--full-index");

		// PATH-shim git logs every argv, then delegates to the real binary. The
		// helper runs in a child process because spawnSync's PATH lookup does not
		// track in-process env mutation.
		const realGit = Bun.which("git");
		if (!realGit) throw new Error("git not on PATH");
		const shimDir = fs.mkdtempSync(path.join(tempRoot, "git-shim-"));
		const logFile = path.join(shimDir, "argv.log");
		fs.writeFileSync(
			path.join(shimDir, "git"),
			`#!/bin/sh\n{ printf '%s\\037' "$@"; printf '\\n'; } >> "${logFile}"\nexec "${realGit}" "$@"\n`,
			{ mode: 0o755 },
		);
		// Dynamic import exception: this string runs in a separate `bun -e`
		// process (module loading boundary); the specifier is a runtime path.
		const gitTs = path.join(import.meta.dir, "../extensions/workflow/git.ts");
		const script = `const { rangeDiffSha256 } = await import(${JSON.stringify(gitTs)});
			const got = rangeDiffSha256(${JSON.stringify(repo)}, ${JSON.stringify(start)}, ${JSON.stringify(final)});
			if (!got) process.exit(2);
			process.stdout.write(got);`;
		const child = Bun.spawnSync([process.execPath, "-e", script], {
			env: { ...process.env, PATH: `${shimDir}:${process.env.PATH}` },
		});
		expect(child.exitCode).toBe(0);
		const got = child.stdout.toString();

		expect(got).toBe(canonical);
		expect(canonical).not.toBe(plain);
		expect(canonical).not.toBe(missingBinary);

		const invocations = fs
			.readFileSync(logFile, "utf8")
			.split("\n")
			.filter(line => line.length > 0)
			.map(line => line.split("\u001f").slice(0, -1));
		const diffCalls = invocations.filter(args => args.includes("diff"));
		expect(diffCalls.length).toBe(1);
		const args = diffCalls[0]!;
		expect(args[0]).toBe("-C");
		expect(args.slice(2)).toEqual(["diff", "--binary", "--full-index", `${start}..${final}`, "--"]);
	});
});

describe("candidateDrift", () => {
	test("detects unchanged HEAD", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const result = candidateDrift(repo, head);
		expect(result).toEqual({ shape: "unchanged", head });
	});

	test("detects descendant fixes on top", () => {
		const repo = makeRepo();
		const candidate = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "fix.txt"), "fix\n");
		Bun.spawnSync(["git", "add", "--", "fix.txt"], { cwd: repo });
		Bun.spawnSync(["git", "commit", "-q", "-m", "fix"], { cwd: repo });
		const head = git(repo, "rev-parse", "HEAD");
		expect(head).not.toBe(candidate);
		const result = candidateDrift(repo, candidate);
		expect(result).toEqual({ shape: "fixes-on-top", head });
	});

	test("detects orphan/divergent history whose repository still contains the candidate object", () => {
		const repo = makeRepo();
		const candidate = git(repo, "rev-parse", "HEAD");
		Bun.spawnSync(["git", "checkout", "-q", "--orphan", "other-branch"], { cwd: repo });
		Bun.spawnSync(["git", "rm", "-rf", "-q", "."], { cwd: repo });
		fs.writeFileSync(path.join(repo, "other.txt"), "other\n");
		Bun.spawnSync(["git", "add", "--", "other.txt"], { cwd: repo });
		Bun.spawnSync(["git", "commit", "-q", "-m", "other root"], { cwd: repo });
		const head = git(repo, "rev-parse", "HEAD");
		expect(head).not.toBe(candidate);
		// Candidate object still exists in repo
		expect(Bun.spawnSync(["git", "cat-file", "-e", candidate], { cwd: repo }).exitCode).toBe(0);
		const result = candidateDrift(repo, candidate);
		expect(result).toEqual({ shape: "unrelated", head });
	});

	test("handles unreadable / non-repository path", () => {
		const nonRepo = path.join(tempRoot, "not-a-repo");
		fs.mkdirSync(nonRepo, { recursive: true });
		const result = candidateDrift(nonRepo, "0123456789abcdef0123456789abcdef01234567");
		expect(result).toEqual({ shape: "unrelated", head: null });
	});

	test("handles null or undefined candidate commit", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		expect(candidateDrift(repo, null)).toEqual({ shape: "unrelated", head });
		expect(candidateDrift(repo, undefined)).toEqual({ shape: "unrelated", head });
	});
});

describe("merge confirmation gating (OMP-212)", () => {
	const mockGhPassing: GhPrRunner = (_root: string, _query: string) => ({
		ok: true,
		out: {
			pr: {
				state: "MERGED",
				baseRefName: "main",
				headRefName: "execution/omp-212",
			},
			requiredChecks: [
				{ name: "test", state: "SUCCESS", bucket: "pass", status: "COMPLETED", conclusion: "SUCCESS" },
			],
		},
		err: "",
	});

	test("refuses completion for direct push to default branch (direct target-branch publication disabled)", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const outcome = verifyMergeConfirmation(repo, head, "refs/heads/main", "refs/heads/main", mockGhPassing);
		expect(outcome.confirmed).toBe(false);
		expect(outcome.detail).toContain("direct push to protected default branch is disabled");
	});

	test("refuses completion when gh PR query fails (missing or non-zero exit)", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const failingGh: GhPrRunner = () => ({ ok: false, err: "command not found: gh" });
		const outcome = verifyMergeConfirmation(repo, head, "refs/heads/execution/omp-212", "refs/heads/main", failingGh);
		expect(outcome.confirmed).toBe(false);
		expect(outcome.detail).toContain("PR query failed");
	});

	test("refuses completion when PR base branch does not match default branch", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const wrongBaseGh: GhPrRunner = () => ({
			ok: true,
			out: {
				pr: {
					state: "MERGED",
					baseRefName: "other-branch",
				},
				requiredChecks: [{ name: "test", state: "SUCCESS", bucket: "pass" }],
			},
			err: "",
		});
		const outcome = verifyMergeConfirmation(repo, head, "refs/heads/execution/omp-212", "refs/heads/main", wrongBaseGh);
		expect(outcome.confirmed).toBe(false);
		expect(outcome.detail).toContain("expected main");
	});

	test("refuses completion when PR state is OPEN or not MERGED", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const openPrGh: GhPrRunner = () => ({
			ok: true,
			out: {
				pr: {
					state: "OPEN",
					baseRefName: "main",
				},
				requiredChecks: [{ name: "test", state: "SUCCESS", bucket: "pass" }],
			},
			err: "",
		});
		const outcome = verifyMergeConfirmation(repo, head, "refs/heads/execution/omp-212", "refs/heads/main", openPrGh);
		expect(outcome.confirmed).toBe(false);
		expect(outcome.detail).toContain("expected MERGED");
	});

	test("refuses completion when PR has no required status checks recorded", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const noChecksGh: GhPrRunner = () => ({
			ok: true,
			out: {
				pr: {
					state: "MERGED",
					baseRefName: "main",
				},
				requiredChecks: [],
			},
			err: "",
		});
		const outcome = verifyMergeConfirmation(repo, head, "refs/heads/execution/omp-212", "refs/heads/main", noChecksGh);
		expect(outcome.confirmed).toBe(false);
		expect(outcome.detail).toContain("no required status checks");
	});

	test("refuses completion when optional check passes but required check fails/pending/cancelled/timed_out/skipped/null", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const failedCheckGh: GhPrRunner = () => ({
			ok: true,
			out: {
				pr: {
					state: "MERGED",
					baseRefName: "main",
				},
				requiredChecks: [
					{ name: "lint-required", state: "FAILURE", bucket: "fail", conclusion: "FAILURE" },
				],
			},
			err: "",
		});
		const outcome = verifyMergeConfirmation(repo, head, "refs/heads/execution/omp-212", "refs/heads/main", failedCheckGh);
		expect(outcome.confirmed).toBe(false);
		expect(outcome.detail).toContain('required check "lint-required" is not passing');
	});

	test("refuses completion when candidate is pushed to execution branch but not merged into origin/main", () => {
		const repo = makeRepo();
		const remote = path.join(tempRoot, `remote-${repoSeq}.git`);
		Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
		git(repo, "remote", "add", "origin", remote);
		git(repo, "push", "-q", "origin", "HEAD:refs/heads/main");

		// Create candidate on execution branch
		fs.writeFileSync(path.join(repo, "feat.txt"), "feat\n");
		git(repo, "add", "--", "feat.txt");
		git(repo, "commit", "-q", "-m", "feat");
		const candidateSha = git(repo, "rev-parse", "HEAD");
		git(repo, "push", "-q", "origin", `HEAD:refs/heads/execution/omp-212`);

		// PR check passes but origin/main has not merged the candidate commit
		const outcome = verifyMergeConfirmation(repo, candidateSha, "refs/heads/execution/omp-212", "refs/heads/main", mockGhPassing);
		expect(outcome.confirmed).toBe(false);
		expect(outcome.detail).toContain(`candidate commit ${candidateSha} is not contained in origin/main`);
	});

	test("confirms completion when PR is merged with required checks and origin/main contains candidate commit", () => {
		const repo = makeRepo();
		const remote = path.join(tempRoot, `remote-${repoSeq}.git`);
		Bun.spawnSync(["git", "init", "--bare", "-q", remote]);
		git(repo, "remote", "add", "origin", remote);

		// Create candidate commit
		fs.writeFileSync(path.join(repo, "feat.txt"), "feat\n");
		git(repo, "add", "--", "feat.txt");
		git(repo, "commit", "-q", "-m", "feat");
		const candidateSha = git(repo, "rev-parse", "HEAD");

		// Push candidate to execution branch AND merge to main on remote
		git(repo, "push", "-q", "origin", `HEAD:refs/heads/execution/omp-212`);
		git(repo, "push", "-q", "origin", `HEAD:refs/heads/main`);

		const outcome = verifyMergeConfirmation(repo, candidateSha, "refs/heads/execution/omp-212", "refs/heads/main", mockGhPassing);
		expect(outcome.confirmed).toBe(true);
		expect(outcome.detail).toContain(`PR merged with 1 required check(s) passing and origin/main contains candidate ${candidateSha}`);
	});

	test("refuses completion when origin fetch fails", () => {
		const repo = makeRepo();
		// No origin remote configured
		const head = git(repo, "rev-parse", "HEAD");
		const outcome = verifyMergeConfirmation(repo, head, "refs/heads/execution/omp-212", "refs/heads/main", mockGhPassing);
		expect(outcome.confirmed).toBe(false);
		expect(outcome.detail).toContain("fetch refs/heads/main failed");
	});
});
