import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import { beforeAll, describe, expect, test } from "bun:test";
import { bumpCanaryVersion, bumpVersion, filterRunsForTag, formatReleaseBranchPushArgs, formatReleaseTagPushArgs, releaseBranchName, releasePrTitle, validateExplicitVersion, validateReleaseTag } from "./release";

describe("validateExplicitVersion", () => {
	test("rejects malformed versions", () => {
		expect(validateExplicitVersion("999.bad")).toBe(null);
		expect(validateExplicitVersion("17")).toBe(null);
		expect(validateExplicitVersion("17.2")).toBe(null);
		expect(validateExplicitVersion("17.2.8.9")).toBe(null);
		expect(validateExplicitVersion("v17.2.8.9")).toBe(null);
		expect(validateExplicitVersion("abc")).toBe(null);
		expect(validateExplicitVersion("")).toBe(null);
		expect(validateExplicitVersion("v")).toBe(null);
		expect(validateExplicitVersion("17.2.8-")).toBe(null);
	});

	test("rejects leading zeroes in numeric segments", () => {
		expect(validateExplicitVersion("018.0.0")).toBe(null);
		expect(validateExplicitVersion("v018.0.0")).toBe(null);
		expect(validateExplicitVersion("18.00.0")).toBe(null);
		expect(validateExplicitVersion("18.0.00")).toBe(null);
	});

	test("rejects prerelease suffixes (not supported by this release path)", () => {
		// Prereleases would be published as npm `latest` because the downstream
		// publish runs `npm publish` with no `--tag`.
		expect(validateExplicitVersion("17.2.8-rc.1")).toBe(null);
		expect(validateExplicitVersion("v17.2.8-beta")).toBe(null);
		expect(validateExplicitVersion("1.0.0-alpha")).toBe(null);
		expect(validateExplicitVersion("1.0.0-alpha.1.2")).toBe(null);
		expect(validateExplicitVersion("1.0.0-0.3.7")).toBe(null);
		expect(validateExplicitVersion("1.0.0-x.7.z.92")).toBe(null);
	});

	test("accepts bare three-segment numeric versions and returns them unchanged", () => {
		expect(validateExplicitVersion("17.2.8")).toBe("17.2.8");
		expect(validateExplicitVersion("0.0.0")).toBe("0.0.0");
		expect(validateExplicitVersion("1.0.0")).toBe("1.0.0");
	});

	test("accepts leading v prefix and normalizes to the bare version", () => {
		expect(validateExplicitVersion("v17.2.8")).toBe("17.2.8");
		expect(validateExplicitVersion("V17.2.8")).toBe(null);
	});
});

describe("release version bumps", () => {
	test("starts a canary patch release after the current stable version", () => {
		expect(bumpCanaryVersion("0.13.0")).toBe("0.13.1-canary.1");
	});

	test("increments the existing canary release number", () => {
		expect(bumpCanaryVersion("0.13.0-canary.2")).toBe("0.13.0-canary.3");
	});

	test("finalizes a canary with a patch bump", () => {
		expect(bumpVersion("0.13.0-canary.2", "patch")).toBe("0.13.0");
	});

	test("bumps the core version when applying a minor bump to a canary", () => {
		expect(bumpVersion("0.13.0-canary.2", "minor")).toBe("0.14.0");
	});

	test("rejects explicit canary versions", () => {
		expect(validateExplicitVersion("1.2.3-canary.1")).toBe(null);
	});
});

describe("release branch and push formatting", () => {
	test("formats release branch name", () => {
		expect(releaseBranchName("18.0.7")).toBe("release/v18.0.7");
	});

	test("formats release PR title", () => {
		expect(releasePrTitle("18.0.7")).toBe("chore: bump version to v18.0.7");
	});

	test("formats push arguments targeting release branch only (no tag, no direct main push)", () => {
		const args = formatReleaseBranchPushArgs("18.0.7");
		expect(args).toEqual(["push", "origin", "HEAD:refs/heads/release/v18.0.7"]);
		expect(args.some(a => a.includes("refs/heads/main"))).toBe(false);
		expect(args.some(a => a.includes("refs/tags/"))).toBe(false);
	});

	test("formats post-merge tag push arguments", () => {
		const args = formatReleaseTagPushArgs("18.0.7", "a1b2c3d4e5f6");
		expect(args).toEqual(["push", "origin", "a1b2c3d4e5f6:refs/tags/v18.0.7"]);
	});
});

