#!/usr/bin/env bun
/**
 * scripts/cpk0-inventory.ts — Standing CPK-0 surface inventory generator (OMP-287).
 *
 * Generates docs/cpk0/surface-inventory.json by statically analyzing the source code:
 * 1. Tools: every first-party tool the coding agent can register (builtin, hidden,
 *    custom tools like generate_image and tts, ephemeral vibe tools, advisor tools,
 *    and session-system workflow tools), with name, source file, and approval tier.
 * 2. Extension and hook surfaces: every exported interface/type in
 *    packages/coding-agent/src/extensibility (extensions and hooks), including
 *    ExtensionUIContext and HookUIContext, with their declared members.
 * 3. Authority crossings: classification for each extension/hook capability
 *    (command/tool registration, execution, messaging, timers, filesystem-facing APIs,
 *    tool invocation), detailing the authority granted and guarding gate.
 *
 * Usage:
 *   bun scripts/cpk0-inventory.ts [--write] [--check] [--stdout]
 */

import * as fs from "node:fs";
import * as path from "node:path";
import { Node, Project, SyntaxKind } from "ts-morph";

export interface ToolInventoryEntry {
	name: string;
	source_file: string;
	approval_tier: string;
	dynamic: boolean;
	tiers: string[];
	kind: "builtin" | "hidden" | "custom" | "vibe" | "advisor" | "session_system";
	description?: string;
}

export interface MemberInventoryEntry {
	name: string;
	kind: "property" | "method" | "call_signature" | "index_signature" | "unknown";
	optional: boolean;
	parameters?: string;
	return_type?: string;
	type?: string;
}

export interface TypeSurfaceEntry {
	name: string;
	kind: "interface" | "type";
	source_file: string;
	extends?: string[];
	members: MemberInventoryEntry[];
	definition?: string;
}

export interface SubsystemSurfaces {
	interface_count: number;
	type_count: number;
	interfaces: TypeSurfaceEntry[];
	types: TypeSurfaceEntry[];
}

export interface AuthorityCrossingEntry {
	capability: string;
	label: string;
	description: string;
	methods: string[];
	authority_granted: string;
	gate: string;
	gate_type: string;
}

export interface RulePathsInventory {
	advisor: string[];
	session_system: {
		hooks: string[];
		rules: string[];
		extensions: string[];
		agents: string[];
		skills: string[];
	};
}

export interface SurfaceInventory {
	schema_version: string;
	task: string;
	description: string;
	tools: ToolInventoryEntry[];
	extension_surfaces: {
		extensions: SubsystemSurfaces;
		hooks: SubsystemSurfaces;
	};
	authority_crossings: AuthorityCrossingEntry[];
	prompts: string[];
	rule_paths: RulePathsInventory;
}

function resolveFilePath(baseDir: string, relPath: string): string {
	const p = path.normalize(path.join(baseDir, relPath));
	if (fs.existsSync(`${p}.ts`)) return `${p}.ts`;
	if (fs.existsSync(path.join(p, "index.ts"))) return path.join(p, "index.ts");
	if (fs.existsSync(p)) return p;
	return p;
}

