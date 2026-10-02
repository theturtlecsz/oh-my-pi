import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";

// The test_ts_native "Fetch advisor trial case history" step. The `run: |`
// body is indented 14 spaces in .github/workflows/ci.yml, same as the
// installed qualification mode step.
async function loadStepScript(): Promise<string> {
	const ciYml = await Bun.file(path.join(import.meta.dir, "..", ".github", "workflows", "ci.yml")).text();
	const match = ciYml.match(/name: Fetch advisor trial case history[\s\S]*?\n {11}run: \|\n([\s\S]*?)\n {9}- uses:/);
	const body = match?.[1];
	if (!body) {
		throw new Error("Failed to extract Fetch advisor trial case history script from .github/workflows/ci.yml");
	}
	return body
		.split("\n")
		.map(line => line.replace(/^ {14}/, ""))
		.join("\n");
}

interface Fixture {
	base: string;
	full: string;
	clone: string;
	tip: string;
	caseSha: string;
}

async function makeFixture(): Promise<Fixture> {
	const base = await fs.mkdtemp(path.join(os.tmpdir(), "ci-advisor-trial-history-"));
	const origin = path.join(base, "origin.git");
	const full = path.join(base, "full");
	const clone = path.join(base, "clone");
	const originUrl = `file://${origin}`;

	await $`git init --bare -b main ${origin}`.quiet();
	await $`git -C ${origin} config uploadpack.allowReachableSHA1InWant true`.quiet();
	await $`git init -b main ${full}`.quiet();
	await $`git -C ${full} config user.name Test`.quiet();
	await $`git -C ${full} config user.email test@example.com`.quiet();

	await Bun.write(path.join(full, "scope.py"), "base\n");
	await $`git -C ${full} add scope.py`.quiet();
	await $`git -C ${full} commit -m base`.quiet();

	await Bun.write(path.join(full, "scope.py"), "case-body\n");
	await $`git -C ${full} add scope.py`.quiet();
	await $`git -C ${full} commit -m case`.quiet();
	const caseSha = (await $`git -C ${full} rev-parse HEAD`.text()).trim();

	await Bun.write(path.join(full, "later.txt"), "later\n");
	await $`git -C ${full} add later.txt`.quiet();
	await $`git -C ${full} commit -m later`.quiet();
	const tip = (await $`git -C ${full} rev-parse HEAD`.text()).trim();

	await $`git -C ${full} remote add origin ${originUrl}`.quiet();
	await $`git -C ${full} push origin main`.quiet();
	await $`git clone --depth 1 --no-tags ${originUrl} ${clone}`.quiet();

	const missing = await $`git -C ${clone} cat-file -t ${caseSha}`.nothrow().quiet();
	if (missing.exitCode === 0) {
		throw new Error("fixture: depth-1 clone already contains the case commit");
	}

	return { base, full, clone, tip, caseSha };
}

interface StepRunResult {
	exitCode: number;
	stdout: string;
	stderr: string;
}

async function runStep(cwd: string, githubSha: string): Promise<StepRunResult> {
	const script = await loadStepScript();
	const proc = Bun.spawn(["bash", "-c", script], {
		cwd,
		env: {
			...process.env,
			GITHUB_SHA: githubSha,
			GIT_TERMINAL_PROMPT: "0",
		},
		stdout: "pipe",
		stderr: "pipe",
	});
	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	return { exitCode, stdout, stderr };
}

describe("CI advisor trial case history step", () => {
	test("unshallows a depth-1 checkout so the case commit is an ancestor of HEAD", async () => {
		const fixture = await makeFixture();
		try {
			const result = await runStep(fixture.clone, fixture.tip);
			expect(result.exitCode, `${result.stdout}\n${result.stderr}`).toBe(0);

			const shallow = (await $`git -C ${fixture.clone} rev-parse --is-shallow-repository`.text()).trim();
			expect(shallow).toBe("false");

			const mergeBase = (await $`git -C ${fixture.clone} merge-base ${fixture.caseSha} HEAD`.text()).trim();
			expect(mergeBase).toBe(fixture.caseSha);

			const diff = (
				await $`git -C ${fixture.clone} diff --name-only ${fixture.caseSha}^ ${fixture.caseSha}`.text()
			).trim();
			expect(diff).toBe("scope.py");

			const blob = (await $`git -C ${fixture.clone} rev-parse ${fixture.caseSha}:scope.py`.text()).trim();
			const body = await $`git -C ${fixture.clone} cat-file -p ${blob}`.text();
			expect(body).toBe("case-body\n");

			const wt = path.join(fixture.base, "wt");
			await $`git -C ${fixture.clone} worktree add --detach ${wt} ${fixture.caseSha}`.quiet();
			expect(await Bun.file(path.join(wt, "scope.py")).text()).toBe("case-body\n");
		} finally {
			await fs.rm(fixture.base, { recursive: true, force: true });
		}
	}, 30000);

	test("does not fetch when the checkout is already complete", async () => {
		const fixture = await makeFixture();
		try {
			await $`git -C ${fixture.full} remote set-url origin ${path.join(fixture.base, "missing.git")}`.quiet();
			const result = await runStep(fixture.full, fixture.tip);
			expect(result.exitCode, `${result.stdout}\n${result.stderr}`).toBe(0);
			const mergeBase = (await $`git -C ${fixture.full} merge-base ${fixture.caseSha} HEAD`.text()).trim();
			expect(mergeBase).toBe(fixture.caseSha);
		} finally {
			await fs.rm(fixture.base, { recursive: true, force: true });
		}
	}, 30000);

	test("fails the step when a shallow checkout cannot unshallow", async () => {
		const fixture = await makeFixture();
		try {
			await $`git -C ${fixture.clone} remote set-url origin ${path.join(fixture.base, "missing.git")}`.quiet();
			const result = await runStep(fixture.clone, fixture.tip);
			expect(result.exitCode).not.toBe(0);
			const missing = await $`git -C ${fixture.clone} cat-file -t ${fixture.caseSha}`.nothrow().quiet();
			expect(missing.exitCode).not.toBe(0);
		} finally {
			await fs.rm(fixture.base, { recursive: true, force: true });
		}
	}, 30000);
});