describe("validateReleaseTag", () => {
	test("accepts valid semantic version tags", () => {
		expect(validateReleaseTag("v18.0.7")).toBe(true);
		expect(validateReleaseTag("v0.1.0")).toBe(true);
		expect(validateReleaseTag("v18.0.7-canary.1")).toBe(true);
	});

	test("rejects malformed or un-prefixed tags", () => {
		expect(validateReleaseTag("18.0.7")).toBe(false);
		expect(validateReleaseTag("v18.0")).toBe(false);
		expect(validateReleaseTag("v18.0.07")).toBe(false);
		expect(validateReleaseTag("v18.0.7-beta.1")).toBe(false);
		expect(validateReleaseTag("")).toBe(false);
		expect(validateReleaseTag("random-tag")).toBe(false);
	});
});

describe("filterRunsForTag", () => {
	const mainRun = { databaseId: 101, status: "completed", conclusion: "success", name: "CI", headBranch: "main" };
	const otherBranchRun = { databaseId: 102, status: "completed", conclusion: "success", name: "CI", headBranch: "feature" };
	const tagRun = { databaseId: 201, status: "in_progress", conclusion: null, name: "CI", headBranch: "v18.0.7" };
	const completedTagRun = { databaseId: 201, status: "completed", conclusion: "success", name: "CI", headBranch: "v18.0.7" };

	test("returns all runs when no tagRef is specified", () => {
		const runs = [mainRun, otherBranchRun];
		expect(filterRunsForTag(runs)).toEqual(runs);
		expect(filterRunsForTag(runs, undefined)).toEqual(runs);
	});

	test("filters strictly by bare tag name", () => {
		const runs = [mainRun, tagRun];
		expect(filterRunsForTag(runs, "v18.0.7")).toEqual([tagRun]);
		expect(filterRunsForTag(runs, "refs/tags/v18.0.7")).toEqual([tagRun]);
	});

	test("returns empty array when tag run has not yet appeared even if prior main run succeeded", () => {
		// Critical safety guard: existing completed main run must NOT be accepted before tag run starts
		const runs = [mainRun];
		const filtered = filterRunsForTag(runs, "v18.0.7");
		expect(filtered).toEqual([]);
	});

	test("matches tag run once it appears alongside main run", () => {
		const runs = [mainRun, completedTagRun];
		const filtered = filterRunsForTag(runs, "v18.0.7");
		expect(filtered).toEqual([completedTagRun]);
	});
});