export function extractApprovalFromNode(approvalProp: Node | undefined): {
	approval_tier: string;
	dynamic: boolean;
	tiers: string[];
} {
	if (!approvalProp) {
		return { approval_tier: "exec", dynamic: false, tiers: ["exec"] };
	}
	let init: Node = approvalProp;
	if (Node.isPropertyDeclaration(approvalProp) || Node.isPropertyAssignment(approvalProp)) {
		init = approvalProp.getInitializer() ?? approvalProp;
	}
	if (Node.isStringLiteral(init)) {
		const val = init.getLiteralValue();
		return { approval_tier: val, dynamic: false, tiers: [val] };
	}
	if (Node.isAsExpression(init)) {
		const inner = init.getExpression();
		if (Node.isStringLiteral(inner)) {
			const val = inner.getLiteralValue();
			return { approval_tier: val, dynamic: false, tiers: [val] };
		}
	}
	if (Node.isPropertyDeclaration(approvalProp)) {
		const initNode = approvalProp.getInitializer();
		if (initNode && Node.isStringLiteral(initNode)) {
			const val = initNode.getLiteralValue();
			return { approval_tier: val, dynamic: false, tiers: [val] };
		}
	}

	let fnNode: Node = init;
	if (Node.isIdentifier(init)) {
		const defs = init.getDefinitions();
		if (defs.length > 0) {
			const d = defs[0].getDeclarationNode();
			if (d && (Node.isFunctionDeclaration(d) || Node.isVariableDeclaration(d))) {
				fnNode = d;
			}
		}
	}

	const foundTiers = new Set<string>();

	// Concise body arrow function, e.g. (args) => cond ? "write" : "read"
	if (Node.isArrowFunction(fnNode)) {
		const body = fnNode.getBody();
		if (Node.isConditionalExpression(body)) {
			const whenTrue = body.getWhenTrue();
			if (Node.isStringLiteral(whenTrue)) foundTiers.add(whenTrue.getLiteralValue());
			const whenFalse = body.getWhenFalse();
			if (Node.isStringLiteral(whenFalse)) foundTiers.add(whenFalse.getLiteralValue());
		} else if (Node.isStringLiteral(body)) {
			foundTiers.add(body.getLiteralValue());
		}
	}

	const returnStatements = fnNode.getDescendantsOfKind(SyntaxKind.ReturnStatement);
	for (const ret of returnStatements) {
		const expr = ret.getExpression();
		if (!expr) continue;
		if (Node.isStringLiteral(expr)) {
			foundTiers.add(expr.getLiteralValue());
		} else if (Node.isConditionalExpression(expr)) {
			const whenTrue = expr.getWhenTrue();
			if (Node.isStringLiteral(whenTrue)) foundTiers.add(whenTrue.getLiteralValue());
			const whenFalse = expr.getWhenFalse();
			if (Node.isStringLiteral(whenFalse)) foundTiers.add(whenFalse.getLiteralValue());
		} else if (Node.isObjectLiteralExpression(expr)) {
			const tierProp = expr.getProperty("tier");
			if (tierProp && Node.isPropertyAssignment(tierProp)) {
				const val = tierProp.getInitializer();
				if (val && Node.isStringLiteral(val)) {
					foundTiers.add(val.getLiteralValue());
				}
			}
		}
	}

	const tiers = Array.from(foundTiers).sort();
	if (tiers.length === 0) tiers.push("exec");
	return { approval_tier: "dynamic", dynamic: true, tiers };
}

function extractMember(m: Node): MemberInventoryEntry {
	if (Node.isPropertySignature(m)) {
		return {
			name: m.getName(),
			kind: "property",
			optional: m.hasQuestionToken(),
			type: m.getTypeNode()?.getText() || m.getType().getText(),
		};
	}
	if (Node.isMethodSignature(m)) {
		const params = m
			.getParameters()
			.map(p => p.getText())
			.join(", ");
		const returnType = m.getReturnTypeNode()?.getText() || m.getReturnType().getText();
		return {
			name: m.getName(),
			kind: "method",
			optional: m.hasQuestionToken(),
			parameters: params,
			return_type: returnType,
		};
	}
	if (Node.isCallSignatureDeclaration(m)) {
		const params = m
			.getParameters()
			.map(p => p.getText())
			.join(", ");
		const returnType = m.getReturnTypeNode()?.getText() || m.getReturnType().getText();
		return {
			name: "<call>",
			kind: "call_signature",
			optional: false,
			parameters: params,
			return_type: returnType,
		};
	}
	if (Node.isIndexSignatureDeclaration(m)) {
		return {
			name: "<index>",
			kind: "index_signature",
			optional: false,
			type: m.getReturnTypeNode()?.getText() || m.getReturnType().getText(),
		};
	}
	return {
		name: m.getText().split("\n")[0] ?? "",
		kind: "unknown",
		optional: false,
	};
}

export function scanGlob(pattern: string, repoRoot: string): string[] {
	const glob = new Bun.Glob(pattern);
	const scanned = Array.from(glob.scanSync({ cwd: repoRoot, onlyFiles: true, dot: false }));
	const normalized = scanned.map(p => p.replaceAll("\\", "/"));
	return Array.from(new Set(normalized)).sort();
}

