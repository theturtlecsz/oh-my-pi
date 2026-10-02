import { describe, expect, test } from "bun:test";
import { readRepoText } from "./fixtures/programme-claims";

const DOCTRINE_REL = "session-system/agents/omp-AGENTS.md";
const D58_HEADING = "## Safety walls, not questions (D58, owner ruling 2026-10-02)";
const MCP_HEADING = "# MCP — gotchas only";

const FIVE_WALLS = [
	"protected main",
	"separate credentials for automation and merge",
	"hard budget ceilings",
	"the stop button",
	"restricted Linux user",
] as const;

function normalizeWs(text: string): string {
	return text.replace(/\s+/g, " ").trim();
}

describe("doctrine D58 safety walls", () => {
	test("the heading occurs once and lies before '# MCP — gotchas only'", async () => {
		const doc = await readRepoText(DOCTRINE_REL);
		const headingMatches = [...doc.matchAll(/^## Safety walls, not questions \(D58, owner ruling 2026-10-02\)$/gm)];
		expect(headingMatches.length).toBe(1);

		const headingIndex = doc.indexOf(D58_HEADING);
		const mcpIndex = doc.indexOf(MCP_HEADING);
		expect(headingIndex).toBeGreaterThan(-1);
		expect(mcpIndex).toBeGreaterThan(-1);
		expect(headingIndex).toBeLessThan(mcpIndex);
	});

	test("section body has items 1-4 in order with required phrases", async () => {
		const doc = await readRepoText(DOCTRINE_REL);
		const headingIndex = doc.indexOf(D58_HEADING);
		expect(headingIndex).toBeGreaterThan(-1);

		const afterHeading = doc.slice(headingIndex + D58_HEADING.length);
		const nextHeadingMatch = afterHeading.search(/\n#+ /);
		const sectionBody = nextHeadingMatch === -1 ? afterHeading : afterHeading.slice(0, nextHeadingMatch);

		const itemMatches = [...sectionBody.matchAll(/(?:^|\n)(\d+)\.\s+([\s\S]*?)(?=(?:\n\d+\.|$))/g)];
		expect(itemMatches.length).toBe(4);

		const itemNumbers = itemMatches.map(m => Number(m[1]));
		expect(itemNumbers).toEqual([1, 2, 3, 4]);

		const items = itemMatches.map(m => normalizeWs(m[2]));

		// item 1 has "D35" and "nothing stops to ask a person"
		expect(items[0]).toContain("D35");
		expect(items[0]).toContain("nothing stops to ask a person");

		// item 2 has all five walls and "OMP-402"
		for (const wall of FIVE_WALLS) {
			expect(items[1]).toContain(wall);
		}
		expect(items[1]).toContain("OMP-402");

		// item 3 has "not run as trusted code"
		expect(items[2]).toContain("not run as trusted code");

		// item 4 has "adds a wall or a limit" and "declined"
		expect(items[3]).toContain("adds a wall or a limit");
		expect(items[3]).toContain("declined");
	});
});
