import { describe, expect, test } from "bun:test";
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

describe("workflow_dispatch and ref release detection logic", () => {
	function evaluateReleaseDetection(params: {
		eventName: string;
		ref: string;
		refName: string;
		inputReleaseTag?: string;
		tagsAtHead?: string[];
	}): { isRelease: boolean; releaseTag: string; channel: string } {
		let isRelease = false;
		let releaseTag = "";
		let channel = "stable";

		if (params.eventName === "workflow_dispatch" && params.inputReleaseTag) {
			const candidate = params.inputReleaseTag;
			if (validateReleaseTag(candidate)) {
				if (params.tagsAtHead?.includes(candidate)) {
					releaseTag = candidate;
				}
			}
		} else {
			if (params.ref.startsWith("refs/tags/v")) {
				releaseTag = params.refName;
			} else if (params.ref === "refs/heads/main") {
				if (params.eventName !== "pull_request") {
					const matching = params.tagsAtHead?.find(t => /^v\d/.test(t));
					if (matching) releaseTag = matching;
				}
			}
		}

		if (releaseTag) {
			isRelease = true;
		}
		if (releaseTag.includes("-canary.")) {
			channel = "canary";
		}

		return { isRelease, releaseTag, channel };
	}

	test("handles workflow_dispatch with valid release_tag on refs/heads/main", () => {
		const result = evaluateReleaseDetection({
			eventName: "workflow_dispatch",
			ref: "refs/heads/main",
			refName: "main",
			inputReleaseTag: "v18.0.7",
			tagsAtHead: ["v18.0.7"],
		});
		expect(result.isRelease).toBe(true);
		expect(result.releaseTag).toBe("v18.0.7");
		expect(result.channel).toBe("stable");
	});

	test("handles workflow_dispatch with valid canary release_tag", () => {
		const result = evaluateReleaseDetection({
			eventName: "workflow_dispatch",
			ref: "refs/heads/main",
			refName: "main",
			inputReleaseTag: "v18.0.7-canary.1",
			tagsAtHead: ["v18.0.7-canary.1"],
		});
		expect(result.isRelease).toBe(true);
		expect(result.releaseTag).toBe("v18.0.7-canary.1");
		expect(result.channel).toBe("canary");
	});

	test("rejects workflow_dispatch with invalid release_tag format", () => {
		const result = evaluateReleaseDetection({
			eventName: "workflow_dispatch",
			ref: "refs/heads/main",
			refName: "main",
			inputReleaseTag: "18.0.7",
			tagsAtHead: ["18.0.7"],
		});
		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
	});

	test("detects tag push events", () => {
		const result = evaluateReleaseDetection({
			eventName: "push",
			ref: "refs/tags/v18.0.7",
			refName: "v18.0.7",
		});
		expect(result.isRelease).toBe(true);
		expect(result.releaseTag).toBe("v18.0.7");
	});

	test("does not detect release on PR events", () => {
		const result = evaluateReleaseDetection({
			eventName: "pull_request",
			ref: "refs/heads/main",
			refName: "main",
			tagsAtHead: ["v18.0.7"],
		});
		expect(result.isRelease).toBe(false);
		expect(result.releaseTag).toBe("");
	});
});
