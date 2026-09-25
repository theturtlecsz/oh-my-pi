import { describe, expect, it } from "bun:test";
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { generateInventory } from "../../../scripts/cpk0-inventory";

const repoRoot = path.resolve(import.meta.dir, "../../..");
const inventoryPath = path.join(repoRoot, "docs/cpk0/surface-inventory.json");

describe("CPK-0 surface inventory (OMP-287)", () => {
	it("committed surface-inventory.json matches generateInventory() output exactly", () => {
		expect(fs.existsSync(inventoryPath)).toBe(true);
		const committedRaw = fs.readFileSync(inventoryPath, "utf8");
		const generated = generateInventory(repoRoot);

		// Structural equality
		expect(JSON.parse(committedRaw)).toEqual(generated);

		// Exact byte formatting (prevent whitespace drift)
		const expectedFormatted = `${JSON.stringify(generated, null, 2)}\n`;
		expect(committedRaw).toBe(expectedFormatted);
	}, 30000);

	it("bun scripts/cpk0-inventory.ts --check succeeds with exit code 0", () => {
		const res = spawnSync("bun", ["scripts/cpk0-inventory.ts", "--check"], {
			cwd: repoRoot,
			encoding: "utf8",
		});
		expect(res.status).toBe(0);
		expect(res.stdout).toContain("CPK-0 surface inventory check passed");
	}, 30000);

	describe("tool inventory completeness", () => {
		it("enumerates all first-party tools including custom and injected tools", () => {
			const inventory = JSON.parse(fs.readFileSync(inventoryPath, "utf8"));
			const toolsByName = new Map(inventory.tools.map((t: { name: string }) => [t.name, t]));

			// Injected custom tools
			expect(toolsByName.has("generate_image")).toBe(true);
			const genImg = toolsByName.get("generate_image")!;
			expect(genImg.source_file).toBe("packages/coding-agent/src/tools/image-gen.ts");
			expect(genImg.approval_tier).toBe("write");

			expect(toolsByName.has("tts")).toBe(true);
			const tts = toolsByName.get("tts")!;
			expect(tts.source_file).toBe("packages/coding-agent/src/tools/tts.ts");
			expect(tts.approval_tier).toBe("write");

			// Core built-in tools
			const expectedBuiltins = [
				"read",
				"bash",
				"edit",
				"ast_grep",
				"ast_edit",
				"ask",
				"debug",
				"eval",
				"github",
				"glob",
				"grep",
				"lsp",
				"inspect_image",
				"browser",
				"computer",
				"checkpoint",
				"rewind",
				"security_scan",
				"task",
				"hub",
				"todo",
				"web_search",
				"write",
				"memory_edit",
				"retain",
				"recall",
				"reflect",
				"learn",
				"manage_skill",
			];
			for (const name of expectedBuiltins) {
				expect(toolsByName.has(name)).toBe(true);
			}

			// Hidden tools
			for (const name of ["think", "yield", "goal"]) {
				expect(toolsByName.has(name)).toBe(true);
			}

			// Ephemeral vibe tools
			for (const name of ["vibe_spawn", "vibe_send", "vibe_wait", "vibe_kill", "vibe_list"]) {
				expect(toolsByName.has(name)).toBe(true);
			}

			// Advisor & session-system tools
			expect(toolsByName.has("advise")).toBe(true);
			expect(toolsByName.has("work")).toBe(true);

			// Every tool has required fields
			for (const tool of inventory.tools) {
				expect(tool.name).toBeString();
				expect(tool.name.length).toBeGreaterThan(0);
				expect(tool.source_file).toBeString();
				expect(fs.existsSync(path.join(repoRoot, tool.source_file))).toBe(true);
				expect(["read", "write", "exec", "dynamic"]).toContain(tool.approval_tier);
				expect(tool.tiers.length).toBeGreaterThan(0);
				for (const tier of tool.tiers) {
					expect(["read", "write", "exec"]).toContain(tier);
				}
			}
		});
	});

	describe("extension and hook surfaces", () => {
		it("includes ExtensionUIContext with its declared members", () => {
			const inventory = JSON.parse(fs.readFileSync(inventoryPath, "utf8"));
			const extIfaces = inventory.extension_surfaces.extensions.interfaces;
			const extUIContext = extIfaces.find((i: { name: string }) => i.name === "ExtensionUIContext");

			expect(extUIContext).toBeDefined();
			expect(extUIContext.source_file).toBe("packages/coding-agent/src/extensibility/extensions/types.ts");
			expect(extUIContext.members.length).toBeGreaterThanOrEqual(20);

			const memberNames = extUIContext.members.map((m: { name: string }) => m.name);
			expect(memberNames).toContain("select");
			expect(memberNames).toContain("confirm");
			expect(memberNames).toContain("input");
			expect(memberNames).toContain("notify");
			expect(memberNames).toContain("custom");
			expect(memberNames).toContain("editor");
			expect(memberNames).toContain("theme");
			expect(memberNames).toContain("setStatus");
		});

		it("includes HookUIContext with its declared members", () => {
			const inventory = JSON.parse(fs.readFileSync(inventoryPath, "utf8"));
			const hookIfaces = inventory.extension_surfaces.hooks.interfaces;
			const hookUIContext = hookIfaces.find((i: { name: string }) => i.name === "HookUIContext");

			expect(hookUIContext).toBeDefined();
			expect(hookUIContext.source_file).toBe("packages/coding-agent/src/extensibility/hooks/types.ts");
			expect(hookUIContext.members.length).toBeGreaterThanOrEqual(10);

			const memberNames = hookUIContext.members.map((m: { name: string }) => m.name);
			expect(memberNames).toContain("select");
			expect(memberNames).toContain("confirm");
			expect(memberNames).toContain("input");
			expect(memberNames).toContain("notify");
			expect(memberNames).toContain("custom");
			expect(memberNames).toContain("editor");
			expect(memberNames).toContain("theme");
			expect(memberNames).toContain("setStatus");
		});

		it("exports ExtensionAPI and HookAPI with registration and action members", () => {
			const inventory = JSON.parse(fs.readFileSync(inventoryPath, "utf8"));
			const extIfaces = inventory.extension_surfaces.extensions.interfaces;
			const extApi = extIfaces.find((i: { name: string }) => i.name === "ExtensionAPI");
			expect(extApi).toBeDefined();

			const hookIfaces = inventory.extension_surfaces.hooks.interfaces;
			const hookApi = hookIfaces.find((i: { name: string }) => i.name === "HookAPI");
			expect(hookApi).toBeDefined();
		});
	});

	describe("authority crossings classification", () => {
		it("covers all required capability crossings with authority and gate descriptions", () => {
			const inventory = JSON.parse(fs.readFileSync(inventoryPath, "utf8"));
			const crossings = inventory.authority_crossings;
			const crossingMap = new Map(crossings.map((c: { capability: string }) => [c.capability, c]));

			const requiredCapabilities = [
				"command_registration",
				"tool_registration",
				"execution",
				"messaging",
				"timers",
				"filesystem_facing_apis",
				"tool_invocation",
			];

			for (const cap of requiredCapabilities) {
				expect(crossingMap.has(cap)).toBe(true);
				const entry = crossingMap.get(cap)!;
				expect(entry.authority_granted).toBeString();
				expect(entry.authority_granted.length).toBeGreaterThan(20);
				expect(entry.gate).toBeString();
				expect(entry.gate.length).toBeGreaterThan(10);
				expect(entry.methods.length).toBeGreaterThan(0);
				expect(entry.gate_type).toBeString();
			}
		});
	});
});
