import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

interface DiscoveryProbe {
	cwd: string;
	settingsCwd: string;
	auditModel: string | null;
	thinking: string;
	agent: string | null;
	extension: string | null;
	plugin: string | null;
	skill: string | null;
	command: string | null;
	prompt: string | null;
	advisorModel: string | null;
	advisorBaseline: string | null;
	lspCommand: string | null;
	dapCommand: string | null;
	claudePluginBeforeRefresh: boolean;
	claudePluginAfterRefresh: boolean;
	ancestorSkill: boolean;
	workspaceRead: string;
}

const temporaryRoots: string[] = [];

afterEach(async () => {
	await Promise.all(temporaryRoots.splice(0).map(root => fs.rm(root, { recursive: true, force: true })));
});

async function writeConfiguration(root: string, origin: string, thinking: string): Promise<void> {
	await fs.mkdir(root, { recursive: true });
	const config = path.join(root, ".omp");
	const plugins = path.join(config, "plugins");
	const plugin = path.join(plugins, "node_modules", "root-plugin");
	const executable = path.join(root, "discovery-server");
	await Promise.all([
		fs.symlink(process.execPath, executable),
		Bun.write(
			path.join(root, "lsp.json"),
			JSON.stringify({
				servers: {
					"root-lsp": { command: executable, fileTypes: [".fixture"], rootMarkers: [".discovery-workspace"] },
				},
			}),
		),
		Bun.write(
			path.join(root, "dap.json"),
			JSON.stringify({
				adapters: {
					"root-dap": { command: executable, fileTypes: [".fixture"], rootMarkers: [".discovery-workspace"] },
				},
			}),
		),
		Bun.write(path.join(config, "config.yml"), `modelRoles:\n  audit: test/${origin}\n`),
		Bun.write(path.join(config, "settings.json"), JSON.stringify({ defaultThinkingLevel: thinking })),
		Bun.write(
			path.join(config, "agents", "root-agent.md"),
			`---\nname: root-agent\ndescription: ${origin}\n---\nDiscovery fixture.\n`,
		),
		Bun.write(
			path.join(config, "extensions", "root-extension.ts"),
			`export default function(pi) { pi.registerCommand("root-extension", { description: ${JSON.stringify(origin)}, handler: async () => {} }); }\n`,
		),
		Bun.write(path.join(plugins, "package.json"), JSON.stringify({ dependencies: { "root-plugin": "1.0.0" } })),
		Bun.write(
			path.join(plugin, "package.json"),
			JSON.stringify({ name: "root-plugin", version: "1.0.0", omp: { extensions: ["./index.ts"] } }),
		),
		Bun.write(
			path.join(plugin, "index.ts"),
			`export default function(pi) { pi.registerCommand("root-plugin", { description: ${JSON.stringify(origin)}, handler: async () => {} }); }\n`,
		),
		Bun.write(
			path.join(config, "skills", "root-skill", "SKILL.md"),
			`---\nname: root-skill\ndescription: ${origin}\n---\nDiscovery fixture.\n`,
		),
		Bun.write(
			path.join(config, "commands", "root-command.md"),
			`---\ndescription: ${origin}\n---\nDiscovery fixture.\n`,
		),
		Bun.write(
			path.join(config, "prompts", "root-prompt.md"),
			`---\ndescription: ${origin}\n---\n${origin} {{arguments}}\n`,
		),
		Bun.write(
			path.join(root, "WATCHDOG.yml"),
			`instructions: ${origin} baseline\nadvisors:\n  - name: root-advisor\n    model: test/${origin}\n`,
		),
		Bun.write(path.join(root, "workspace.txt"), `${origin} workspace contents\n`),
	]);
}

