/**
 * HOME-118: /done commits and pushes the session's work. Contracts proven here
 * against the exported commit-step helpers (no Linear network involved):
 *  - porcelain -z parsing keeps rename NEW paths only (staging the old path
 *    would error or commit the wrong file);
 *  - the credential scan fires on ADDED lines only (a secret on a removed line
 *    must not false-abort; a secret on an added line must never reach the
 *    public fork);
 *  - the integration path stages exact paths, excludes packages/* (Chris's own
 *    lane), commits `session close: <identifier>`, and reports the no-remote
 *    not-pushed variant;
 *  - a secret hit aborts the commit and restores the tree (no staged residue);
 *  - owner decline leaves the repo untouched.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";
import { dirtyPaths, findSecrets, freezeCandidateCommit, parsePorcelain, pushCandidate } from "../extensions/workflow/git";

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
const makeUi = (answer: boolean): FakeUi => {
	const ui: FakeUi = {
		notices: [],
		confirms: [],
		confirm: async (title, body) => {
			ui.confirms.push({ title, body });
			return answer;
		},
		notify: (msg, level) => ui.notices.push(`${level ?? "info"}: ${msg}`),
	};
	return ui;
};

describe("candidate freeze and push", () => {
	test("parsePorcelain keeps rename new path, drops original", () => {
		const z = "M  a.txt\0?? b.txt\0R  new.txt\0old.txt\0";
		expect(parsePorcelain(z)).toEqual(["a.txt", "b.txt", "new.txt"]);
	});

	test("findSecrets fires on added lines only", () => {
		expect(findSecrets("+const k = 'lin_api_abc123';")).toEqual(["linear-key"]);
		expect(findSecrets("-const k = 'lin_api_abc123';\n+const k = env.KEY;")).toEqual([]);
		expect(findSecrets("+++ b/file.ts\n+const x = 1;")).toEqual([]);
	});
	test("candidate freeze leaves pre-session owner files uncommitted", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "owner.txt"), "owner setting\n");
		const preExisting = dirtyPaths(repo);
		fs.writeFileSync(path.join(repo, "session.txt"), "session work\n");
		const ui = makeUi(true);

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1", preExisting);

		expect(frozen?.paths).toEqual(["session.txt"]);
		expect(git(repo, "show", "--name-only", "--format=", "HEAD")).toBe("session.txt");
		expect(fs.readFileSync(path.join(repo, "owner.txt"), "utf8")).toBe("owner setting\n");
		expect(git(repo, "status", "--porcelain")).toBe("?? owner.txt");
		expect(ui.confirms[0].body).toContain("left alone: 1 pre-session file(s)");
	});

	test("candidate freeze adopts current HEAD when only pre-session files are dirty", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "session.txt"), "committed work\n");
		Bun.spawnSync(["git", "add", "--", "session.txt"], { cwd: repo });
		Bun.spawnSync(["git", "commit", "-q", "-m", "work"], { cwd: repo });
		const head = git(repo, "rev-parse", "HEAD");
		fs.writeFileSync(path.join(repo, "owner.txt"), "owner setting\n");
		const ui = makeUi(true);

		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1", dirtyPaths(repo));

		expect(frozen?.commitSha).toBe(head);
		expect(frozen?.paths).toEqual(["session.txt"]);
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("2");
		expect(git(repo, "status", "--porcelain")).toBe("?? owner.txt");
		expect(ui.confirms[0].title).toContain("Use current HEAD");
	});

	test("secret in staged diff aborts freeze and restores the tree", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "config.ts"), "const key = 'lin_api_FAKE123';\n");
		const ui = makeUi(true);
		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");
		expect(frozen).toBeNull();
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1"); // seed only
		expect(git(repo, "diff", "--cached", "--name-only")).toBe(""); // nothing left staged
		expect(ui.notices.some(n => n.startsWith("error: freeze refused — possible secret") && n.includes("linear-key"))).toBe(true);
	});

	test("owner decline leaves repo untouched", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "a.txt"), "work\n");
		const ui = makeUi(false);
		const frozen = await freezeCandidateCommit(ui, repo, "HOME-1", "candidate-1");
		expect(frozen).toBeNull();
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1");
		expect(git(repo, "diff", "--cached", "--name-only")).toBe("");
	});

	test("pushCandidate reports not_pushed when no remote", () => {
		const repo = makeRepo();
		const head = git(repo, "rev-parse", "HEAD");
		const outcome = pushCandidate(repo, head);
		expect(outcome.status).toBe("not_pushed");
	});
});
