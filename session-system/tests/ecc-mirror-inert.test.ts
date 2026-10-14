import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { setAgentDir } from "@oh-my-pi/pi-utils";
import { clearCache } from "../../packages/coding-agent/src/capability/fs";
import { type ContextFile, contextFileCapability } from "../../packages/coding-agent/src/capability/context-file";
import { type Extension, extensionCapability } from "../../packages/coding-agent/src/capability/extension";
import { type Hook, hookCapability } from "../../packages/coding-agent/src/capability/hook";
import { type MCPServer, mcpCapability } from "../../packages/coding-agent/src/capability/mcp";
import { type Prompt, promptCapability } from "../../packages/coding-agent/src/capability/prompt";
import { type Rule, ruleCapability } from "../../packages/coding-agent/src/capability/rule";
import { type Skill, skillCapability } from "../../packages/coding-agent/src/capability/skill";
import { type SlashCommand, slashCommandCapability } from "../../packages/coding-agent/src/capability/slash-command";
import { type CustomTool, toolCapability } from "../../packages/coding-agent/src/capability/tool";
import type { SourceMeta } from "../../packages/coding-agent/src/capability/types";
import { loadCapability } from "../../packages/coding-agent/src/discovery";

const repoRoot = path.resolve(import.meta.dir, "../..");
const eccDir = path.resolve(import.meta.dir, "../ecc");
const mirrorDir = path.join(eccDir, "mirror");

const tempDirs: string[] = [];
let originalAgentDir: string | undefined;

beforeEach(async () => {
	originalAgentDir = process.env.PI_CODING_AGENT_DIR;
	setAgentDir(await makeTempDir("ecc-mirror-inert-agent-"));
	clearCache();
});

afterEach(async () => {
	if (originalAgentDir !== undefined) setAgentDir(originalAgentDir);
	else delete process.env.PI_CODING_AGENT_DIR;
	clearCache();
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
});

async function makeTempDir(prefix: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

async function makeProject(): Promise<string> {
	const dir = await makeTempDir("ecc-mirror-inert-project-");
	await fs.mkdir(path.join(dir, ".git"), { recursive: true });
	return dir;
}

/** Absolute paths from an item's own path field and its source metadata. */
function located(...candidates: Array<string | undefined>): string[] {
	const paths: string[] = [];
	for (const candidate of candidates) {
		if (typeof candidate !== "string" || candidate.length === 0) continue;
		const resolved = path.resolve(candidate);
		if (!paths.includes(resolved)) paths.push(resolved);
	}
	return paths;
}

/**
 * True when `candidate` is `root` or a path inside it.
 * `path.relative` rejects a sibling whose name only shares the root's prefix.
 */
function resolvesUnder(root: string, candidate: string): boolean {
	const relative = path.relative(path.resolve(root), path.resolve(candidate));
	return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

/** Source paths that the negative assertion rejects. */
function pathsUnder(root: string, paths: readonly string[]): string[] {
	return paths.filter(candidate => resolvesUnder(root, candidate));
}

interface Discovery {
	paths: string[];
	contextFiles: Array<ContextFile & { _source: SourceMeta }>;
}

/**
 * Load every capability the mirror could activate.
 * Path fields, from each item type: rule.path, skill.path, slash-command.path,
 * hook.path, context-file.path, extension.path, tool.path, prompt.path.
 * An MCP server has no path of its own; its source file is `_source.path`.
 */
async function discover(cwd: string): Promise<Discovery> {
	clearCache();
	const [rules, skills, commands, hooks, contextFiles, mcps, extensions, tools, prompts] = await Promise.all([
		loadCapability<Rule>(ruleCapability.id, { cwd }),
		loadCapability<Skill>(skillCapability.id, { cwd }),
		loadCapability<SlashCommand>(slashCommandCapability.id, { cwd }),
		loadCapability<Hook>(hookCapability.id, { cwd }),
		loadCapability<ContextFile>(contextFileCapability.id, { cwd }),
		loadCapability<MCPServer>(mcpCapability.id, { cwd }),
		loadCapability<Extension>(extensionCapability.id, { cwd }),
		loadCapability<CustomTool>(toolCapability.id, { cwd }),
		loadCapability<Prompt>(promptCapability.id, { cwd }),
	]);

	const paths = [
		...rules.all.flatMap(item => located(item.path, item._source.path)),
		...skills.all.flatMap(item => located(item.path, item._source.path)),
		...commands.all.flatMap(item => located(item.path, item._source.path)),
		...hooks.all.flatMap(item => located(item.path, item._source.path)),
		...contextFiles.all.flatMap(item => located(item.path, item._source.path)),
		...mcps.all.flatMap(item => located(item._source.path)),
		...extensions.all.flatMap(item => located(item.path, item._source.path)),
		...tools.all.flatMap(item => located(item.path, item._source.path)),
		...prompts.all.flatMap(item => located(item.path, item._source.path)),
	];
	return { paths, contextFiles: contextFiles.items };
}

describe("vendored ecc mirror stays inert", () => {
	test("discovery from the repo root and from session-system/ecc reads nothing under the mirror", async () => {
		for (const cwd of [repoRoot, eccDir]) {
			const discovered = await discover(cwd);
			expect(pathsUnder(mirrorDir, discovered.paths)).toEqual([]);
		}
	});

	test("a project that copies mirror AGENTS.md is discovered as that context file", async () => {
		const project = await makeProject();
		const agentsCopy = path.join(project, "AGENTS.md");
		await fs.copyFile(path.join(mirrorDir, "AGENTS.md"), agentsCopy);
		await fs.copyFile(path.join(mirrorDir, ".mcp.json"), path.join(project, ".mcp.json"));

		const discovered = await discover(project);
		const copied = path.resolve(agentsCopy);
		expect(discovered.contextFiles.some(file => path.resolve(file.path) === copied)).toBe(true);
		// Same path match as the negative case. Pointed at this project, that assertion fails.
		expect(pathsUnder(project, discovered.paths)).toContain(copied);
	});
});
