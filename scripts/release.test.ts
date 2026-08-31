import { describe, expect, test } from "bun:test";
import { bumpCanaryVersion, bumpVersion, formatReleaseBranchPushArgs, formatReleaseTagPushArgs, releaseBranchName, releasePrTitle, validateExplicitVersion, validateReleaseTag } from "./release";

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
