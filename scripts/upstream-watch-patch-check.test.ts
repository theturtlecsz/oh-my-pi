import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";

interface WorkflowStep {
	name?: string;
	id?: string;
	if?: string;
	uses?: string;
	env?: Record<string, string>;
	run?: string;
}

interface UpstreamWatchWorkflow {
	jobs: {
		discover: { steps: WorkflowStep[] };
	};
}

const WORKFLOW_PATH = path.join(import.meta.dir, "..", ".github", "workflows", "upstream-watch.yml");
const NEWER = "steps.discovery.outputs.newer == 'true'";

const dirs: string[] = [];
const GIT_ENV = {
	...process.env,
	GIT_CONFIG_GLOBAL: "/dev/null",
	GIT_CONFIG_SYSTEM: "/dev/null",
	GIT_AUTHOR_NAME: "test",
	GIT_AUTHOR_EMAIL: "test@example.com",
	GIT_COMMITTER_NAME: "test",
	GIT_COMMITTER_EMAIL: "test@example.com",
};

async function loadWorkflow(): Promise<UpstreamWatchWorkflow> {
	return Bun.YAML.parse(await Bun.file(WORKFLOW_PATH).text()) as UpstreamWatchWorkflow;
}

async function run(dir: string, args: string[]): Promise<{ exitCode: number; out: string; err: string }> {
	const proc = await $`git ${args}`.cwd(dir).quiet().nothrow().env(GIT_ENV);
	return { exitCode: proc.exitCode, out: proc.text(), err: proc.stderr.toString() };
}

async function ok(dir: string, args: string[]): Promise<string> {
	const result = await run(dir, args);
	if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")} failed: ${result.err}`);
	return result.out;
}

async function commitAll(dir: string, subject: string): Promise<string> {
	await ok(dir, ["add", "-A"]);
	await ok(dir, ["commit", "-m", subject]);
	return (await ok(dir, ["rev-parse", "HEAD"])).trim();
}

/** Temp repo: baseline target = base; fork patch edits a.txt line 1; upstream edits a.txt or b.txt. */
async function makeRepo(): Promise<{ dir: string; upstreamA: string; upstreamB: string }> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-watch-patch-check-"));
	dirs.push(dir);
	await ok(dir, ["init", "-b", "main"]);
	await Bun.write(path.join(dir, "a.txt"), "a1\na2\na3\n");
	await Bun.write(path.join(dir, "b.txt"), "b1\nb2\nb3\n");
	const base = await commitAll(dir, "base");

	await ok(dir, ["checkout", "-b", "fork"]);
	await Bun.write(path.join(dir, "a.txt"), "a1-fork\na2\na3\n");
	await commitAll(dir, "fork edit a.txt line 1");

	await ok(dir, ["checkout", "-b", "upstream-a", base]);
	await Bun.write(path.join(dir, "a.txt"), "a1-upstream\na2\na3\n");
	const upstreamA = await commitAll(dir, "upstream edit a.txt line 1");

	await ok(dir, ["checkout", "-b", "upstream-b", base]);
	await Bun.write(path.join(dir, "b.txt"), "b1-upstream\nb2\nb3\n");
	const upstreamB = await commitAll(dir, "upstream edit b.txt line 1");

	await ok(dir, ["checkout", "fork"]);
	await Bun.write(
		path.join(dir, "docs", "upstream-baseline.json"),
		`${JSON.stringify({ upstream_repo: "https://github.com/can1357/oh-my-pi", target: base }, null, "\t")}\n`,
	);
	await Bun.write(
		path.join(dir, "docs", "upstream-fork-inventory.tsv"),
		[
			"path\tscope\tstate\thead_blob\tbehavior\tclassification",
			"a.txt\tshared\tmodified\t111111111111\tfork prepends a guard line to a.txt\tretained",
			"",
		].join("\n"),
	);
	await fs.symlink(import.meta.dir, path.join(dir, "scripts"));
	return { dir, upstreamA, upstreamB };
}