async function fixture() {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "omp-discovery-root-"));
	temporaryRoots.push(root);
	const approved = path.join(root, "approved");
	const candidate = path.join(root, "candidate");
	const empty = path.join(root, "empty");
	const home = path.join(root, "home");
	const claudePlugin = path.join(home, ".claude", "plugins", "cache", "resume-plugin");
	await Promise.all([
		fs.mkdir(path.join(root, ".git")),
		fs.mkdir(home, { recursive: true }),
		fs.mkdir(empty),
		writeConfiguration(root, "ancestor", "low"),
		writeConfiguration(approved, "approved", "high"),
		writeConfiguration(candidate, "candidate", "minimal"),
		Bun.write(path.join(candidate, ".discovery-workspace"), "Candidate workspace marker.\n"),
		Bun.write(
			path.join(home, ".claude", "settings.json"),
			JSON.stringify({ enabledPlugins: { "resume-plugin@fixture": false } }),
		),
		Bun.write(
			path.join(candidate, ".claude", "settings.json"),
			JSON.stringify({ enabledPlugins: { "resume-plugin@fixture": true } }),
		),
		Bun.write(
			path.join(home, ".claude", "plugins", "installed_plugins.json"),
			JSON.stringify({
				version: 2,
				plugins: { "resume-plugin@fixture": [{ scope: "user", installPath: claudePlugin, version: "1.0.0" }] },
			}),
		),
		Bun.write(
			path.join(claudePlugin, ".claude-plugin", "plugin.json"),
			JSON.stringify({ name: "resume-plugin", version: "1.0.0" }),
		),
		Bun.write(
			path.join(root, ".omp", "skills", "ancestor-only", "SKILL.md"),
			"---\nname: ancestor-only\ndescription: Ancestor discovery fixture.\n---\nFixture.\n",
		),
	]);
	return { root, approved, candidate, empty, home };
}

async function probe(candidate: string, home: string, discoveryRoot?: string): Promise<DiscoveryProbe> {
	const child = Bun.spawn([process.execPath, path.join(import.meta.dir, "fixtures", "discovery-root-probe.ts")], {
		cwd: candidate,
		env: {
			PATH: process.env.PATH ?? "",
			HOME: home,
			XDG_CONFIG_HOME: path.join(home, "config"),
			XDG_DATA_HOME: path.join(home, "data"),
			XDG_STATE_HOME: path.join(home, "state"),
			PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent"),
			...(discoveryRoot ? { OMP_DISCOVERY_CWD: discoveryRoot } : {}),
		},
		stdout: "pipe",
		stderr: "pipe",
		timeout: 15000,
	});
	const [stdout, stderr, exitCode] = await Promise.all([
		new Response(child.stdout).text(),
		new Response(child.stderr).text(),
		child.exited,
	]);
	expect(exitCode, stderr).toBe(0);
	return JSON.parse(stdout);
}

describe("host-owned discovery root", () => {
	test("approved configuration wins while file tools still read the candidate workspace", async () => {
		const paths = await fixture();
		const result = await probe(paths.candidate, paths.home, paths.approved);
		expect(result).toMatchObject({
			cwd: paths.candidate,
			settingsCwd: paths.candidate,
			auditModel: "test/approved",
			thinking: "high",
			agent: "approved",
			extension: "approved",
			plugin: "approved",
			skill: "approved",
			command: "approved",
			prompt: "approved probe",
			advisorModel: "test/approved",
			advisorBaseline: "approved baseline",
			lspCommand: path.join(paths.approved, "discovery-server"),
			dapCommand: path.join(paths.approved, "discovery-server"),
			claudePluginBeforeRefresh: false,
			claudePluginAfterRefresh: false,
			ancestorSkill: false,
		});
		expect(result.workspaceRead).toContain("candidate workspace contents");
		expect(result.workspaceRead).not.toContain("approved workspace contents");
	}, 20000);

	test("ordinary launches retain candidate configuration and ancestor skill discovery", async () => {
		const paths = await fixture();
		const result = await probe(paths.candidate, paths.home);
		expect(result).toMatchObject({
			auditModel: "test/candidate",
			thinking: "minimal",
			agent: "candidate",
			extension: "candidate",
			plugin: "candidate",
			skill: "candidate",
			command: "candidate",
			prompt: "candidate probe",
			advisorModel: "test/candidate",
			advisorBaseline: "ancestor baseline\n\ncandidate baseline",
			lspCommand: path.join(paths.candidate, "discovery-server"),
			dapCommand: path.join(paths.candidate, "discovery-server"),
			claudePluginBeforeRefresh: true,
			claudePluginAfterRefresh: true,
			ancestorSkill: true,
		});
		expect(result.workspaceRead).toContain("candidate workspace contents");
	}, 20000);

	test("an empty approved root cannot fall back to candidate or ancestor configuration", async () => {
		const paths = await fixture();
		const result = await probe(paths.candidate, paths.home, paths.empty);
		expect(result).toMatchObject({
			auditModel: null,
			agent: null,
			extension: null,
			plugin: null,
			skill: null,
			command: null,
			prompt: null,
			advisorModel: null,
			advisorBaseline: null,
			lspCommand: null,
			dapCommand: null,
			claudePluginBeforeRefresh: false,
			claudePluginAfterRefresh: false,
			ancestorSkill: false,
		});
		expect(result.workspaceRead).toContain("candidate workspace contents");
	}, 20000);
});
