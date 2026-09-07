import { describe, expect, test } from "bun:test";
import { parseGithubRepo, pickNewestStable, resolveTagCommit, type UpstreamRelease } from "./upstream-discovery.ts";

const release = (tag_name: string, extra: Partial<UpstreamRelease> = {}): UpstreamRelease => ({
	tag_name,
	draft: false,
	prerelease: false,
	...extra,
});

describe("pickNewestStable", () => {
	test("picks the newest final release above the baseline", () => {
		const releases = [release("v18.1.0"), release("v18.1.2"), release("v18.1.1"), release("v18.0.6")];
		expect(pickNewestStable(releases, "18.0.6")).toEqual({ tag: "v18.1.2", version: "18.1.2" });
	});
	test("groups multiple intervening releases into the single newest candidate", () => {
		const releases = [release("v18.0.7"), release("v18.0.8"), release("v18.1.0")];
		expect(pickNewestStable(releases, "18.0.6")).toEqual({ tag: "v18.1.0", version: "18.1.0" });
	});
	test("excludes drafts and prereleases", () => {
		const releases = [
			release("v18.2.0", { draft: true }),
			release("v18.1.9", { prerelease: true }),
			release("v18.1.2"),
		];
		expect(pickNewestStable(releases, "18.0.6")).toEqual({ tag: "v18.1.2", version: "18.1.2" });
	});
	test("excludes non-final tags", () => {
		const releases = [release("v18.2.0-rc.1"), release("v18.2.0-preview"), release("nightly"), release("v18.1.2")];
		expect(pickNewestStable(releases, "18.0.6")).toEqual({ tag: "v18.1.2", version: "18.1.2" });
	});
	test("returns null at or below the baseline", () => {
		expect(pickNewestStable([release("v18.0.6"), release("v18.0.5")], "18.0.6")).toBeNull();
		expect(pickNewestStable([], "18.0.6")).toBeNull();
	});
	test("compares semver numerically, not lexically", () => {
		expect(pickNewestStable([release("v18.9.0"), release("v18.10.0")], "18.0.6")).toEqual({
			tag: "v18.10.0",
			version: "18.10.0",
		});
	});
});

describe("resolveTagCommit", () => {
	test("prefers the peeled commit of an annotated tag", () => {
		const text = [
			"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/tags/v18.1.2",
			"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\trefs/tags/v18.1.2^{}",
			"",
		].join("\n");
		expect(resolveTagCommit(text, "v18.1.2")).toBe("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
	});
	test("falls back to the tag object for lightweight tags", () => {
		const text = "cccccccccccccccccccccccccccccccccccccccc\trefs/tags/v18.1.2\n";
		expect(resolveTagCommit(text, "v18.1.2")).toBe("cccccccccccccccccccccccccccccccccccccccc");
	});
	test("never matches a different tag, including prefix collisions", () => {
		const text = "dddddddddddddddddddddddddddddddddddddddd\trefs/tags/v18.1.20\n";
		expect(resolveTagCommit(text, "v18.1.2")).toBeNull();
	});
});

describe("parseGithubRepo", () => {
	test("parses owner/repo with optional .git and trailing slash", () => {
		expect(parseGithubRepo("https://github.com/can1357/oh-my-pi")).toEqual({ owner: "can1357", repo: "oh-my-pi" });
		expect(parseGithubRepo("https://github.com/can1357/oh-my-pi.git")).toEqual({
			owner: "can1357",
			repo: "oh-my-pi",
		});
		expect(parseGithubRepo("https://github.com/can1357/oh-my-pi/")).toEqual({ owner: "can1357", repo: "oh-my-pi" });
	});
	test("rejects non-github URLs", () => {
		expect(() => parseGithubRepo("git@github.com:can1357/oh-my-pi.git")).toThrow(/unsupported/);
	});
});
