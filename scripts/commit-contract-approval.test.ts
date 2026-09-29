import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import { APPROVAL_PATH, checkApprovalProvenance } from "./approval-provenance.ts";
import { commitContractApproval, ContractApprovalError } from "./commit-contract-approval.ts";

interface Author {
	name: string;
	email: string;
}

const HUMAN: Author = { name: "someone-else", email: "human@example" };

const dirs: string[] = [];

async function makeRepo(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-contract-approval-"));
	dirs.push(dir);
	const init = await run(dir, ["init", "-b", "main"]);
	if (init.exitCode !== 0) throw new Error(init.err);
	await Bun.write(path.join(dir, APPROVAL_PATH), "attestation A\n");
	await Bun.write(path.join(dir, "packages/work-client/src/contract.ts"), "export const DIGEST = 'A';\n");
	await commitAll(dir, "base approval");
	return dir;
}

async function run(dir: string, args: string[], author: Author = HUMAN): Promise<{ exitCode: number; out: string; err: string }> {
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

async function commitAll(dir: string, subject: string, author?: Author): Promise<string> {
	await ok(dir, ["add", "-A"]);
	await ok(dir, ["commit", "-m", subject], author);
	return (await ok(dir, ["rev-parse", "HEAD"])).trim();
}

/** Run scripts/approval-provenance.ts against a temporary repo. */
async function provenanceCli(
	dir: string,
	args: string[],
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
	const script = path.join(import.meta.dir, "approval-provenance.ts");
	const proc = await $`bun ${script} ${args}`.cwd(dir).quiet().nothrow();
	return { exitCode: proc.exitCode, stdout: proc.text(), stderr: proc.stderr.toString() };
}

/** Call `fn` with no global or system git config, then restore the parent env. */
async function withoutGitConfig<T>(fn: () => Promise<T>): Promise<T> {
	const savedGlobal = process.env.GIT_CONFIG_GLOBAL;
	const savedSystem = process.env.GIT_CONFIG_SYSTEM;
	process.env.GIT_CONFIG_GLOBAL = "/dev/null";
	process.env.GIT_CONFIG_SYSTEM = "/dev/null";
	try {
		return await fn();
	} finally {
		if (savedGlobal === undefined) delete process.env.GIT_CONFIG_GLOBAL;
		else process.env.GIT_CONFIG_GLOBAL = savedGlobal;
		if (savedSystem === undefined) delete process.env.GIT_CONFIG_SYSTEM;
		else process.env.GIT_CONFIG_SYSTEM = savedSystem;
	}
}

afterEach(async () => {
	while (dirs.length) await fs.rm(dirs.pop() as string, { recursive: true, force: true });
});

describe("commitContractApproval", () => {
	test("commits the owner approval so approval-provenance accepts it and rejects any other author", async () => {
		const dir = await makeRepo();
		const base = (await ok(dir, ["rev-parse", "HEAD"])).trim();

		// Same change the helper makes, but by an unattributed author: this is the
		// exact failure mode the owner session produced for OMP-405.
		await Bun.write(path.join(dir, APPROVAL_PATH), "attestation B\n");
		const other = await commitAll(dir, "tweak approval");
		await ok(dir, ["reset", "--hard", base]);

		await Bun.write(path.join(dir, APPROVAL_PATH), "attestation B\n");
		// No git identity is configured. The helper has to supply author and committer itself.
		const sha = await withoutGitConfig(() => commitContractApproval({ cwd: dir, issue: "OMP-452" }));

		expect(sha).toBe((await ok(dir, ["rev-parse", "HEAD"])).trim());
		const log = (await ok(dir, ["log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%s"])).trim().split("\n");
		expect(log[0]).toBe("flood-owner <flood@localhost>");
		expect(log[1]).toBe("flood-owner <flood@localhost>");
		expect(log[2]).toContain("owner step by owner session");
		expect(log[2]).toContain("OMP-452");

		expect(await checkApprovalProvenance({ cwd: dir, base })).toEqual([]);
		const accepted = await provenanceCli(dir, ["--base", base]);
		expect(accepted.exitCode).toBe(0);
		expect(accepted.stdout).toBe("");

		const otherViolations = await checkApprovalProvenance({ cwd: dir, base: base, head: other });
		expect(otherViolations.map(v => v.subject)).toEqual(["tweak approval"]);
		const rejected = await provenanceCli(dir, ["--base", base, "--head", other]);
		expect(rejected.exitCode).toBe(1);
		expect(rejected.stdout).toContain("someone-else");
		expect(rejected.stdout).toContain("tweak approval");
	});

	test("verifies a supplied digest against the approval file and stages only the approval paths", async () => {
		const dir = await makeRepo();
		const base = (await ok(dir, ["rev-parse", "HEAD"])).trim();
		await Bun.write(
			path.join(dir, APPROVAL_PATH),
			`${JSON.stringify({ contract_sha256: "d".repeat(64), issue: "OMP-452" })}\n`,
		);
		await Bun.write(path.join(dir, "packages/work-client/src/contract.ts"), "export const DIGEST = 'D';\n");
		await Bun.write(path.join(dir, "unrelated.txt"), "leave me\n");

		const sha = await commitContractApproval({ cwd: dir, issue: "OMP-452", digest: "d".repeat(64) });
		const names = (await ok(dir, ["show", "--name-only", "--format=", sha])).trim().split("\n");
		expect(names).toEqual(["packages/work-client/src/contract.ts", APPROVAL_PATH]);

		// The digest the approval does not carry is refused, and the staged change
		// stays uncommitted for the owner to inspect.
		await Bun.write(
			path.join(dir, APPROVAL_PATH),
			`${JSON.stringify({ contract_sha256: "c".repeat(64), issue: "OMP-452" })}\n`,
		);
		await expect(commitContractApproval({ cwd: dir, issue: "OMP-452", digest: "e".repeat(64) })).rejects.toBeInstanceOf(
			ContractApprovalError,
		);
		expect((await ok(dir, ["rev-parse", "HEAD"])).trim()).toBe(sha);
		expect(await checkApprovalProvenance({ cwd: dir, base })).toEqual([]);
	});

	test("refuses a missing approval file without creating a commit", async () => {
		const dir = await makeRepo();
		const base = (await ok(dir, ["rev-parse", "HEAD"])).trim();
		await fs.rm(path.join(dir, APPROVAL_PATH));

		await expect(commitContractApproval({ cwd: dir, issue: "OMP-452" })).rejects.toBeInstanceOf(ContractApprovalError);
		expect((await ok(dir, ["rev-parse", "HEAD"])).trim()).toBe(base);
	});
});
