import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentTool } from "@oh-my-pi/pi-agent-core";
import { createScopedTools, pathAllowed, type ScopedTools } from "../src/scoped-tools";

let base: string;
let root: string;
let outside: string;
let aborts: string[];
let scoped: ScopedTools;

async function call(tool: AgentTool<any>, params: Record<string, string>): Promise<string> {
	const result = await tool.execute("id", params);
	return result.content.map(block => (block.type === "text" ? block.text : "")).join("");
}
const write = (p: string, content = "new") => call(scoped.tools[1], { path: p, content });
const read = (p: string) => call(scoped.tools[0], { path: p });
const text = (p: string) => Bun.file(p).text();

beforeEach(async () => {
	base = await fs.mkdtemp(path.join(os.tmpdir(), "scoped-tools-"));
	root = path.join(base, "wt");
	outside = path.join(base, "outside");
	await fs.mkdir(path.join(root, "src"), { recursive: true });
	await fs.mkdir(path.join(root, ".git"), { recursive: true });
	await fs.mkdir(outside);
	await Bun.write(path.join(root, "src/a.py"), "old");
	await Bun.write(path.join(root, ".git/config"), "git-old");
	await Bun.write(path.join(outside, "secret"), "secret-old");
	aborts = [];
	scoped = createScopedTools({ root, allowedPaths: ["src/*.py", "docs/**/*.md"], abort: r => aborts.push(r) });
});

afterEach(async () => {
	await fs.rm(base, { recursive: true, force: true });
});

describe("scoped tools", () => {
	it("exposes only read and write", () => {
		expect(scoped.tools.map(t => t.name)).toEqual(["read", "write"]);
	});

	it("writes permitted existing and new files, creating parents", async () => {
		await write("src/a.py", "changed");
		await write("docs/x/y/z.md", "doc");
		expect(await text(path.join(root, "src/a.py"))).toBe("changed");
		expect(await text(path.join(root, "docs/x/y/z.md"))).toBe("doc");
		expect(await read("src/a.py")).toContain("changed");
		expect(scoped.state.failed).toBe(false);
		expect(aborts).toEqual([]);
	});

	it("matches globs per segment, never by prefix", () => {
		const allowed = ["src/*.py", "docs/**/*.md", "lib/a?.[ch]"];
		expect(pathAllowed("src/a.py", allowed)).toBe(true);
		expect(pathAllowed("src/sub/a.py", allowed)).toBe(false);
		expect(pathAllowed("src", allowed)).toBe(false);
		expect(pathAllowed("src/a.pyc", allowed)).toBe(false);
		expect(pathAllowed("docs/a.md", allowed)).toBe(true);
		expect(pathAllowed("docs/a/b/c.md", allowed)).toBe(true);
		expect(pathAllowed("lib/ab.c", allowed)).toBe(true);
		expect(pathAllowed("lib/abc.c", allowed)).toBe(false);
		expect(pathAllowed("src2/a.py", allowed)).toBe(false);
		expect(pathAllowed("src/a.py", ["src"])).toBe(false);
	});

	const refusals: Array<[string, () => string, string?]> = [
		["outside allowed_paths", () => "other.txt"],
		["prefix-only match", () => "src/deep/a.py"],
		["traversal", () => "src/../../outside/secret"],
		["absolute path", () => path.join(outside, "secret")],
		[".git metadata", () => ".git/config"],
		["xd:// transport", () => "xd://device/x"],
		["url", () => "https://example.com/a.py"],
	];
	for (const [label, target] of refusals) {
		it(`refuses ${label} and leaves forbidden bytes unchanged`, async () => {
			await expect(write(target())).rejects.toThrow();
			expect(scoped.state.failed).toBe(true);
			expect(aborts).toHaveLength(1);
			expect(await text(path.join(outside, "secret"))).toBe("secret-old");
			expect(await text(path.join(root, ".git/config"))).toBe("git-old");
			expect(await text(path.join(root, "src/a.py"))).toBe("old");
		});
	}

	it("refuses symlink parent and symlink target", async () => {
		await fs.symlink(outside, path.join(root, "docs"));
		await expect(write("docs/new.md")).rejects.toThrow();
		expect(await fs.readdir(outside)).toEqual(["secret"]);

		const second = createScopedTools({ root, allowedPaths: ["src/*.py"], abort: r => aborts.push(r) });
		await fs.symlink(path.join(outside, "secret"), path.join(root, "src/link.py"));
		await expect(call(second.tools[1], { path: "src/link.py", content: "x" })).rejects.toThrow();
		expect(await text(path.join(outside, "secret"))).toBe("secret-old");
		expect(second.state.failed).toBe(true);
	});

	it("refuses a hardlinked target without mutating the other link", async () => {
		await fs.link(path.join(outside, "secret"), path.join(root, "src/hard.py"));
		await expect(write("src/hard.py", "x")).rejects.toThrow();
		expect(await text(path.join(outside, "secret"))).toBe("secret-old");
		expect(scoped.state.failed).toBe(true);
	});

	it("refuses reads outside the worktree", async () => {
		await fs.symlink(path.join(outside, "secret"), path.join(root, "src/leak"));
		await expect(read("src/leak")).rejects.toThrow();
		expect(scoped.state.failed).toBe(true);
	});

	it("keeps refusal fatal after a later valid call", async () => {
		await expect(write("other.txt")).rejects.toThrow();
		await expect(write("src/a.py", "late")).rejects.toThrow(/failed/);
		await expect(read("src/a.py")).rejects.toThrow(/failed/);
		expect(await text(path.join(root, "src/a.py"))).toBe("old");
		expect(scoped.state.failed).toBe(true);
		expect(aborts).toHaveLength(1);
	});

	it("does not treat a missing file read as a refusal", async () => {
		await expect(read("src/missing.py")).rejects.toThrow(/not found/);
		expect(scoped.state.failed).toBe(false);
	});
});