export function generateInventory(repoRoot: string = process.cwd()): SurfaceInventory {
	const project = new Project({
		skipAddingFilesFromTsConfig: true,
		skipFileDependencyResolution: true,
	});

	// ---------------------------------------------------------------------------
	// 1. Tool Surface Discovery
	// ---------------------------------------------------------------------------
	const toolsMap = new Map<string, ToolInventoryEntry>();

	// Parse packages/coding-agent/src/tools/index.ts
	const indexRelPath = "packages/coding-agent/src/tools/index.ts";
	const indexAbsPath = path.join(repoRoot, indexRelPath);
	const indexSf = project.addSourceFileAtPath(indexAbsPath);

	const importMap = new Map<string, string>();
	for (const imp of indexSf.getImportDeclarations()) {
		const modSpec = imp.getModuleSpecifierValue();
		const resolved = resolveFilePath(path.join(repoRoot, "packages/coding-agent/src/tools"), modSpec);
		for (const named of imp.getNamedImports()) {
			importMap.set(named.getName(), path.relative(repoRoot, resolved));
		}
	}

	function processToolMap(varName: string, kind: "builtin" | "hidden") {
		const varDecl = indexSf.getVariableDeclaration(varName);
		const obj = varDecl?.getInitializer();
		if (!obj || !Node.isObjectLiteralExpression(obj)) return;

		for (const prop of obj.getProperties()) {
			if (!Node.isPropertyAssignment(prop)) continue;
			const toolName = prop.getName();
			const init = prop.getInitializer();
			if (!init) continue;

			const idents = init.getDescendantsOfKind(SyntaxKind.Identifier);
			let targetClass = "";
			for (const id of idents) {
				const name = id.getText();
				if (name.endsWith("Tool") && importMap.has(name)) {
					targetClass = name;
					break;
				}
			}

			let filePath = targetClass ? importMap.get(targetClass)! : "";
			let targetSf = project.addSourceFileAtPath(path.join(repoRoot, filePath));
			let cls = targetSf.getClass(targetClass);
			if (!cls && filePath.endsWith("lsp/index.ts")) {
				filePath = "packages/coding-agent/src/lsp/tool.ts";
				targetSf = project.addSourceFileAtPath(path.join(repoRoot, filePath));
				cls = targetSf.getClass(targetClass);
			}

			const approvalProp = cls?.getProperty("approval");
			const approvalInfo = extractApprovalFromNode(approvalProp);

			const descProp = cls?.getProperty("description") ?? cls?.getProperty("summary");
			let descText: string | undefined;
			if (descProp && (Node.isPropertyDeclaration(descProp) || Node.isPropertyAssignment(descProp))) {
				const descInit = descProp.getInitializer();
				if (descInit && Node.isStringLiteral(descInit)) {
					descText = descInit.getLiteralValue();
				}
			}

			toolsMap.set(toolName, {
				name: toolName,
				source_file: filePath,
				approval_tier: approvalInfo.approval_tier,
				dynamic: approvalInfo.dynamic,
				tiers: approvalInfo.tiers,
				kind,
				...(descText ? { description: descText } : {}),
			});
		}
	}

	processToolMap("BUILTIN_TOOLS", "builtin");
	processToolMap("HIDDEN_TOOLS", "hidden");

	// Image generation tool (packages/coding-agent/src/tools/image-gen.ts)
	const imgRelPath = "packages/coding-agent/src/tools/image-gen.ts";
	const imgSf = project.addSourceFileAtPath(path.join(repoRoot, imgRelPath));
	const imgVar = imgSf.getVariableDeclaration("imageGenTool")?.getInitializer();
	if (imgVar && Node.isObjectLiteralExpression(imgVar)) {
		const approval = extractApprovalFromNode(imgVar.getProperty("approval"));
		toolsMap.set("generate_image", {
			name: "generate_image",
			source_file: imgRelPath,
			approval_tier: approval.approval_tier,
			dynamic: approval.dynamic,
			tiers: approval.tiers,
			kind: "custom",
		});
	}

	// Speech generation / TTS tool (packages/coding-agent/src/tools/tts.ts)
	const ttsRelPath = "packages/coding-agent/src/tools/tts.ts";
	const ttsSf = project.addSourceFileAtPath(path.join(repoRoot, ttsRelPath));
	const ttsVar = ttsSf.getVariableDeclaration("ttsTool")?.getInitializer();
	if (ttsVar && Node.isObjectLiteralExpression(ttsVar)) {
		const approval = extractApprovalFromNode(ttsVar.getProperty("approval"));
		toolsMap.set("tts", {
			name: "tts",
			source_file: ttsRelPath,
			approval_tier: approval.approval_tier,
			dynamic: approval.dynamic,
			tiers: approval.tiers,
			kind: "custom",
		});
	}

	// Ephemeral Vibe tools (packages/coding-agent/src/tools/vibe.ts)
	const vibeRelPath = "packages/coding-agent/src/tools/vibe.ts";
	const vibeSf = project.addSourceFileAtPath(path.join(repoRoot, vibeRelPath));
	const vibeClasses: [string, string][] = [
		["vibe_spawn", "VibeSpawnTool"],
		["vibe_send", "VibeSendTool"],
		["vibe_wait", "VibeWaitTool"],
		["vibe_kill", "VibeKillTool"],
		["vibe_list", "VibeListTool"],
	];
	for (const [toolName, clsName] of vibeClasses) {
		const cls = vibeSf.getClass(clsName);
		const approval = extractApprovalFromNode(cls?.getProperty("approval"));
		toolsMap.set(toolName, {
			name: toolName,
			source_file: vibeRelPath,
			approval_tier: approval.approval_tier,
			dynamic: approval.dynamic,
			tiers: approval.tiers,
			kind: "vibe",
		});
	}

	// Advisor tool (packages/coding-agent/src/advisor/advise-tool.ts)
	const advRelPath = "packages/coding-agent/src/advisor/advise-tool.ts";
	const advSf = project.addSourceFileAtPath(path.join(repoRoot, advRelPath));
	const advCls = advSf.getClass("AdviseTool");
	const advApproval = extractApprovalFromNode(advCls?.getProperty("approval"));
	toolsMap.set("advise", {
		name: "advise",
		source_file: advRelPath,
		approval_tier: advApproval.approval_tier,
		dynamic: advApproval.dynamic,
		tiers: advApproval.tiers,
		kind: "advisor",
	});

	// Session-system workflow tool (session-system/extensions/workflow/host.ts)
	const workRelPath = "session-system/extensions/workflow/host.ts";
	toolsMap.set("work", {
		name: "work",
		source_file: workRelPath,
		approval_tier: "exec",
		dynamic: false,
		tiers: ["exec"],
		kind: "session_system",
	});

	const tools = Array.from(toolsMap.values()).sort((a, b) => a.name.localeCompare(b.name));

	// ---------------------------------------------------------------------------
	// 2. Extension and Hook Surface Discovery
	// ---------------------------------------------------------------------------
	function collectSubsystemSurfaces(subsystemDirRel: string): SubsystemSurfaces {
		const absDir = path.join(repoRoot, subsystemDirRel);
		const files = fs
			.readdirSync(absDir, { recursive: true, withFileTypes: true })
			.filter(dirent => dirent.isFile() && dirent.name.endsWith(".ts"))
			.map(dirent => {
				const full = path.join(dirent.parentPath || absDir, dirent.name);
				return path.relative(repoRoot, full);
			})
			.sort();

		const ifacesMap = new Map<string, TypeSurfaceEntry>();
		const typesMap = new Map<string, TypeSurfaceEntry>();

		for (const relFile of files) {
			const sf = project.addSourceFileAtPath(path.join(repoRoot, relFile));

			for (const iface of sf.getInterfaces()) {
				if (!iface.isExported()) continue;
				const name = iface.getName();
				if (ifacesMap.has(name)) continue;

				const extendsList = iface.getExtends().map(e => e.getText());
				const members = iface.getMembers().map(extractMember);

				ifacesMap.set(name, {
					name,
					kind: "interface",
					source_file: relFile,
					...(extendsList.length > 0 ? { extends: extendsList } : {}),
					members,
				});
			}

			for (const t of sf.getTypeAliases()) {
				if (!t.isExported()) continue;
				const name = t.getName();
				if (typesMap.has(name)) continue;

				const typeNode = t.getTypeNode();
				let members: MemberInventoryEntry[] = [];
				if (typeNode && Node.isTypeLiteral(typeNode)) {
					members = typeNode.getMembers().map(extractMember);
				}

				typesMap.set(name, {
					name,
					kind: "type",
					source_file: relFile,
					members,
					definition: typeNode?.getText() || t.getType().getText(),
				});
			}
		}

		const interfaces = Array.from(ifacesMap.values()).sort((a, b) => a.name.localeCompare(b.name));
		const types = Array.from(typesMap.values()).sort((a, b) => a.name.localeCompare(b.name));

		return {
			interface_count: interfaces.length,
			type_count: types.length,
			interfaces,
			types,
		};
	}

	const extensionsSurfaces = collectSubsystemSurfaces("packages/coding-agent/src/extensibility/extensions");
	const hooksSurfaces = collectSubsystemSurfaces("packages/coding-agent/src/extensibility/hooks");

	// ---------------------------------------------------------------------------
	// 3. Authority Crossings Classification
	// ---------------------------------------------------------------------------
	const authorityCrossings: AuthorityCrossingEntry[] = [
		{
			capability: "command_registration",
			label: "Command Registration",
			description: "Register slash commands (/command) into the session or user interface",
			methods: ["ExtensionAPI.registerCommand", "HookAPI.registerCommand"],
			authority_granted:
				"Allows extensions and hooks to register custom slash commands (/command-name) that execute arbitrary TypeScript code with host process privileges when triggered by the user or session.",
			gate: "User invocation gate: slash commands are inert until explicitly triggered by user input in the chat/CLI or via session slash command dispatch; command names are validated to prevent accidental namespace collisions.",
			gate_type: "user_invocation",
		},
		{
			capability: "tool_registration",
			label: "Tool Registration",
			description: "Register LLM-callable tools into the agent's tool set",
			methods: [
				"ExtensionAPI.registerTool",
				"ExtensionAPI.registerFileWriteFallback",
				"ExtensionAPI.registerFileDeleteFallback",
			],
			authority_granted:
				"Allows extensions to register new tools into the agent's model-visible toolset, making them callable by the LLM during autonomous agent turns, or register fallback handlers for denied write/delete operations.",
			gate: "Tool approval policy gate (tools.approvalMode and tools.approval.<tool>). In non-yolo modes ('always-ask' and 'write'), write and exec tier executions prompt the user for interactive confirmation. Tool arguments are validated against declared schemas before execution.",
			gate_type: "approval_policy",
		},
		{
			capability: "execution",
			label: "Process Execution",
			description: "Direct child process and shell command execution",
			methods: ["ExtensionAPI.exec", "HookAPI.exec"],
			authority_granted:
				"Allows extensions and hooks to spawn arbitrary child processes and execute shell commands directly on the host operating system with the user's host OS permissions.",
			gate: "None (ungated): extension code executes commands directly on the host operating system without prompting the user or running inside an OS sandbox.",
			gate_type: "none",
		},
		{
			capability: "messaging",
			label: "Session Messaging & Journal",
			description: "Inject messages into conversation history or persist custom entries to the session journal",
			methods: [
				"ExtensionAPI.sendMessage",
				"ExtensionAPI.sendUserMessage",
				"ExtensionAPI.appendEntry",
				"ExtensionAPI.deliverMessage",
				"HookAPI.sendMessage",
				"HookAPI.appendEntry",
			],
			authority_granted:
				"Allows injecting synthetic assistant or user messages into the active context (influencing agent reasoning and autonomous behavior), triggering new model turns, and persisting custom metadata into the session journal.",
			gate: "Session lifecycle and schema validation gate: calling messaging APIs during extension load throws ExtensionRuntimeNotInitializedError; messages must follow session entry schemas and retain attribution tags.",
			gate_type: "session_lifecycle",
		},
		{
			capability: "timers",
			label: "Managed Timers & Scheduling",
			description: "Schedule delayed or periodic background execution on the event loop",
			methods: [
				"ExtensionAPI.setTimeout",
				"ExtensionAPI.setInterval",
				"ExtensionAPI.clearTimeout",
				"ExtensionAPI.clearInterval",
				"ExtensionContext.setTimeout",
				"ExtensionContext.setInterval",
				"ExtensionContext.clearTimer",
			],
			authority_granted:
				"Allows scheduling asynchronous deferred or recurring execution on the Node.js/Bun event loop.",
			gate: "Managed timer lifecycle gate: handlers catch and contain uncaught exceptions to prevent process crashes; timer handles are tracked by ManagedTimerTracker and automatically cancelled/unreferenced on session disposal or shutdown.",
			gate_type: "lifecycle_tracking",
		},
		{
			capability: "filesystem_facing_apis",
			label: "Filesystem Access",
			description: "Access to workspace working directory, session files, and disk paths",
			methods: [
				"ExtensionAPI.cwd",
				"ExtensionContext.cwd",
				"HookAPI.cwd",
				"HookContext.cwd",
				"ExtensionContext.sessionManager.getSessionFile",
			],
			authority_granted:
				"Exposes current workspace root and session file paths, enabling reading and writing of files on disk (via standard node:fs or exec).",
			gate: "Operating system file permissions gate: access is bounded by the POSIX user/group permissions of the host process; no kernel-level filesystem virtualization or chroot sandbox is applied.",
			gate_type: "os_permissions",
		},
		{
			capability: "tool_invocation",
			label: "Tool Invocation & Interception",
			description: "Intercept, inspect, modify, or block tool calls and results, or invoke tools",
			methods: [
				"ExtensionAPI.on('tool_call')",
				"ExtensionAPI.on('tool_result')",
				"HookAPI.on('tool_call')",
				"HookAPI.on('tool_result')",
				"ExtensionContext.invokeTool",
			],
			authority_granted:
				"Allows intercepting all agent tool calls before execution (with authority to modify input arguments, substitute results, or block execution via { block: true, reason }) and after execution. invokeTool allows delegating execution to the native implementation of a shadowed built-in tool.",
			gate: "Interception pipeline gate & delegation boundary: tool_call handlers execute in registration order and can halt execution; invokeTool only permits same-tool delegation (cannot invoke arbitrary unapproved tools), inherits call approval, and is recursion-depth-guarded.",
			gate_type: "pipeline_gate",
		},
	];

	// ---------------------------------------------------------------------------
	// 4. Prompts and Rule Paths Discovery
	// ---------------------------------------------------------------------------
	const prompts = scanGlob("packages/coding-agent/src/**/*.md", repoRoot);
	const advisor = prompts.filter(p => p.includes("src/prompts/advisor/"));
	const hooks = scanGlob("session-system/hooks/**/*.mjs", repoRoot);
	const rules = scanGlob("session-system/rules/**/*.md", repoRoot);
	const extensions = scanGlob("session-system/extensions/**/*.{ts,md}", repoRoot);
	const agents = scanGlob("session-system/agents/**/*.md", repoRoot);
	const skills = scanGlob("session-system/skills/*/SKILL.md", repoRoot);

	return {
		schema_version: "cpk0/v1",
		task: "OMP-287",
		description:
			"CPK-0 surface inventory: first-party tools, exported extension and hook surfaces, and authority classifications",
		tools,
		extension_surfaces: {
			extensions: extensionsSurfaces,
			hooks: hooksSurfaces,
		},
		authority_crossings: authorityCrossings,
		prompts,
		rule_paths: {
			advisor,
			session_system: {
				hooks,
				rules,
				extensions,
				agents,
				skills,
			},
		},
	};
}