describe("CI release_metadata workflow detection", () => {
	let originDir: string;
	let localDir: string;
	let detectScript: string;

	beforeAll(async () => {
		const ciYml = await Bun.file(".github/workflows/ci.yml").text();
		const detectStepMatch = ciYml.match(/id:\s*detect[\s\S]*?run:\s*\|\n([\s\S]*?)\n\s*check:/);
		if (!detectStepMatch) {
			throw new Error("Failed to extract detect script from .github/workflows/ci.yml");
		}
		// Strip leading 14 spaces of yaml indentation
		detectScript = detectStepMatch[1]
			.split("\n")
			.map(line => line.replace(/^ {14}/, ""))
			.join("\n");
	});

	async function setupGitRepos(): Promise<{ origin: string; local: string }> {
		const baseTemp = await fs.promises.mkdtemp(path.join(os.tmpdir(), "ci-release-test-"));
		const origin = path.join(baseTemp, "origin.git");
		const local = path.join(baseTemp, "local");

		await $`git init --bare ${origin}`.quiet();
		await $`git clone ${origin} ${local}`.quiet();
		await $`git -C ${local} config user.name "Test"`.quiet();
		await $`git -C ${local} config user.email "test@example.com"`.quiet();
		await $`git -C ${local} commit --allow-empty -m "initial commit"`.quiet();
		await $`git -C ${local} branch -M main`.quiet();
		await $`git -C ${local} push origin main`.quiet();

		return { origin, local };
	}

	async function runDetectScript(
		cwd: string,
		env: {
			EVENT_NAME: string;
			REF: string;
			REF_NAME: string;
			RELEASE_TAG_INPUT?: string;
		},
	): Promise<{ isRelease: boolean; releaseTag: string; channel: string; exitCode: number; stdout: string }> {
		const outputFile = path.join(cwd, ".github_output_tmp");
		await Bun.write(outputFile, "");

		const proc = Bun.spawn(["bash", "-c", detectScript], {
			cwd,
			env: {
				...process.env,
				EVENT_NAME: env.EVENT_NAME,
				REF: env.REF,
				REF_NAME: env.REF_NAME,
				RELEASE_TAG_INPUT: env.RELEASE_TAG_INPUT ?? "",
				GITHUB_OUTPUT: outputFile,
			},
			stdout: "pipe",
			stderr: "pipe",
		});

		const stdout = await new Response(proc.stdout).text();
		const stderr = await new Response(proc.stderr).text();
		const exitCode = await proc.exited;

		const outputContent = await Bun.file(outputFile).text();
		const isReleaseMatch = outputContent.match(/^is-release=(true|false)$/m);
		const releaseTagMatch = outputContent.match(/^release-tag=([^\n]*)$/m);
		const channelMatch = outputContent.match(/^channel=([^\n]*)$/m);

		return {
			isRelease: isReleaseMatch ? isReleaseMatch[1] === "true" : false,
			releaseTag: releaseTagMatch ? releaseTagMatch[1] : "",
			channel: channelMatch ? channelMatch[1] : "",
			exitCode,
			stdout: stdout + stderr,
		};
	}

	test("workflow_dispatch with valid release_tag on main branch triggers release", async () => {
		const { local } = await setupGitRepos();
		await $`git -C ${local} tag v18.0.7`.quiet();
		await $`git -C ${local} push origin v18.0.7`.quiet();

		const result = await runDetectScript(local, {
			EVENT_NAME: "workflow_dispatch",
			REF: "refs/heads/main",
			REF_NAME: "main",
			RELEASE_TAG_INPUT: "v18.0.7",
		});

		expect(result.exitCode).toBe(0);
		expect(result.isRelease).toBe(true);
		expect(result.releaseTag).toBe("v18.0.7");
		expect(result.channel).toBe("stable");
	});

	test("workflow_dispatch with canary tag triggers canary release", async () => {
		const { local } = await setupGitRepos();
		await $`git -C ${local} tag v18.0.7-canary.1`.quiet();
		await $`git -C ${local} push origin v18.0.7-canary.1`.quiet();

		const result = await runDetectScript(local, {
			EVENT_NAME: "workflow_dispatch",
			REF: "refs/heads/main",
			REF_NAME: "main",
			RELEASE_TAG_INPUT: "v18.0.7-canary.1",
		});

		expect(result.exitCode).toBe(0);
		expect(result.isRelease).toBe(true);
		expect(result.releaseTag).toBe("v18.0.7-canary.1");
		expect(result.channel).toBe("canary");
	});

	test("workflow_dispatch refuses shell metacharacter injection attack", async () => {
		const { local } = await setupGitRepos();

		const injectionPayload = '"; release_tag=v99.0.0; candidate_tag="x';
		const result = await runDetectScript(local, {
			EVENT_NAME: "workflow_dispatch",
			REF: "refs/heads/main",
			REF_NAME: "main",
			RELEASE_TAG_INPUT: injectionPayload,
		});

		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
		expect(result.stdout).toContain("Invalid release tag format");
	});

	test("workflow_dispatch refuses multiline input attempting GITHUB_OUTPUT injection", async () => {
		const { local } = await setupGitRepos();
		await $`git -C ${local} tag v18.0.7`.quiet();
		await $`git -C ${local} push origin v18.0.7`.quiet();

		const multilinePayload = "v18.0.7\nrelease-tag=v99.0.0\nis-release=true";
		const result = await runDetectScript(local, {
			EVENT_NAME: "workflow_dispatch",
			REF: "refs/heads/main",
			REF_NAME: "main",
			RELEASE_TAG_INPUT: multilinePayload,
		});

		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
		expect(result.stdout).toContain("Invalid release tag format");
	});

	test("workflow_dispatch refuses malformed or un-prefixed release tags", async () => {
		const { local } = await setupGitRepos();

		const result = await runDetectScript(local, {
			EVENT_NAME: "workflow_dispatch",
			REF: "refs/heads/main",
			REF_NAME: "main",
			RELEASE_TAG_INPUT: "18.0.7",
		});

		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
		expect(result.stdout).toContain("Invalid release tag format: 18.0.7");
	});

	test("workflow_dispatch refuses tag that does not point to checked-out HEAD", async () => {
		const { local } = await setupGitRepos();
		// Tag initial commit
		await $`git -C ${local} tag v18.0.7`.quiet();
		await $`git -C ${local} push origin v18.0.7`.quiet();
		// Advance HEAD without moving tag
		await $`git -C ${local} commit --allow-empty -m "advance commit"`.quiet();
		await $`git -C ${local} push origin main`.quiet();

		const result = await runDetectScript(local, {
			EVENT_NAME: "workflow_dispatch",
			REF: "refs/heads/main",
			REF_NAME: "main",
			RELEASE_TAG_INPUT: "v18.0.7",
		});

		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
		expect(result.stdout).toContain("does not point to checked-out HEAD");
	});

	test("workflow_dispatch refuses tag not reachable from origin/main", async () => {
		const { local } = await setupGitRepos();
		// Create a separate unmerged branch
		await $`git -C ${local} checkout -b feature-unmerged`.quiet();
		await $`git -C ${local} commit --allow-empty -m "unmerged feature"`.quiet();
		await $`git -C ${local} tag v18.0.7`.quiet();
		await $`git -C ${local} push origin v18.0.7`.quiet();

		const result = await runDetectScript(local, {
			EVENT_NAME: "workflow_dispatch",
			REF: "refs/heads/feature-unmerged",
			REF_NAME: "feature-unmerged",
			RELEASE_TAG_INPUT: "v18.0.7",
		});

		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
		expect(result.stdout).toContain("is not reachable from origin/main");
	});

	test("push event to refs/tags/v* triggers release directly when tag is valid semver", async () => {
		const { local } = await setupGitRepos();

		const result = await runDetectScript(local, {
			EVENT_NAME: "push",
			REF: "refs/tags/v18.0.7",
			REF_NAME: "v18.0.7",
		});

		expect(result.isRelease).toBe(true);
		expect(result.releaseTag).toBe("v18.0.7");
	});

	test("push event to refs/tags/* ignores tags with invalid format or shell metacharacters", async () => {
		const { local } = await setupGitRepos();

		const injectionResult = await runDetectScript(local, {
			EVENT_NAME: "push",
			REF: 'refs/tags/v99.0.0"; echo hacked',
			REF_NAME: 'v99.0.0"; echo hacked',
		});
		expect(injectionResult.isRelease).toBe(false);
		expect(injectionResult.releaseTag).toBe("");
		expect(injectionResult.stdout).toContain("Tag Ignored");

		const malformedResult = await runDetectScript(local, {
			EVENT_NAME: "push",
			REF: "refs/tags/v18.0",
			REF_NAME: "v18.0",
		});
		expect(malformedResult.isRelease).toBe(false);
		expect(malformedResult.releaseTag).toBe("");
		expect(malformedResult.stdout).toContain("Tag Ignored");
	});

	test("push event to refs/heads/main with tag at HEAD triggers release", async () => {
		const { local } = await setupGitRepos();
		await $`git -C ${local} tag v18.0.7`.quiet();

		const result = await runDetectScript(local, {
			EVENT_NAME: "push",
			REF: "refs/heads/main",
			REF_NAME: "main",
		});

		expect(result.isRelease).toBe(true);
		expect(result.releaseTag).toBe("v18.0.7");
	});

	test("push event to refs/heads/main without tag at HEAD does not trigger release", async () => {
		const { local } = await setupGitRepos();

		const result = await runDetectScript(local, {
			EVENT_NAME: "push",
			REF: "refs/heads/main",
			REF_NAME: "main",
		});

		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
	});

	test("pull_request event does not trigger release even if tag points at HEAD", async () => {
		const { local } = await setupGitRepos();
		await $`git -C ${local} tag v18.0.7`.quiet();

		const result = await runDetectScript(local, {
			EVENT_NAME: "pull_request",
			REF: "refs/heads/main",
			REF_NAME: "main",
		});

		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
	});
});