async function runStep(
	dir: string,
	script: string,
	commit: string,
	tag: string,
): Promise<{ exitCode: number; summary: string }> {
	const summaryFile = path.join(dir, "step_summary.md");
	await Bun.write(summaryFile, "prior summary\n");
	const result = await $`bash -c ${script}`.cwd(dir).quiet().nothrow().env({
		...GIT_ENV,
		GITHUB_STEP_SUMMARY: summaryFile,
		COMMIT: commit,
		TAG: tag,
	});
	return { exitCode: result.exitCode, summary: await Bun.file(summaryFile).text() };
}

afterEach(async () => {
	while (dirs.length) {
		const dir = dirs.pop() as string;
		await fs.unlink(path.join(dir, "scripts")).catch(() => {});
		await fs.rm(dir, { recursive: true, force: true });
	}
});

describe("upstream-watch fork-patch steps", () => {
	test("patch check runs after the candidate artifact upload", async () => {
		const steps = (await loadWorkflow()).jobs.discover.steps;
		const upload = steps.findIndex(s => s.uses?.startsWith("actions/upload-artifact"));
		const fetch = steps.findIndex(s => s.name === "Fetch candidate and baseline commits");
		const patch = steps.findIndex(s => s.id === "patch-check");
		expect(upload).toBeGreaterThanOrEqual(0);
		expect(fetch).toBeGreaterThan(upload);
		expect(patch).toBeGreaterThan(fetch);

		const fetchStep = steps[fetch];
		const patchStep = steps[patch];
		expect(fetchStep?.if).toBe(NEWER);
		expect(patchStep?.if).toBe(NEWER);
		expect(patchStep?.name).toBe("Check fork patches against candidate");
		expect(fetchStep?.env?.COMMIT).toBe("${{ steps.discovery.outputs.commit }}");
		expect(patchStep?.env?.COMMIT).toBe("${{ steps.discovery.outputs.commit }}");
		expect(patchStep?.env?.TAG).toBe("${{ steps.discovery.outputs.tag }}");

		const fetchRun = fetchStep?.run ?? "";
		expect(fetchRun).toContain("jq -r .upstream_repo docs/upstream-baseline.json");
		expect(fetchRun).toContain("jq -r .target docs/upstream-baseline.json");
		expect(fetchRun).toContain('git fetch --no-tags --depth=1 "$repo" "$target"');
		expect(fetchRun).toContain('git fetch --no-tags --depth=1 "$repo" "$COMMIT"');
		expect(fetchRun).not.toMatch(/\bgh\b/);

		const patchRun = patchStep?.run ?? "";
		expect(patchRun).toContain('bun scripts/upstream-patch-check.ts --target "$COMMIT"');
		expect(patchRun).toContain('echo "## Fork patches vs $TAG"');
		expect(patchRun).toContain('>> "$GITHUB_STEP_SUMMARY"');
		expect(patchRun).not.toMatch(/\bgh\b/);

		const firstNewerRun = steps.find(s => s.if === NEWER && s.run);
		expect(firstNewerRun?.run ?? "").toContain("upstream-candidate.json");
	});

	test("a conflicting candidate exits non-zero and records the broken patch", async () => {
		const step = (await loadWorkflow()).jobs.discover.steps.find(s => s.id === "patch-check");
		const { dir, upstreamA } = await makeRepo();
		const result = await runStep(dir, step?.run ?? "", upstreamA, "v-conflict");
		expect(result.exitCode).not.toBe(0);
		expect(result.summary).toContain("prior summary\n");
		expect(result.summary).toContain("## Fork patches vs v-conflict");
		expect(result.summary).toContain("BROKEN a.txt");
	});

	test("a clean candidate exits 0 and records PASS", async () => {
		const step = (await loadWorkflow()).jobs.discover.steps.find(s => s.id === "patch-check");
		const { dir, upstreamB } = await makeRepo();
		const result = await runStep(dir, step?.run ?? "", upstreamB, "v-clean");
		expect(result.exitCode).toBe(0);
		expect(result.summary).toContain("prior summary\n");
		expect(result.summary).toContain("## Fork patches vs v-clean");
		expect(result.summary).toContain("PASS");
	});
});
