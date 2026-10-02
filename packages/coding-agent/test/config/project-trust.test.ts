import { afterEach, describe, expect, it } from "bun:test";
import { $ } from "bun";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
	isProjectPathTrusted,
	readTrustedProjectPaths,
	trustedProjectsPath,
} from "@oh-my-pi/pi-coding-agent/config/project-trust";
import { getAgentDir } from "@oh-my-pi/pi-utils";

const roots: string[] = [];

function makeRoot(prefix: string): string {
	const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), prefix)));
	roots.push(root);
	return root;
}

function writeTrusted(agentDir: string, paths: unknown[]): void {
	fs.mkdirSync(agentDir, { recursive: true });
	fs.writeFileSync(trustedProjectsPath(agentDir), JSON.stringify({ version: 1, paths }), "utf-8");
}

async function initRepo(dir: string): Promise<void> {
	fs.mkdirSync(dir, { recursive: true });
	await $`git init --initial-branch=main`.cwd(dir).quiet();
	await $`git -c user.email=test@example.com -c user.name=Test commit --allow-empty -m init`.cwd(dir).quiet();
}

afterEach(() => {
	for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 50 });
});

describe("project trust list", () => {
	it("trusts the default agent directory when no explicit one is given", () => {
		expect(trustedProjectsPath()).toBe(path.join(getAgentDir(), "trusted-projects.json"));
	});

	it("trusts a listed directory and its subdirectories, not others", () => {
		const root = makeRoot("omp-trust-");
		const agentDir = path.join(root, "agent");
		const project = path.join(root, "project");
		const other = path.join(root, "other");
		fs.mkdirSync(path.join(project, "sub", "deep"), { recursive: true });
		fs.mkdirSync(other, { recursive: true });
		writeTrusted(agentDir, [project]);

		expect(isProjectPathTrusted(project, agentDir)).toBe(true);
		expect(isProjectPathTrusted(path.join(project, "sub", "deep"), agentDir)).toBe(true);
		expect(isProjectPathTrusted(other, agentDir)).toBe(false);
		// Sibling of a trusted path sharing a name prefix must not slip through
		// a naive `startsWith` containment check.
		expect(isProjectPathTrusted(`${project}-sibling`, agentDir)).toBe(false);
	});

	it("trusts a linked worktree of a listed checkout but not one of an unlisted checkout", async () => {
		const root = makeRoot("omp-trust-worktree-");
		const agentDir = path.join(root, "agent");
		const listed = path.join(root, "listed");
		const unlisted = path.join(root, "unlisted");
		await initRepo(listed);
		await initRepo(unlisted);
		const listedWorktree = path.join(root, "worktrees", "listed");
		const unlistedWorktree = path.join(root, "worktrees", "unlisted");
		await $`git worktree add ${listedWorktree} -b wt-listed`.cwd(listed).quiet();
		await $`git worktree add ${unlistedWorktree} -b wt-unlisted`.cwd(unlisted).quiet();
		writeTrusted(agentDir, [listed]);

		expect(isProjectPathTrusted(listedWorktree, agentDir)).toBe(true);
		// The worktree root resolves to its main checkout; a nested directory does too.
		expect(isProjectPathTrusted(path.join(listedWorktree, "nested"), agentDir)).toBe(true);
		expect(isProjectPathTrusted(unlistedWorktree, agentDir)).toBe(false);
	});

	it("rejects arbitrary directories that spoof a gitdir pointer without genuine linked-worktree registration", async () => {
		const root = makeRoot("omp-trust-spoof-");
		const agentDir = path.join(root, "agent");
		const listed = path.join(root, "listed");
		await initRepo(listed);
		const listedWorktree = path.join(root, "worktrees", "listed");
		await $`git worktree add ${listedWorktree} -b wt-listed`.cwd(listed).quiet();
		writeTrusted(agentDir, [listed]);

		// An unlisted directory containing .git with gitdir: /listed-checkout/.git
		const rogueMain = path.join(root, "rogue-main");
		fs.mkdirSync(path.join(rogueMain, "sub"), { recursive: true });
		fs.writeFileSync(path.join(rogueMain, ".git"), `gitdir: ${path.join(listed, ".git")}\n`, "utf-8");

		// An unlisted directory borrowing an existing worktree's admin dir
		const rogueWt = path.join(root, "rogue-wt");
		fs.mkdirSync(path.join(rogueWt, "sub"), { recursive: true });
		fs.writeFileSync(
			path.join(rogueWt, ".git"),
			`gitdir: ${path.join(listed, ".git", "worktrees", "wt-listed")}\n`,
			"utf-8",
		);

		expect(isProjectPathTrusted(rogueMain, agentDir)).toBe(false);
		expect(isProjectPathTrusted(path.join(rogueMain, "sub"), agentDir)).toBe(false);
		expect(isProjectPathTrusted(rogueWt, agentDir)).toBe(false);
		expect(isProjectPathTrusted(path.join(rogueWt, "sub"), agentDir)).toBe(false);
	});

	it("fails closed on a missing, malformed, or relative-only list", () => {
		const root = makeRoot("omp-trust-bad-");
		const agentDir = path.join(root, "agent");
		const project = path.join(root, "project");
		fs.mkdirSync(project, { recursive: true });

		// No file at all.
		expect(readTrustedProjectPaths(agentDir)).toEqual([]);
		expect(isProjectPathTrusted(project, agentDir)).toBe(false);

		// Unparseable content.
		fs.mkdirSync(agentDir, { recursive: true });
		fs.writeFileSync(trustedProjectsPath(agentDir), "{ not json", "utf-8");
		expect(readTrustedProjectPaths(agentDir)).toEqual([]);
		expect(isProjectPathTrusted(project, agentDir)).toBe(false);

		// Invalid or missing version field.
		for (const invalidVersion of [undefined, 2, "1", null, 0]) {
			fs.writeFileSync(
				trustedProjectsPath(agentDir),
				JSON.stringify({ version: invalidVersion, paths: [project] }),
				"utf-8",
			);
			expect(readTrustedProjectPaths(agentDir)).toEqual([]);
			expect(isProjectPathTrusted(project, agentDir)).toBe(false);
		}

		// Wrong shape: `paths` absent.
		fs.writeFileSync(trustedProjectsPath(agentDir), JSON.stringify({ version: 1 }), "utf-8");
		expect(readTrustedProjectPaths(agentDir)).toEqual([]);

		// Relative and non-string entries are dropped; the absolute entry survives.
		fs.writeFileSync(
			trustedProjectsPath(agentDir),
			JSON.stringify({ version: 1, paths: ["relative/dir", 42, project] }),
			"utf-8",
		);
		expect(readTrustedProjectPaths(agentDir)).toEqual([fs.realpathSync.native(project)]);
		expect(isProjectPathTrusted(project, agentDir)).toBe(true);
		expect(isProjectPathTrusted(path.join(root, "relative", "dir"), agentDir)).toBe(false);
	});
});
