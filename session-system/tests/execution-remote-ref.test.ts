import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";
import {
	cleanupExecutionWorkspace,
	ensureExecutionWorkspace,
	executionRemoteRef,
	executionRemoteRefRefusal,
} from "../extensions/workflow/git";

describe("executionRemoteRef and executionRemoteRefRefusal (OMP-233)", () => {
	const grantId = "00000000-0000-7000-8000-000000000237";
	const canonical = executionRemoteRef("OMP-237", grantId);

	test("canonical remote ref format", () => {
		expect(canonical).toBe("refs/heads/execution/omp-237-00000000000070008000000000000237");
		expect(executionRemoteRef("omp-237", grantId)).toBe(canonical);
	});

	describe("refusal matrix", () => {
		test("canonical passes", () => {
			expect(executionRemoteRefRefusal(canonical, "OMP-237", grantId)).toBeUndefined();
			expect(executionRemoteRefRefusal(canonical, "omp-237", grantId)).toBeUndefined();
		});

		test("unqualified other-item ref refuses", () => {
			const otherRef = "refs/heads/execution/omp-238";
			const refusal = executionRemoteRefRefusal(otherRef, "OMP-237", grantId);
			expect(refusal).toBeDefined();
			expect(refusal).toContain(otherRef);
			expect(refusal).toContain(canonical);
		});

		test("unqualified same-item ref refuses", () => {
			const unqualifiedRef = "refs/heads/execution/omp-237";
			const refusal = executionRemoteRefRefusal(unqualifiedRef, "OMP-237", grantId);
			expect(refusal).toBeDefined();
			expect(refusal).toContain(unqualifiedRef);
			expect(refusal).toContain(canonical);
		});

		test("prior grant's qualified ref refuses", () => {
			const priorGrantId = "00000000-0000-7000-8000-000000000001";
			const priorRef = executionRemoteRef("OMP-237", priorGrantId);
			const refusal = executionRemoteRefRefusal(priorRef, "OMP-237", grantId);
			expect(refusal).toBeDefined();
			expect(refusal).toContain(priorRef);
			expect(refusal).toContain(canonical);
		});

		test("refs/heads/main refuses", () => {
			const mainRef = "refs/heads/main";
			const refusal = executionRemoteRefRefusal(mainRef, "OMP-237", grantId);
			expect(refusal).toBeDefined();
			expect(refusal).toContain(mainRef);
			expect(refusal).toContain(canonical);
		});

		test("undefined ref refuses", () => {
			const refusal = executionRemoteRefRefusal(undefined, "OMP-237", grantId);
			expect(refusal).toBeDefined();
			expect(refusal).toContain("undefined");
			expect(refusal).toContain(canonical);
		});

		test("undefined anchor key refuses", () => {
			const refusalWithRecorded = executionRemoteRefRefusal(canonical, undefined, grantId);
			expect(refusalWithRecorded).toBeDefined();
			expect(refusalWithRecorded).toContain("missing anchor key");
			expect(refusalWithRecorded).toContain(canonical);

			const refusalWithUndefinedRecorded = executionRemoteRefRefusal(undefined, undefined, grantId);
			expect(refusalWithUndefinedRecorded).toBeDefined();
			expect(refusalWithUndefinedRecorded).toContain("missing anchor key");
		});
	});
});

describe("ensureExecutionWorkspace symbolic-ref HEAD and grant isolation (OMP-233)", () => {
	const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-exec-remote-ref-"));
	afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

	let repoSeq = 0;
	const makeRepo = (branch = "main"): string => {
		const repo = path.join(tempRoot, `repo-${repoSeq++}`);
		fs.mkdirSync(repo, { recursive: true });
		for (const args of [
			["init", `--initial-branch=${branch}`, "-q"],
			["config", "user.email", "test@example.com"],
			["config", "user.name", "Test"],
		]) {
			Bun.spawnSync(["git", ...args], { cwd: repo });
		}
		fs.writeFileSync(path.join(repo, "seed.txt"), "seed\n");
		Bun.spawnSync(["git", "add", "--", "seed.txt"], { cwd: repo });
		Bun.spawnSync(["git", "commit", "-q", "-m", "seed"], { cwd: repo });
		return repo;
	};

	const git = (repo: string, ...args: string[]): string =>
		Bun.spawnSync(["git", ...args], { cwd: repo }).stdout.toString().trim();

	test("ensureExecutionWorkspace worktree's git symbolic-ref HEAD equals executionRemoteRef(key, grantId)", async () => {
		const repo = makeRepo();
		const baseline = git(repo, "rev-parse", "HEAD");
		const worktreesRoot = path.join(tempRoot, `managed-${repoSeq++}`);
		const grantId = "00000000-0000-7000-8000-000000000233";
		const key = "OMP-233";

		const ws = await ensureExecutionWorkspace(repo, key, grantId, baseline, {}, worktreesRoot);
		try {
			const headRef = git(ws.path, "symbolic-ref", "HEAD");
			expect(headRef).toBe(executionRemoteRef(key, grantId));
		} finally {
			await cleanupExecutionWorkspace(ws);
		}
	});

	test("two grants of one key get distinct refs", async () => {
		const repo = makeRepo();
		const baseline = git(repo, "rev-parse", "HEAD");
		const worktreesRoot = path.join(tempRoot, `managed-${repoSeq++}`);
		const key = "OMP-233";
		const grantId1 = "00000000-0000-7000-8000-000000000001";
		const grantId2 = "00000000-0000-7000-8000-000000000002";

		const ws1 = await ensureExecutionWorkspace(repo, key, grantId1, baseline, {}, worktreesRoot);
		const ws2 = await ensureExecutionWorkspace(repo, key, grantId2, baseline, {}, worktreesRoot);
		try {
			const ref1 = git(ws1.path, "symbolic-ref", "HEAD");
			const ref2 = git(ws2.path, "symbolic-ref", "HEAD");

			expect(ref1).toBe(executionRemoteRef(key, grantId1));
			expect(ref2).toBe(executionRemoteRef(key, grantId2));
			expect(ref1).not.toBe(ref2);
		} finally {
			await cleanupExecutionWorkspace(ws1);
			await cleanupExecutionWorkspace(ws2);
		}
	});
});
