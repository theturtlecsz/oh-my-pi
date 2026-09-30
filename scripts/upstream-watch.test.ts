import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";

interface WorkflowStep {
	name?: string;
	id?: string;
	if?: string;
	uses?: string;
	with?: Record<string, unknown>;
	env?: Record<string, string>;
	run?: string;
}

interface WorkflowJob {
	name?: string;
	"runs-on"?: string;
	steps: WorkflowStep[];
}

interface UpstreamWatchWorkflow {
	name: string;
	on: Record<string, unknown>;
	permissions: Record<string, string>;
	jobs: {
		discover: WorkflowJob;
	};
}

const WORKFLOW_PATH = path.join(import.meta.dir, "..", ".github", "workflows", "upstream-watch.yml");

describe("upstream-watch workflow contract", () => {
	test("permissions has contents: read and omits issues permission", async () => {
		const text = await Bun.file(WORKFLOW_PATH).text();
		const workflow = Bun.YAML.parse(text) as UpstreamWatchWorkflow;
		expect(workflow.permissions).toEqual({ contents: "read" });
		expect((workflow.permissions as Record<string, unknown>).issues).toBeUndefined();
	});

	test("workflow steps do not call gh issue", async () => {
		const text = await Bun.file(WORKFLOW_PATH).text();
		const workflow = Bun.YAML.parse(text) as UpstreamWatchWorkflow;
		for (const step of workflow.jobs.discover.steps) {
			if (step.run) {
				expect(step.run).not.toMatch(/\bgh\s+issue\b/);
			}
		}
	});

	test("candidate discovery step writes summary and prepares json artifact", async () => {
		const text = await Bun.file(WORKFLOW_PATH).text();
		const workflow = Bun.YAML.parse(text) as UpstreamWatchWorkflow;
		const candidateStep = workflow.jobs.discover.steps.find(
			s => s.if === "steps.discovery.outputs.newer == 'true'" && s.run,
		);
		expect(candidateStep).toBeDefined();
		const run = candidateStep?.run ?? "";
		expect(run).toContain("$GITHUB_STEP_SUMMARY");
		expect(run).toContain("upstream-candidate.json");
	});

	test("artifact upload step uploads upstream-candidate artifact", async () => {
		const text = await Bun.file(WORKFLOW_PATH).text();
		const workflow = Bun.YAML.parse(text) as UpstreamWatchWorkflow;
		const uploadStep = workflow.jobs.discover.steps.find(
			s => s.if === "steps.discovery.outputs.newer == 'true'" && s.uses?.startsWith("actions/upload-artifact"),
		);
		expect(uploadStep).toBeDefined();
		expect(uploadStep?.with?.name).toBe("upstream-candidate");
		expect(uploadStep?.with?.path).toBe("upstream-candidate.json");
	});

	test("up to date step reports status when newer is false", async () => {
		const text = await Bun.file(WORKFLOW_PATH).text();
		const workflow = Bun.YAML.parse(text) as UpstreamWatchWorkflow;
		const upToDateStep = workflow.jobs.discover.steps.find(
			s => s.if === "steps.discovery.outputs.newer != 'true'",
		);
		expect(upToDateStep).toBeDefined();
		const run = upToDateStep?.run ?? "";
		expect(run).toContain("$GITHUB_STEP_SUMMARY");
		expect(run).toContain("No stable upstream release above the accepted baseline");
	});
});

describe("upstream-watch step script execution", () => {
	test("candidate discovery step creates upstream-candidate.json and writes summary with exit 0", async () => {
		const text = await Bun.file(WORKFLOW_PATH).text();
		const workflow = Bun.YAML.parse(text) as UpstreamWatchWorkflow;
		const candidateStep = workflow.jobs.discover.steps.find(
			s => s.if === "steps.discovery.outputs.newer == 'true'" && s.run,
		);
		expect(candidateStep?.run).toBeDefined();

		const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-watch-test-candidate-"));
		const summaryFile = path.join(tmpDir, "step_summary.md");
		await Bun.write(summaryFile, "");

		try {
			const script = candidateStep?.run ?? "";
			const result = await $`bash -c ${script}`
				.cwd(tmpDir)
				.env({
					...process.env,
					GITHUB_STEP_SUMMARY: summaryFile,
					VERSION: "18.4.2",
					TAG: "v18.4.2",
					COMMIT: "4620bb8338e0ecace7ea237da9d5088d16068617",
					BASELINE: "18.0.6",
				})
				.quiet()
				.nothrow();

			expect(result.exitCode).toBe(0);

			const summaryContent = await Bun.file(summaryFile).text();
			expect(summaryContent).toContain("v18.4.2");
			expect(summaryContent).toContain("4620bb8338e0ecace7ea237da9d5088d16068617");
			expect(summaryContent).toContain("18.0.6");

			const artifactPath = path.join(tmpDir, "upstream-candidate.json");
			const artifactJson = (await Bun.file(artifactPath).json()) as {
				tag: string;
				commit: string;
				baseline: string;
				version: string;
			};
			expect(artifactJson.tag).toBe("v18.4.2");
			expect(artifactJson.commit).toBe("4620bb8338e0ecace7ea237da9d5088d16068617");
			expect(artifactJson.baseline).toBe("18.0.6");
			expect(artifactJson.version).toBe("18.4.2");
		} finally {
			await fs.rm(tmpDir, { recursive: true, force: true });
		}
	});

	test("up-to-date step writes summary and exits 0", async () => {
		const text = await Bun.file(WORKFLOW_PATH).text();
		const workflow = Bun.YAML.parse(text) as UpstreamWatchWorkflow;
		const upToDateStep = workflow.jobs.discover.steps.find(
			s => s.if === "steps.discovery.outputs.newer != 'true'",
		);
		expect(upToDateStep?.run).toBeDefined();

		const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-watch-test-uptodate-"));
		const summaryFile = path.join(tmpDir, "step_summary.md");
		await Bun.write(summaryFile, "");

		try {
			const script = upToDateStep?.run ?? "";
			const result = await $`bash -c ${script}`
				.cwd(tmpDir)
				.env({
					...process.env,
					GITHUB_STEP_SUMMARY: summaryFile,
					BASELINE: "18.0.6",
				})
				.quiet()
				.nothrow();

			expect(result.exitCode).toBe(0);

			const summaryContent = await Bun.file(summaryFile).text();
			expect(summaryContent).toContain("No stable upstream release above the accepted baseline (18.0.6).");
		} finally {
			await fs.rm(tmpDir, { recursive: true, force: true });
		}
	});

	test("discovery step against a fixed future baseline outputs newer: false and exits 0", async () => {
		const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-watch-test-baseline-"));
		const baselinePath = path.join(tmpDir, "upstream-baseline.json");
		await Bun.write(
			baselinePath,
			JSON.stringify({
				upstream_repo: "https://github.com/can1357/oh-my-pi",
				upstream_version: "99.0.0",
			}),
		);

		try {
			const repoRoot = path.join(import.meta.dir, "..");
			const result = await $`bun scripts/upstream-discovery.ts --baseline ${baselinePath} --json`
				.cwd(repoRoot)
				.quiet()
				.nothrow();

			expect(result.exitCode).toBe(0);
			const parsed = JSON.parse(result.text()) as { newer: boolean; baseline_version: string };
			expect(parsed.newer).toBe(false);
			expect(parsed.baseline_version).toBe("99.0.0");
		} finally {
			await fs.rm(tmpDir, { recursive: true, force: true });
		}
	});
});
