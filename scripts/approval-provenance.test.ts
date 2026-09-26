import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import { APPROVAL_PATH, checkApprovalProvenance, UnknownRevisionError } from "./approval-provenance.ts";

const SCRIPT = path.join(import.meta.dir, "approval-provenance.ts");

interface Author {
	name: string;
	email: string;
}

const OWNER: Author = { name: "flood-owner", email: "owner@flood" };
const AUTO: Author = { name: "zimmermanc", email: "auto@flood" };
const HUMAN: Author = { name: "someone-else", email: "human@example" };

const dirs: string[] = [];

async function makeRepo(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-approval-prov-"));
	dirs.push(dir);
	const init = await run(dir, ["init", "-b", "main"]);
	if (init.exitCode !== 0) throw new Error(init.err);
	return dir;
}

async function run(
	dir: string,
	args: string[],
	author: Author = HUMAN,
): Promise<{ exitCode: number; out: string; err: string }> {
	const proc = await $`git ${args}`
		.cwd(dir)
		.quiet()
		.nothrow()
		.env({
			...process.env,
			GIT_CONFIG_GLOBAL: "/dev/null",
			GIT_CONFIG_SYSTEM: "/dev/null",
			GIT_AUTHOR_NAME: author.name,
			GIT_AUTHOR_EMAIL: author.email,
			GIT_COMMITTER_NAME: author.name,
			GIT_COMMITTER_EMAIL: author.email,
		});
	return { exitCode: proc.exitCode, out: proc.text(), err: proc.stderr.toString() };
}

