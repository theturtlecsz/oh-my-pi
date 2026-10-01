import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";

// The check job's Upstream guardrail step, extracted the same way as the
// release_metadata detect step in scripts/release.test.ts: the `run: |` body
// is indented 14 spaces in .github/workflows/ci.yml.
async function loadGuardrailScript(): Promise<string> {
	const ciYml = await Bun.file(path.join(import.meta.dir, "..", ".github", "workflows", "ci.yml")).text();
	const match = ciYml.match(
		/name: Upstream guardrail \(inventory consistency \/ full review\)[\s\S]*?\n {11}run: \|\n([\s\S]*?)\n {9}- name:/,
	);
	const body = match?.[1];
	if (!body) throw new Error("Failed to extract Upstream guardrail script from .github/workflows/ci.yml");
	return body
		.split("\n")
		.map(line => line.replace(/^ {14}/, ""))
		.join("\n");
}

interface Fixture {
	base: string;
	clone: string;
	merge: string;
	target: string;
	bin: string;
	bunLog: string;
	ghLog: string;
	summary: string;
}

async function makeFixture(): Promise<Fixture> {
	const base = await fs.mkdtemp(path.join(os.tmpdir(), "ci-upstream-guardrail-"));
	const origin = path.join(base, "origin.git");
	const src = path.join(base, "src");
	const clone = path.join(base, "clone");
	const originUrl = `file://${origin}`;
	await $`git init --bare -b main ${origin}`.quiet();
	await $`git -C ${origin} config uploadpack.allowAnySHA1InWant true`.quiet();
	await $`git init -b main ${src}`.quiet();
	await $`git -C ${src} config user.name Test`.quiet();
	await $`git -C ${src} config user.email test@example.com`.quiet();
	await $`git -C ${src} commit --allow-empty -m initial`.quiet();
	const target = (await $`git -C ${src} rev-parse HEAD`.text()).trim();
	const baseline = `${JSON.stringify(
		{
			upstream_repo: "https://github.com/example/up",
			base: target,
			fork: target,
			target,
		},
		null,
		2,
	)}\n`;
	const baselineBytes = path.join(base, "baseline.json");
	await Bun.write(baselineBytes, baseline);

	await fs.mkdir(path.join(src, "docs"));
	await fs.copyFile(baselineBytes, path.join(src, "docs", "upstream-baseline.json"));
	await $`git -C ${src} add docs/upstream-baseline.json`.quiet();
	await $`git -C ${src} commit -m "baseline on main"`.quiet();

	// Same bytes on the PR branch, branched before main's edit, so the merge
	// result matches the first parent and a first-parent diff misses the file.
	await $`git -C ${src} checkout -b pr ${target}`.quiet();
	await fs.mkdir(path.join(src, "docs"));
	await fs.copyFile(baselineBytes, path.join(src, "docs", "upstream-baseline.json"));
	await $`git -C ${src} add docs/upstream-baseline.json`.quiet();
	await $`git -C ${src} commit -m "baseline on pr"`.quiet();
	await $`git -C ${src} checkout main`.quiet();
	await $`git -C ${src} merge --no-ff pr -m "merge pr"`.quiet();
	const merge = (await $`git -C ${src} rev-parse HEAD`.text()).trim();
	const firstParentDiff = await $`git -C ${src} diff --name-only ${merge}^1 ${merge}`.text();
	if (firstParentDiff.split("\n").includes("docs/upstream-baseline.json")) {
		throw new Error("fixture: first-parent diff contains the baseline; the same-edit case is not reproduced");
	}

	await $`git -C ${src} remote add origin ${originUrl}`.quiet();
	await $`git -C ${src} push origin main`.quiet();
	await $`git -C ${src} push origin ${merge}:refs/pull/1/merge`.quiet();

	await $`git init -b main ${clone}`.quiet();
	await $`git -C ${clone} remote add origin ${originUrl}`.quiet();
	await $`git -C ${clone} fetch --depth 1 origin refs/pull/1/merge`.quiet();
	await $`git -C ${clone} checkout --detach FETCH_HEAD`.quiet();

	const bin = path.join(base, "bin");
	await fs.mkdir(bin);
	const bunLog = path.join(base, "bun.log");
	const ghLog = path.join(base, "gh.log");
	await Bun.write(bunLog, "");
	await Bun.write(ghLog, "");
	await Bun.write(path.join(bin, "bun"), '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$BUN_LOG"\nexit 0\n');
	await Bun.write(path.join(bin, "gh"), '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$GH_LOG"\nexit 1\n');
	await fs.chmod(path.join(bin, "bun"), 0o755);
	await fs.chmod(path.join(bin, "gh"), 0o755);

	return { base, clone, merge, target, bin, bunLog, ghLog, summary: path.join(base, "step-summary.txt") };
}

async function runGuardrail(
	fixture: Fixture,
): Promise<{ exitCode: number; output: string; bunLog: string; ghLog: string }> {
	const script = await loadGuardrailScript();
	await Bun.write(fixture.summary, "");
	const proc = Bun.spawn(["bash", "-c", script], {
		cwd: fixture.clone,
		env: {
			...process.env,
			PATH: `${fixture.bin}${path.delimiter}${process.env.PATH ?? ""}`,
			GITHUB_SHA: fixture.merge,
			GITHUB_STEP_SUMMARY: fixture.summary,
			GIT_TERMINAL_PROMPT: "0",
			BUN_LOG: fixture.bunLog,
			GH_LOG: fixture.ghLog,
		},
		stdout: "pipe",
		stderr: "pipe",
	});
	const [exitCode, stdout, stderr] = await Promise.all([
		proc.exited,
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
	]);
	return {
		exitCode,
		output: `${stdout}${stderr}`,
		bunLog: await Bun.file(fixture.bunLog).text(),
		ghLog: await Bun.file(fixture.ghLog).text(),
	};
}

describe("CI upstream guardrail changed files", () => {
	test("a baseline edit shared with main still runs the strict review from a depth-1 merge clone", async () => {
		const fixture = await makeFixture();
		try {
			const result = await runGuardrail(fixture);
			expect(result.exitCode, result.output).toBe(0);
			expect(result.bunLog).toContain("scripts/verify-upstream-handoff.ts --record docs/upstream-baseline.json");
			expect(result.ghLog).toBe("");
			const firstParentDiff =
				await $`git -C ${fixture.clone} diff --name-only ${fixture.merge}^1 ${fixture.merge}`.text();
			expect(firstParentDiff.split("\n")).not.toContain("docs/upstream-baseline.json");
		} finally {
			await fs.rm(fixture.base, { recursive: true, force: true });
		}
	}, 30000);

	test("a missing origin after the clone fails the step and does not run the strict review", async () => {
		const fixture = await makeFixture();
		try {
			await $`git -C ${fixture.clone} fetch --depth 1 origin ${fixture.target}`.quiet();
			await $`git -C ${fixture.clone} remote set-url origin file://${path.join(fixture.base, "missing.git")}`.quiet();
			const result = await runGuardrail(fixture);
			expect(result.exitCode, result.output).not.toBe(0);
			expect(result.bunLog).not.toContain("verify-upstream-handoff");
			expect(result.ghLog).toBe("");
		} finally {
			await fs.rm(fixture.base, { recursive: true, force: true });
		}
	}, 30000);
});