async function main() {
	const args = process.argv.slice(2);
	const repoRoot = process.cwd();
	const outputPath = path.join(repoRoot, "docs/cpk0/surface-inventory.json");

	const inventory = generateInventory(repoRoot);
	const jsonText = `${JSON.stringify(inventory, null, 2)}\n`;

	if (args.includes("--stdout")) {
		process.stdout.write(jsonText);
		return;
	}

	if (args.includes("--check")) {
		if (!fs.existsSync(outputPath)) {
			console.error(`Check failed: ${outputPath} does not exist. Run bun scripts/cpk0-inventory.ts to generate it.`);
			process.exit(1);
		}
		const existing = fs.readFileSync(outputPath, "utf8");
		if (existing !== jsonText) {
			console.error(
				`Check failed: ${outputPath} is stale or differs from script output. Run bun scripts/cpk0-inventory.ts to regenerate it.`,
			);
			process.exit(1);
		}
		console.log(`CPK-0 surface inventory check passed: ${outputPath} is up to date.`);
		return;
	}

	// Default or --write
	fs.mkdirSync(path.dirname(outputPath), { recursive: true });
	fs.writeFileSync(outputPath, jsonText, "utf8");
	console.log(`Generated CPK-0 surface inventory -> ${outputPath}`);
	console.log(
		`  Tools: ${inventory.tools.length}, Extension Interfaces: ${inventory.extension_surfaces.extensions.interface_count}, Hook Interfaces: ${inventory.extension_surfaces.hooks.interface_count}, Authority Crossings: ${inventory.authority_crossings.length}`,
	);
}

if (import.meta.main) {
	try {
		await main();
	} catch (err) {
		console.error(err instanceof Error ? (err.stack ?? err.message) : String(err));
		process.exit(1);
	}
}
