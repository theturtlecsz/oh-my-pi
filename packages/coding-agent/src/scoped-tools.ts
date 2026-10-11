/**
 * Scoped local-file tools for an orchestrator-driven agent run.
 *
 * `createScopedTools` returns exactly two tools, `read` and `write`, bound to a
 * root the trusted host supplies. Both go through one authorization path
 * (`#authorize`). Any refusal flips a permanent failed state and aborts the run
 * through the supplied callback; nothing a later call does can clear it.
 *
 * `allowedPaths` carry the `candidate_git.path_allowed` semantics of the Python
 * orchestrator: repository-relative globs matched per path segment (`*`, `?`,
 * `[..]` stay inside one segment, only an explicit `**` segment spans several),
 * never by prefix.
 */
import type { Stats } from "node:fs";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { type } from "@oh-my-pi/omptype";
import type { AgentTool, AgentToolResult } from "@oh-my-pi/pi-agent-core";
import { isEnoent, prompt } from "@oh-my-pi/pi-utils";
import orchestratorToolsPrompt from "./prompts/orchestrator-tools.md" with { type: "text" };
import { decodeUtf8Text, formatTextWithMode } from "./tools/read-format";
import { toolResult } from "./tools/tool-result";

const MAX_READ_BYTES = 1024 * 1024;
const URL_SCHEME_RE = /^[A-Za-z][A-Za-z0-9+.-]*:/;

const readSchema = type({
	path: type("string").describe("worktree-relative file path"),
});

const writeSchema = type({
	path: type("string").describe("worktree-relative file path"),
	content: type("string").describe("complete new file content"),
});

/** Raised for a policy refusal; every other failure is an ordinary tool error. */
export class ScopedToolRefusal extends Error {
	constructor(message: string) {
		super(message);
		this.name = "ScopedToolRefusal";
	}
}

/** Sticky failure record shared by both tools. */
export class ScopedToolsState {
	#reason: string | undefined;

	get failed(): boolean {
		return this.#reason !== undefined;
	}

	/** First refusal message, or `undefined` while no refusal happened. */
	get reason(): string | undefined {
		return this.#reason;
	}

	/** Record a refusal. Returns `true` only for the first one. */
	fail(reason: string): boolean {
		if (this.#reason !== undefined) return false;
		this.#reason = reason;
		return true;
	}
}

export interface ScopedToolsOptions {
	/** Absolute worktree root, supplied by the trusted host. */
	root: string;
	/** Repository-relative segment globs a write may touch. */
	allowedPaths: readonly string[];
	/** Called once, on the first refusal, to abort the agent run. */
	abort: (reason: string) => void;
}

export interface ScopedTools {
	tools: [AgentTool<typeof readSchema>, AgentTool<typeof writeSchema>];
	state: ScopedToolsState;
}

function splitSegments(value: string): string[] {
	return value.split("/").filter(segment => segment !== "" && segment !== ".");
}

function matchSegments(pathSegments: readonly string[], patSegments: readonly string[]): boolean {
	const n = pathSegments.length;
	const m = patSegments.length;
	const dp: boolean[][] = Array.from({ length: n + 1 }, () => Array.from({ length: m + 1 }, () => false));
	dp[n][m] = true;
	for (let j = m - 1; j >= 0 && patSegments[j] === "**"; j--) dp[n][j] = dp[n][j + 1];
	for (let i = n - 1; i >= 0; i--) {
		for (let j = m - 1; j >= 0; j--) {
			dp[i][j] =
				patSegments[j] === "**"
					? dp[i][j + 1] || dp[i + 1][j]
					: new Bun.Glob(patSegments[j]).match(pathSegments[i]) && dp[i + 1][j + 1];
		}
	}
	return dp[0][0];
}

/** TypeScript twin of `candidate_git.path_allowed`. */
export function pathAllowed(relPath: string, allowedPaths: readonly string[]): boolean {
	const segments = splitSegments(relPath);
	return allowedPaths.some(pattern => matchSegments(segments, splitSegments(pattern)));
}

interface Authorized {
	/** Root-relative POSIX path, normalized. */
	rel: string;
	segments: string[];
	/** Real worktree root. */
	realRoot: string;
}

class ScopedToolSet {
	readonly state = new ScopedToolsState();
	readonly #root: string;
	readonly #allowedPaths: readonly string[];
	readonly #abort: (reason: string) => void;

	constructor(options: ScopedToolsOptions) {
		this.#root = path.resolve(options.root);
		this.#allowedPaths = [...options.allowedPaths];
		this.#abort = options.abort;
	}

	/** Refuse: record permanently, abort the run once, and fail the call. */
	#refuse(message: string): never {
		if (this.state.fail(message)) this.#abort(message);
		throw new ScopedToolRefusal(message);
	}

	/** Run one tool call; a refusal anywhere inside it is fatal, a prior one blocks it outright. */
	async run<T>(op: () => Promise<T>): Promise<T> {
		if (this.state.failed) throw new ScopedToolRefusal(`scoped tools failed: ${this.state.reason}`);
		try {
			return await op();
		} catch (error) {
			if (error instanceof ScopedToolRefusal) this.#refuse(error.message);
			throw error;
		}
	}

