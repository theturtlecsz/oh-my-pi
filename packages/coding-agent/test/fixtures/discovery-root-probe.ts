import * as os from "node:os";
import { discoverAdvisorConfigs } from "../../src/advisor/config";
import { expandPromptTemplate, loadPromptTemplates } from "../../src/config/prompt-templates";
import { Settings } from "../../src/config/settings";
import { selectLaunchAdapter } from "../../src/dap/config";
import { initializeWithSettings } from "../../src/discovery";
import { clearClaudePluginRootsCache, getPreloadedPluginRoots, preloadPluginRoots } from "../../src/discovery/helpers";
import { discoverAndLoadExtensions } from "../../src/extensibility/extensions/loader";
import { loadSkills } from "../../src/extensibility/skills";
import { loadSlashCommands } from "../../src/extensibility/slash-commands";
import { getServerForFile, loadConfig } from "../../src/lsp/config";
import { discoverAgents } from "../../src/task/discovery";
import { ReadTool } from "../../src/tools/read";

const cwd = process.cwd();
const settings = await Settings.loadReadOnly({ cwd });
initializeWithSettings(settings);
await preloadPluginRoots(os.homedir(), cwd);
const claudePluginBeforeRefresh = getPreloadedPluginRoots().some(plugin => plugin.id === "resume-plugin@fixture");
clearClaudePluginRootsCache();
await preloadPluginRoots(os.homedir(), cwd);
const claudePluginAfterRefresh = getPreloadedPluginRoots().some(plugin => plugin.id === "resume-plugin@fixture");
const [extensions, agents, skills, commands, prompts, advisors] = await Promise.all([
	discoverAndLoadExtensions([], cwd),
	discoverAgents(cwd),
	loadSkills({ cwd }),
	loadSlashCommands({ cwd }),
	loadPromptTemplates({ cwd }),
	discoverAdvisorConfigs(cwd),
]);
if (extensions.errors.length > 0) throw new Error(JSON.stringify(extensions.errors));
const lsp = getServerForFile(loadConfig(cwd), "workspace.fixture");
const dap = selectLaunchAdapter("workspace.fixture", cwd, "root-dap");

const read = await new ReadTool({
	cwd: settings.getCwd(),
	settings,
	hasUI: false,
	getSessionFile: () => null,
	getSessionSpawns: () => "*",
}).execute("workspace-read", { path: "workspace.txt" });

process.stdout.write(
	JSON.stringify({
		cwd: process.cwd(),
		settingsCwd: settings.getCwd(),
		auditModel: settings.getModelRole("audit") ?? null,
		thinking: settings.get("defaultThinkingLevel"),
		agent: agents.agents.find(agent => agent.name === "root-agent")?.description ?? null,
		extension:
			extensions.extensions
				.flatMap(extension => [...extension.commands.values()])
				.find(command => command.name === "root-extension")?.description ?? null,
		plugin:
			extensions.extensions
				.flatMap(extension => [...extension.commands.values()])
				.find(command => command.name === "root-plugin")?.description ?? null,
		skill: skills.skills.find(skill => skill.name === "root-skill")?.description ?? null,
		command: commands.find(command => command.name === "root-command")?.description ?? null,
		prompt: prompts.some(template => template.name === "root-prompt")
			? expandPromptTemplate("/root-prompt probe", prompts).trim()
			: null,
		advisorModel: advisors.advisors.find(advisor => advisor.name === "root-advisor")?.model ?? null,
		advisorBaseline: advisors.sharedInstructions ?? null,
		lspCommand: lsp?.[1].command ?? null,
		dapCommand: dap.kind === "adapter" ? dap.adapter.command : null,
		claudePluginBeforeRefresh,
		claudePluginAfterRefresh,
		ancestorSkill: skills.skills.some(skill => skill.name === "ancestor-only"),
		workspaceRead: read.content.flatMap(content => (content.type === "text" ? [content.text] : [])).join("\n"),
	}),
);