async function ok(dir: string, args: string[], author?: Author): Promise<string> {
	const result = await run(dir, args, author);
	if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${result.err}`);
	return result.out;
}

async function headSha(dir: string): Promise<string> {
	return (await ok(dir, ["rev-parse", "HEAD"])).trim();
}

async function writeApproval(dir: string, content: string): Promise<void> {
	await Bun.write(path.join(dir, APPROVAL_PATH), content);
}

/** Stage everything and commit, returning the new commit SHA. */
async function commit(dir: string, subject: string, author?: Author): Promise<string> {
	await ok(dir, ["add", "-A"]);
	await ok(dir, ["commit", "-m", subject], author);
	return headSha(dir);
}

async function cli(dir: string, args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
	const proc = await $`bun ${SCRIPT} ${args}`.cwd(dir).quiet().nothrow();
	return { exitCode: proc.exitCode, stdout: proc.text(), stderr: proc.stderr.toString() };
}

afterEach(async () => {
	while (dirs.length) await fs.rm(dirs.pop() as string, { recursive: true, force: true });
});

describe("checkApprovalProvenance", () => {
	test("flags an auto-commit by another author that writes approval.json", async () => {
		const dir = await makeRepo();
		await writeApproval(dir, "attestation A\n");
		const base = await commit(dir, "base", HUMAN);
		await writeApproval(dir, "attestation B\n");
		const offender = await commit(dir, "OMP-266-s04: flood auto-commit (attempt 2)", AUTO);

		const violations = await checkApprovalProvenance({ cwd: dir, base });
		expect(violations).toEqual([
			{ sha: offender, author: "zimmermanc", subject: "OMP-266-s04: flood auto-commit (attempt 2)" },
		]);

		const result = await cli(dir, ["--base", base]);
		expect(result.exitCode).toBe(1);
		expect(result.stdout).toContain(offender.slice(0, 12));
	});

	test("ignores approval commits: flood-owner with either owner marker", async () => {
		const dir = await makeRepo();
		await writeApproval(dir, "attestation A\n");
		const base = await commit(dir, "base", HUMAN);

		await writeApproval(dir, "attestation B\n");
		await commit(dir, "chore(contract): owner approval of v1 digest D (OMP-295) — owner step by flood", OWNER);
		await writeApproval(dir, "attestation C\n");
		await commit(dir, "flood rebase_repair: carry owner approval forward", OWNER);

		expect(await checkApprovalProvenance({ cwd: dir, base })).toEqual([]);
		const result = await cli(dir, ["--base", base]);
		expect(result.exitCode).toBe(0);
		expect(result.stdout).toBe("");
	});

	test("flags a marker-less flood-owner approval and a marker subject from another author", async () => {
		const dir = await makeRepo();
		await writeApproval(dir, "attestation A\n");
		const base = await commit(dir, "base", HUMAN);

		await writeApproval(dir, "attestation B\n");
		const ownerDrift = await commit(dir, "tweak approval", OWNER);
		await writeApproval(dir, "attestation C\n");
		const impostor = await commit(dir, "owner step by flood", AUTO);

		const violations = await checkApprovalProvenance({ cwd: dir, base });
		// git log is newest-first, so the later impostor commit is reported first.
		expect(violations.map(v => v.sha)).toEqual([impostor, ownerDrift]);
		expect(violations.map(v => v.author)).toEqual(["zimmermanc", "flood-owner"]);
	});

	test("flags a merge that resolves approval.json to neither parent's content", async () => {
		const dir = await makeRepo();
		await writeApproval(dir, "attestation A\n");
		const base = await commit(dir, "base", HUMAN);

		await ok(dir, ["checkout", "-b", "side"]);
		await writeApproval(dir, "attestation SIDE\n");
		await commit(dir, "owner step by flood: side attestation", OWNER);
		await ok(dir, ["checkout", "main"]);
		await writeApproval(dir, "attestation MAIN\n");
		await commit(dir, "owner step by flood: main attestation", OWNER);

		const merge = await run(dir, ["merge", "--no-ff", "-m", "merge side", "side"], HUMAN);
		expect(merge.exitCode).not.toBe(0);
		await writeApproval(dir, "attestation RESOLVED\n");
		await ok(dir, ["add", "-A"]);
		await ok(dir, ["commit", "--no-edit"], HUMAN);
		const mergeSha = await headSha(dir);

		const violations = await checkApprovalProvenance({ cwd: dir, base });
		expect(violations.map(v => v.sha)).toEqual([mergeSha]);
		expect(violations[0].subject).toBe("merge side");
	});

	test("ignores merges whose approval.json blob matches a parent", async () => {
		const dir = await makeRepo();
		await writeApproval(dir, "attestation A\n");
		const base = await commit(dir, "base", HUMAN);

		// The only side change is an owner-marked approval commit; the merge takes
		// that blob verbatim, matching a parent.
		await ok(dir, ["checkout", "-b", "side"]);
		await writeApproval(dir, "attestation SIDE\n");
		await commit(dir, "owner step by flood: side attestation", OWNER);
		await ok(dir, ["checkout", "main"]);
		await Bun.write(path.join(dir, "README.md"), "main unrelated\n");
		await commit(dir, "docs: main unrelated", HUMAN);
		await ok(dir, ["merge", "--no-ff", "-m", "merge side", "side"], HUMAN);

		expect(await checkApprovalProvenance({ cwd: dir, base })).toEqual([]);
	});

	test("reports nothing when no commit changes approval.json", async () => {
		const dir = await makeRepo();
		await writeApproval(dir, "attestation A\n");
		const base = await commit(dir, "base", HUMAN);
		await Bun.write(path.join(dir, "README.md"), "hello\n");
		await commit(dir, "docs: add readme", HUMAN);

		expect(await checkApprovalProvenance({ cwd: dir, base })).toEqual([]);
		const result = await cli(dir, ["--base", base, "--head", "HEAD"]);
		expect(result.exitCode).toBe(0);
		expect(result.stdout).toBe("");
	});

	test("flags a root commit that adds approval.json (diff-tree --root)", async () => {
		const dir = await makeRepo();
		// An orphan base on an unrelated history gives `git log base..head` a
		// reachable root commit to inspect.
		await Bun.write(path.join(dir, "orphan.txt"), "orphan\n");
		await ok(dir, ["add", "-A"]);
		await ok(dir, ["commit", "-m", "orphan base"], HUMAN);
		const base = await headSha(dir);

		await ok(dir, ["checkout", "--orphan", "topic"]);
		await ok(dir, ["rm", "-rf", "--cached", "-q", "."]);
		await writeApproval(dir, "attestation ROOT\n");
		await Bun.write(path.join(dir, "root.txt"), "root\n");
		const root = await commit(dir, "OMP-266-s04: flood auto-commit (attempt 2)", AUTO);

		const parents = (await ok(dir, ["rev-list", "--parents", root])).trim();
		expect(parents.split(" ")).toHaveLength(1); // no parent: a real root commit

		const violations = await checkApprovalProvenance({ cwd: dir, base });
		expect(violations.map(v => v.sha)).toEqual([root]);
	});

	test("throws UnknownRevisionError for an unresolvable base or head, and the CLI exits 2", async () => {
		const dir = await makeRepo();
		await Bun.write(path.join(dir, "README.md"), "hello\n");
		await commit(dir, "base", HUMAN);
		const head = await headSha(dir);

		await expect(checkApprovalProvenance({ cwd: dir, base: "deadbeef00" })).rejects.toBeInstanceOf(
			UnknownRevisionError,
		);
		await expect(checkApprovalProvenance({ cwd: dir, base: head, head: "nope" })).rejects.toBeInstanceOf(
			UnknownRevisionError,
		);

		const unknown = await cli(dir, ["--base", "deadbeef00"]);
		expect(unknown.exitCode).toBe(2);
		const noBase = await cli(dir, []);
		expect(noBase.exitCode).toBe(2);
	});
});
