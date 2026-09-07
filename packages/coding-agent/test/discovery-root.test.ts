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
	ancestorSkill: boolean;
	workspaceRead: string;
}

const temporaryRoots: string[] = [];

afterEach(async () => {
	await Promise.all(temporaryRoots.splice(0).map(root => fs.rm(root, { recursive: true, force: true })));
});

async function writeConfiguration(root: string, origin: string, thinking: string): Promise<void> {
	const config = path.join(root, ".omp");
	const plugins = path.join(config, "plugins");
	const plugin = path.join(plugins, "node_modules", "root-plugin");
	await Promise.all([
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
	await Promise.all([
		fs.mkdir(path.join(root, ".git")),
		fs.mkdir(home),
		fs.mkdir(empty),
		writeConfiguration(root, "ancestor", "low"),
		writeConfiguration(approved, "approved", "high"),
		writeConfiguration(candidate, "candidate", "minimal"),
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
			ancestorSkill: false,
		});
		expect(result.workspaceRead).toContain("candidate workspace contents");
	}, 20000);
});
