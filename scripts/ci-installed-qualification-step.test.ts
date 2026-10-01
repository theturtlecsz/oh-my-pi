import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";

async function loadStepScript(): Promise<string> {
	const ciYml = await Bun.file(path.join(import.meta.dir, "..", ".github", "workflows", "ci.yml")).text();
	const match = ciYml.match(/name: Resolve installed qualification mode[\s\S]*?\n\s+run: \|\n([\s\S]*?)\n\s+- name:/);
	const body = match?.[1];
	if (!body) {
		throw new Error("Failed to extract Resolve installed qualification mode script from .github/workflows/ci.yml");
	}
	return body
		.split("\n")
		.map(line => line.replace(/^ {14}/, ""))
		.join("\n");
}

interface Fixture {
	base: string;
	clone: string;
	merge: string;
	bin: string;
	bunLog: string;
	ghLog: string;
	copiedFiles: string;
	runnerTemp: string;
	githubOutput: string;
	prFiles: string[];
}

async function makeFixture(): Promise<Fixture> {
	const base = await fs.mkdtemp(path.join(os.tmpdir(), "ci-installed-qualification-"));
	const origin = path.join(base, "origin.git");
	const src = path.join(base, "src");
	const clone = path.join(base, "clone");

	await $`git init --bare -b main ${origin}`.quiet();
	await $`git -C ${origin} config uploadpack.allowAnySHA1InWant true`.quiet();

	await $`git init -b main ${src}`.quiet();
	await $`git -C ${src} config user.name Test`.quiet();
	await $`git -C ${src} config user.email test@example.com`.quiet();
	await $`git -C ${src} commit --allow-empty -m initial`.quiet();
	const baseCommit = (await $`git -C ${src} rev-parse HEAD`.text()).trim();

	const prFiles: string[] = [];
	await $`git -C ${src} checkout -b pr ${baseCommit}`.quiet();
	for (let i = 1; i <= 301; i++) {
		const filename = `file-${String(i).padStart(3, "0")}.txt`;
		prFiles.push(filename);
		await Bun.write(path.join(src, filename), `file ${i}`);
	}
	await $`git -C ${src} add .`.quiet();
	await $`git -C ${src} commit -m "pr adds 301 files"`.quiet();

	await $`git -C ${src} checkout main`.quiet();
	await Bun.write(path.join(src, "base-only.txt"), "base only\n");
	await $`git -C ${src} add base-only.txt`.quiet();
	await $`git -C ${src} commit -m "main adds base-only.txt"`.quiet();

	await $`git -C ${src} merge --no-ff pr -m "merge pr"`.quiet();
	const merge = (await $`git -C ${src} rev-parse HEAD`.text()).trim();

	await $`git -C ${src} remote add origin ${origin}`.quiet();
	await $`git -C ${src} push origin main`.quiet();
	await $`git -C ${src} push origin ${merge}:refs/pull/1/merge`.quiet();

	await $`git init -b main ${clone}`.quiet();
	await $`git -C ${clone} remote add origin ${origin}`.quiet();
	await $`git -C ${clone} fetch --depth 1 origin refs/pull/1/merge`.quiet();
	await $`git -C ${clone} checkout --detach FETCH_HEAD`.quiet();

	const bin = path.join(base, "bin");
	await fs.mkdir(bin, { recursive: true });
	const bunLog = path.join(base, "bun.log");
	const ghLog = path.join(base, "gh.log");
	const copiedFiles = path.join(base, "copied-changed-files.txt");
	await Bun.write(bunLog, "");
	await Bun.write(ghLog, "");

	const bunShim = `#!/bin/sh
printf '%s\\n' "$*" >> "$BUN_LOG"
if [ -n "$CHANGED_FILES_FILE" ] && [ -f "$CHANGED_FILES_FILE" ]; then
	cp "$CHANGED_FILES_FILE" "$COPIED_CHANGED_FILES"
fi
exit 0
`;
	const ghShim = `#!/bin/sh
printf '%s\\n' "$*" >> "$GH_LOG"
exit 1
`;
	await Bun.write(path.join(bin, "bun"), bunShim);
	await Bun.write(path.join(bin, "gh"), ghShim);
	await fs.chmod(path.join(bin, "bun"), 0o755);
	await fs.chmod(path.join(bin, "gh"), 0o755);

	const runnerTemp = path.join(base, "runner-temp");
	await fs.mkdir(runnerTemp, { recursive: true });
	const githubOutput = path.join(base, "github-output.txt");
	await Bun.write(githubOutput, "");

	return {
		base,
		clone,
		merge,
		bin,
		bunLog,
		ghLog,
		copiedFiles,
		runnerTemp,
		githubOutput,
		prFiles,
	};
}

interface StepRunResult {
	exitCode: number;
	stdout: string;
	stderr: string;
	bunLog: string;
	ghLog: string;
}

async function runStep(fixture: Fixture): Promise<StepRunResult> {
	const script = await loadStepScript();
	const proc = Bun.spawn(["bash", "-c", script], {
		cwd: fixture.clone,
		env: {
			...process.env,
			PATH: `${fixture.bin}${path.delimiter}${process.env.PATH ?? ""}`,
			EVENT_NAME: "pull_request",
			IS_RELEASE: "false",
			GITHUB_SHA: fixture.merge,
			RUNNER_TEMP: fixture.runnerTemp,
			GITHUB_OUTPUT: fixture.githubOutput,
			GIT_TERMINAL_PROMPT: "0",
			BUN_LOG: fixture.bunLog,
			GH_LOG: fixture.ghLog,
			COPIED_CHANGED_FILES: fixture.copiedFiles,
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
		stdout,
		stderr,
		bunLog: await Bun.file(fixture.bunLog).text(),
		ghLog: await Bun.file(fixture.ghLog).text(),
	};
}

describe("CI installed qualification mode step", () => {
	test("resolves 301 PR files without gh and calls bun qualification mode script", async () => {
		const fixture = await makeFixture();
		try {
			const result = await runStep(fixture);
			expect(result.exitCode, `${result.stdout}\n${result.stderr}`).toBe(0);
			expect(result.bunLog).toContain("scripts/installed-qualification-mode.ts");
			expect(result.ghLog).toBe("");

			const copiedText = await Bun.file(fixture.copiedFiles).text();
			const copiedFiles = copiedText.trim().split("\n").filter(Boolean).sort();
			const expectedFiles = [...fixture.prFiles].sort();
			expect(copiedFiles).toEqual(expectedFiles);
			expect(copiedFiles).toHaveLength(301);
			expect(copiedFiles).not.toContain("base-only.txt");
		} finally {
			await fs.rm(fixture.base, { recursive: true, force: true });
		}
	}, 30000);

	test("falls back to full mode when origin is missing and does not call bun or gh", async () => {
		const fixture = await makeFixture();
		try {
			await $`git -C ${fixture.clone} remote set-url origin ${path.join(fixture.base, "missing.git")}`.quiet();
			const result = await runStep(fixture);
			expect(result.exitCode, `${result.stdout}\n${result.stderr}`).toBe(0);
			expect(result.stdout).toContain("installed qualification mode: full (changed files unavailable: git fetch");
			const output = await Bun.file(fixture.githubOutput).text();
			expect(output).toContain("mode=full");
			expect(result.bunLog).toBe("");
			expect(result.ghLog).toBe("");
		} finally {
			await fs.rm(fixture.base, { recursive: true, force: true });
		}
	}, 30000);
});
