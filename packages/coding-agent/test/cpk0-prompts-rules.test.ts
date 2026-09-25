import { describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { generateInventory, type SurfaceInventory } from "../../../scripts/cpk0-inventory";

const repoRoot = path.resolve(import.meta.dir, "../../..");
const inventoryPath = path.join(repoRoot, "docs/cpk0/surface-inventory.json");

describe("CPK-0 prompts and rule paths inventory (OMP-204-s02)", () => {
	const committed = JSON.parse(fs.readFileSync(inventoryPath, "utf8")) as SurfaceInventory;

	it("prompts equals independent in-test Bun.Glob scan, is sorted, and has no duplicates", () => {
		const independentPrompts = Array.from(
			new Bun.Glob("packages/coding-agent/src/**/*.md").scanSync({
				cwd: repoRoot,
				onlyFiles: true,
				dot: false,
			}),
		)
			.map(p => p.replaceAll("\\", "/"))
			.sort();

		expect(committed.prompts).toEqual(independentPrompts);
		expect(committed.prompts).toContain("packages/coding-agent/src/prompts/advisor/system.md");
		expect(new Set(committed.prompts).size).toBe(committed.prompts.length);
		expect(committed.prompts).toEqual([...committed.prompts].sort());
	});

	it("rule_paths session_system contains required hooks, rules, skills, and extensions", () => {
		const { session_system } = committed.rule_paths;
		expect(session_system.hooks).toContain("session-system/hooks/task-observer-first-tool.mjs");
		expect(session_system.rules).toContain("session-system/rules/work-plan.md");
		expect(session_system.skills).toContain("session-system/skills/task-observer/SKILL.md");
		expect(session_system.extensions).toContain("session-system/extensions/workflow/work.ts");
	});

	it("every advisor entry is in prompts", () => {
		const { advisor } = committed.rule_paths;
		expect(advisor.length).toBeGreaterThan(0);
		for (const entry of advisor) {
			expect(committed.prompts).toContain(entry);
		}
	});

	it("generateInventory() produces matching prompts and rule_paths", () => {
		const generated = generateInventory(repoRoot);
		expect(generated.prompts).toEqual(committed.prompts);
		expect(generated.rule_paths).toEqual(committed.rule_paths);
	}, 30000);
});
