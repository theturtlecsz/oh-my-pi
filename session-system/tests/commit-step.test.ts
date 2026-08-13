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
import { commitSessionWork, findSecrets, parsePorcelain } from "../extensions/linear-now";

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

describe("HOME-118 /done commit step", () => {
	test("parsePorcelain keeps rename new path, drops original", () => {
		const z = "M  a.txt\0?? b.txt\0R  new.txt\0old.txt\0";
		expect(parsePorcelain(z)).toEqual(["a.txt", "b.txt", "new.txt"]);
	});

	test("findSecrets fires on added lines only", () => {
		expect(findSecrets("+const k = 'lin_api_abc123';")).toEqual(["linear-key"]);
		expect(findSecrets("-const k = 'lin_api_abc123';\n+const k = env.KEY;")).toEqual([]);
		expect(findSecrets("+++ b/file.ts\n+const x = 1;")).toEqual([]);
	});

	test("commits exact paths, excludes packages/*, reports not-pushed (no remote)", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "a.txt"), "session work\n");
		fs.mkdirSync(path.join(repo, "packages"), { recursive: true });
		fs.writeFileSync(path.join(repo, "packages", "x.ts"), "chris's lane\n");
		const ui = makeUi(true);
		const notice = await commitSessionWork(ui, repo, "HOME-1");
		expect(notice).toBe("[linear] /done committed 1 file(s) on HOME-1 (not pushed — no remote)");
		expect(git(repo, "log", "-1", "--format=%s")).toBe("session close: HOME-1");
		expect(git(repo, "show", "--name-only", "--format=", "HEAD")).toBe("a.txt");
		expect(git(repo, "status", "--porcelain")).toBe("?? packages/"); // untracked dir collapses; still excluded, still dirty
		expect(ui.confirms[0].body).toContain("left alone: 1 file(s) under packages/ (yours)");
	});

	test("secret in staged diff aborts commit and restores the tree", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "config.ts"), "const key = 'lin_api_FAKE123';\n");
		const ui = makeUi(true);
		const notice = await commitSessionWork(ui, repo, "HOME-1");
		expect(notice).toBeNull();
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1"); // seed only
		expect(git(repo, "diff", "--cached", "--name-only")).toBe(""); // nothing left staged
		expect(ui.notices.some(n => n.startsWith("error: commit refused — possible secret") && n.includes("linear-key"))).toBe(true);
	});

	test("owner decline leaves repo untouched", async () => {
		const repo = makeRepo();
		fs.writeFileSync(path.join(repo, "a.txt"), "work\n");
		const ui = makeUi(false);
		const notice = await commitSessionWork(ui, repo, "HOME-1");
		expect(notice).toBeNull();
		expect(git(repo, "rev-list", "--count", "HEAD")).toBe("1");
		expect(git(repo, "diff", "--cached", "--name-only")).toBe("");
	});
});