	/** Lexical checks shared by reads and writes. */
	async #authorize(rawPath: string, mode: "read" | "write"): Promise<Authorized> {
		if (rawPath === "" || rawPath.includes("\0")) this.#refuse(`${mode}: invalid path`);
		if (URL_SCHEME_RE.test(rawPath)) this.#refuse(`${mode}: URL or device transport refused: ${rawPath}`);
		if (path.isAbsolute(rawPath) || rawPath.startsWith("~"))
			this.#refuse(`${mode}: path must be relative: ${rawPath}`);
		const rel = path.posix.normalize(rawPath);
		const segments = splitSegments(rel);
		if (segments.length === 0 || segments[0] === "..") this.#refuse(`${mode}: path escapes the worktree: ${rawPath}`);
		if (segments.some(segment => segment.toLowerCase() === ".git")) this.#refuse(`${mode}: .git metadata refused`);
		if (mode === "write" && !pathAllowed(segments.join("/"), this.#allowedPaths)) {
			this.#refuse(`write: path outside allowed_paths: ${segments.join("/")}`);
		}
		let realRoot: string;
		try {
			realRoot = await fs.realpath(this.#root);
		} catch {
			this.#refuse(`${mode}: worktree root unavailable`);
		}
		return { rel: segments.join("/"), segments, realRoot };
	}

	async read(rawPath: string): Promise<AgentToolResult> {
		const { rel, realRoot } = await this.#authorize(rawPath, "read");
		const target = path.join(realRoot, rel);
		let real: string;
		try {
			real = await fs.realpath(target);
		} catch (error) {
			if (isEnoent(error)) throw new Error(`File not found: ${rel}`);
			throw error;
		}
		const inside = path.relative(realRoot, real);
		if (inside === "" || inside.startsWith("..") || path.isAbsolute(inside)) {
			this.#refuse(`read: resolves outside the worktree: ${rel}`);
		}
		if (splitSegments(inside).some(segment => segment.toLowerCase() === ".git")) {
			this.#refuse("read: .git metadata refused");
		}
		const stat = await fs.stat(real);
		if (!stat.isFile()) this.#refuse(`read: not a regular file: ${rel}`);
		if (stat.size > MAX_READ_BYTES) throw new Error(`File too large to read: ${rel}`);
		const text = decodeUtf8Text(new Uint8Array(await fs.readFile(real)));
		if (text === null) throw new Error(`Not a UTF-8 text file: ${rel}`);
		return toolResult()
			.text(formatTextWithMode(text, 1, false, true))
			.done();
	}

	async write(rawPath: string, content: string): Promise<AgentToolResult> {
		const { rel, segments, realRoot } = await this.#authorize(rawPath, "write");
		// Walk from the real root, vetting every existing component with lstat
		// before anything is created: a symlink at any depth is refused.
		let current = realRoot;
		const missing: string[] = [];
		for (const [index, segment] of segments.entries()) {
			const next = path.join(current, segment);
			const isLast = index === segments.length - 1;
			let stat: Stats;
			try {
				stat = await fs.lstat(next);
			} catch (error) {
				if (!isEnoent(error)) throw error;
				missing.push(...segments.slice(index));
				break;
			}
			if (stat.isSymbolicLink()) this.#refuse(`write: symlink refused: ${segments.slice(0, index + 1).join("/")}`);
			if (isLast) {
				if (!stat.isFile()) this.#refuse(`write: target is not a regular file: ${rel}`);
				if (stat.nlink > 1) this.#refuse(`write: hardlinked target refused: ${rel}`);
			} else if (!stat.isDirectory()) {
				this.#refuse(`write: parent is not a directory: ${segments.slice(0, index + 1).join("/")}`);
			}
			current = next;
		}
		// Create missing parents one level at a time (non-recursive mkdir fails on a
		// racing symlink instead of following it), then swap the file in by rename
		// so another hardlink to the old inode is never written through.
		for (const segment of missing.slice(0, -1)) {
			current = path.join(current, segment);
			await fs.mkdir(current);
		}
		const target = path.join(realRoot, rel);
		const existing = await fs.lstat(target).catch((error: unknown) => {
			if (isEnoent(error)) return null;
			throw error;
		});
		const tmp = path.join(
			path.dirname(target),
			`.${path.basename(target)}.scoped-${process.pid}-${Bun.randomUUIDv7()}`,
		);
		try {
			// "wx" is O_EXCL: fails on any pre-existing name, symlinks included.
			await fs.writeFile(tmp, content, { flag: "wx", mode: existing ? existing.mode & 0o777 : 0o644 });
			await fs.rename(tmp, target);
		} catch (error) {
			await fs.rm(tmp, { force: true }).catch(() => {});
			throw error;
		}
		return toolResult()
			.text(`Wrote ${Buffer.byteLength(content)} bytes to ${rel}`)
			.done();
	}
}

/** Build the scoped `read`/`write` pair and the sticky refusal state they share. */
export function createScopedTools(options: ScopedToolsOptions): ScopedTools {
	const set = new ScopedToolSet(options);
	const readTool: AgentTool<typeof readSchema> = {
		name: "read",
		label: "Read",
		description: prompt.render(orchestratorToolsPrompt, { read: true }),
		parameters: readSchema,
		strict: true,
		approval: "read",
		execute: (_id, params) => set.run(() => set.read(params.path)),
	};
	const writeTool: AgentTool<typeof writeSchema> = {
		name: "write",
		label: "Write",
		description: prompt.render(orchestratorToolsPrompt, { write: true, allowedPaths: options.allowedPaths }),
		parameters: writeSchema,
		strict: true,
		approval: "write",
		execute: (_id, params) => set.run(() => set.write(params.path, params.content)),
	};
	return { tools: [readTool, writeTool], state: set.state };
}
