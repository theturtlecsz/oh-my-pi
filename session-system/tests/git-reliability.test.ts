/** Regression contracts for execution freeze and delivery. All Git operations
 * use temporary real repositories; only the GitHub service is simulated. */
import { afterAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import {
	freezeCandidateCommit,
	runGit,
	verifyMergeConfirmation,
	type FreezeUi,
	type GhPrRunner,
} from "../extensions/workflow/git";

const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ss-git-reliability-"));
afterAll(() => fs.rm(tempRoot, { recursive: true, force: true }));

function git(root: string, ...args: string[]): string {
	const result = runGit(root, args);
	if (!result.ok) throw new Error(`fixture git ${args.join(" ")}: ${result.err}`);
	return result.out;
}

async function makeRepo(parent = tempRoot): Promise<string> {
	const root = await fs.mkdtemp(path.join(parent, "repo-"));
	git(root, "init", "--initial-branch=main", "-q");
	git(root, "config", "user.email", "fixture@example.invalid");
	git(root, "config", "user.name", "Local Fixture");
	git(root, "config", "core.hooksPath", path.join(root, ".git", "no-hooks"));
	await Bun.write(path.join(root, "seed.txt"), "baseline\n");
	git(root, "add", "--", "seed.txt");
	git(root, "commit", "-qm", "baseline");
	return root;
}

const ui: FreezeUi = { confirm: async () => true, notify: () => {} };

describe("execution freeze preserves deletion and credential-scan contracts", () => {
	test("tracked deletion creates a clean candidate instead of refusing ENOENT", async () => {
		const root = await makeRepo();
		await fs.rm(path.join(root, "seed.txt"));
		const result = await freezeCandidateCommit(ui, root, "OMP-1", "deletion", [], {
			mode: "execution",
			sealedPaths: ["seed.txt"],
		});
		if ("refused" in result) throw new Error(result.reason);
		expect(result.paths).toEqual(["seed.txt"]);
		expect(git(root, "diff-tree", "--name-status", "--no-commit-id", "-r", result.commitSha)).toBe("D\tseed.txt");
		expect(git(root, "status", "--porcelain")).toBe("");
	});

	test("deleting a tracked path does not bypass scanning another file's added secret", async () => {
		const root = await makeRepo();
		const baseline = git(root, "rev-parse", "HEAD");
		await fs.rm(path.join(root, "seed.txt"));
		await Bun.write(path.join(root, "added.txt"), `token=${"lin_" + "api_0123456789abcdef"}\n`);
		const result = await freezeCandidateCommit(ui, root, "OMP-1", "secret", [], {
			mode: "execution",
			sealedPaths: ["seed.txt", "added.txt"],
		});
		expect("refused" in result && result.reason).toContain("secret detected");
		expect(git(root, "rev-parse", "HEAD")).toBe(baseline);
	});

	test("a binary replacement is still refused before it can become a candidate", async () => {
		const root = await makeRepo();
		const baseline = git(root, "rev-parse", "HEAD");
		await Bun.write(path.join(root, "seed.txt"), new Uint8Array([0, 1, 2]));
		const result = await freezeCandidateCommit(ui, root, "OMP-1", "binary", [], {
			mode: "execution",
			sealedPaths: ["seed.txt"],
		});
		expect("refused" in result && result.reason).toContain("binary file");
		expect(git(root, "rev-parse", "HEAD")).toBe(baseline);
		expect(git(root, "diff", "--cached", "--name-only")).toBe("");
	});

	test("a dangling replacement symlink is unreadable, not a tracked deletion", async () => {
		const root = await makeRepo();
		const baseline = git(root, "rev-parse", "HEAD");
		await fs.rm(path.join(root, "seed.txt"));
		await fs.symlink("missing-target", path.join(root, "seed.txt"));
		const result = await freezeCandidateCommit(ui, root, "OMP-1", "symlink", [], {
			mode: "execution",
			sealedPaths: ["seed.txt"],
		});
		expect("refused" in result && result.reason).toContain("unreadable file for credential scan");
		expect(git(root, "rev-parse", "HEAD")).toBe(baseline);
	});
});

interface MergeFixture {
	root: string;
	remote: string;
	branch: string;
	baseline: string;
	candidate: string;
	shimDir: string;
	query: GhPrRunner;
}

async function makeMergeFixture(): Promise<MergeFixture> {
	const container = await fs.mkdtemp(path.join(tempRoot, "merge-"));
	const root = await makeRepo(container);
	const remote = path.join(container, "remote.git");
	await fs.mkdir(remote);
	git(remote, "init", "--bare", "--initial-branch=main", "-q");
	git(remote, "config", "user.email", "fixture@example.invalid");
	git(remote, "config", "user.name", "Local Fixture");
	git(root, "remote", "add", "origin", remote);
	git(root, "push", "-q", "origin", "main");
	const baseline = git(root, "rev-parse", "HEAD");
	const branch = "execution/reliability";
	git(root, "checkout", "-qb", branch);
	await Bun.write(path.join(root, "audited.txt"), "audited candidate\n");
	git(root, "add", "--", "audited.txt");
	git(root, "commit", "-qm", "audited candidate");
	const candidate = git(root, "rev-parse", "HEAD");
	git(root, "push", "-q", "origin", branch);
	await Bun.write(path.join(root, ".git", "merge-fixture.json"), JSON.stringify({ remote }));
	const shimDir = path.join(container, "bin");
	const fixturePath = path.join(import.meta.dir, "fixtures", "gh-merge-fixture.ts");
	const quote = (value: string): string => `'${value.replaceAll("'", "'\\''")}'`;
	await Bun.write(path.join(shimDir, "gh"), `#!/bin/sh\nexec ${quote(process.execPath)} ${quote(fixturePath)} "$@"\n`);
	await fs.chmod(path.join(shimDir, "gh"), 0o755);
	const query: GhPrRunner = () => {
		const currentHead = git(remote, "rev-parse", `refs/heads/${branch}`);
		const merged = runGit(remote, ["merge-base", "--is-ancestor", currentHead, "refs/heads/main"]).ok;
		return {
			ok: true,
			err: "",
			out: {
				pr: { state: merged ? "MERGED" : "OPEN", baseRefName: "main", headRefName: branch, headRefOid: currentHead, mergeStateStatus: "CLEAN" },
				requiredChecks: [{ name: "fixture-check", state: "SUCCESS", bucket: "pass" }],
			},
		};
	};
	return { root, remote, branch, baseline, candidate, shimDir, query };
}

function mergeInChild(fixture: MergeFixture, expectedHeadSha = fixture.candidate): { ok: boolean; detail: string } {
	const modulePath = path.join(import.meta.dir, "../extensions/workflow/git.ts");
	const script = `import { mergePullRequest } from ${JSON.stringify(modulePath)};
process.stdout.write(JSON.stringify(mergePullRequest(${JSON.stringify(fixture.root)}, ${JSON.stringify(fixture.branch)}, ${JSON.stringify(expectedHeadSha)})));`;
	const result = Bun.spawnSync([process.execPath, "-e", script], {
		cwd: fixture.root,
		env: { ...process.env, PATH: `${fixture.shimDir}${path.delimiter}${process.env.PATH ?? ""}` },
	});
	if (result.exitCode !== 0) throw new Error(result.stderr.toString());
	return JSON.parse(result.stdout.toString());
}

describe("delivery binds the merge mutation to the audited head", () => {
	test("an unchanged audited head merges and verifies against the fetched default branch", async () => {
		const fixture = await makeMergeFixture();
		const merged = mergeInChild(fixture);
		expect(merged.ok).toBe(true);
		const confirmation = verifyMergeConfirmation(fixture.root, fixture.candidate, fixture.branch, "refs/heads/main", fixture.query);
		expect(confirmation.confirmed).toBe(true);
		expect(runGit(fixture.remote, ["merge-base", "--is-ancestor", fixture.candidate, "main"]).ok).toBe(true);
	});

	test("a branch update after precheck is rejected before unaudited bytes land on main", async () => {
		const fixture = await makeMergeFixture();
		const precheck = verifyMergeConfirmation(fixture.root, fixture.candidate, fixture.branch, "refs/heads/main", fixture.query);
		expect(precheck.confirmed).toBe(false);
		expect(precheck.detail).toContain("expected MERGED");
		await Bun.write(path.join(fixture.root, "unaudited.txt"), "post-audit change\n");
		git(fixture.root, "add", "--", "unaudited.txt");
		git(fixture.root, "commit", "-qm", "unaudited change");
		const unaudited = git(fixture.root, "rev-parse", "HEAD");
		git(fixture.root, "push", "-q", "origin", fixture.branch);
		const merged = mergeInChild(fixture);
		expect(merged.ok).toBe(false);
		expect(merged.detail).toContain("no longer matches expected commit");
		expect(git(fixture.remote, "rev-parse", "main")).toBe(fixture.baseline);
		expect(runGit(fixture.remote, ["merge-base", "--is-ancestor", unaudited, "main"]).ok).toBe(false);
	});

	test("a short head identifier refuses delivery before invoking the merge command", async () => {
		const fixture = await makeMergeFixture();
		const merged = mergeInChild(fixture, fixture.candidate.slice(0, 12));
		expect(merged.ok).toBe(false);
		expect(merged.detail).toContain("full commit SHA");
		expect(git(fixture.remote, "rev-parse", "main")).toBe(fixture.baseline);
	});
});
