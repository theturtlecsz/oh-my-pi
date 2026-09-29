import { describe, expect, test } from "bun:test";
import { readRepoText, unresolvedLinks } from "./fixtures/programme-claims";

const MASTER_REL = "MASTER.md";
const HEADING = "## Architecture mandate — September 28, 2026 (specification)";
const NEXT_HEADING = "## Programme decision amendment — September 18, 2026 (G1–G12)";
const ADR_LINK = "[ADR 0005](docs/adr/0005-mission-first-architecture.md)";
const COCKPIT_CLAIM_PATTERN = /grok ?bot|cockpit_verbs/i;

const REQUIREMENT_TITLES = [
	"Mission interface",
	"Automatic lifecycle",
	"Commands become internal operations",
	"OMP owns authoritative work state",
	"Durable execution",
	"Capability layer",
	"Worker routing inside OMP",
	"Context compilation",
	"Project as a first-class object",
	"Human-decision events",
	"Events and notifications",
	"Internal machinery invisible by default",
	"Fewer persistent agent roles",
	"Run Owner refactor",
	"Preserve advanced capabilities",
	"UX acceptance test",
	"Recovery acceptance test",
	"Replacement acceptance test",
	"Roadmap reconciliation",
] as const;

const REQUIREMENT_LINE = /^(\d+)\. (.+?) — (.+)$/;

function sectionLines(content: string): string[] {
	const lines = content.split("\n");
	const start = lines.findIndex(line => line === HEADING);
	expect(start).toBeGreaterThanOrEqual(0);
	const body: string[] = [];
	for (let i = start + 1; i < lines.length; i++) {
		if (lines[i].startsWith("## ")) break;
		body.push(lines[i]);
	}
	return body;
}

describe("MASTER.md architecture mandate", () => {
	test("the heading occurs once, after line 19, and the next ## heading is G1–G12", async () => {
		const content = await readRepoText(MASTER_REL);
		const lines = content.split("\n");
		const occurrences = lines.flatMap((line, index) => (line === HEADING ? [index] : []));

		expect(content.split(HEADING).length - 1).toBe(1);
		expect(occurrences).toHaveLength(1);
		// Line 19 is index 18. The mandate is inserted later; that line stays put.
		expect(occurrences[0]).toBeGreaterThan(18);

		const next = lines.slice(occurrences[0] + 1).find(line => line.startsWith("## "));
		expect(next).toBe(NEXT_HEADING);
	});

	test("Requirements lists items 1–19 with the mandated titles and a sentence each", async () => {
		const lines = sectionLines(await readRepoText(MASTER_REL));
		const requirementsAt = lines.findIndex(line => line === "### Requirements");
		expect(requirementsAt).toBeGreaterThanOrEqual(0);
		expect(lines.filter(line => line === "### Requirements")).toHaveLength(1);

		const items: { n: number; title: string; sentence: string }[] = [];
		for (const line of lines.slice(requirementsAt + 1)) {
			if (line.startsWith("### ")) break;
			const match = REQUIREMENT_LINE.exec(line);
			if (!match) continue;
			items.push({ n: Number(match[1]), title: match[2], sentence: match[3].trim() });
		}

		expect(items.map(item => item.n)).toEqual(REQUIREMENT_TITLES.map((_, index) => index + 1));
		expect(items.map(item => item.title)).toEqual([...REQUIREMENT_TITLES]);
		for (const item of items) {
			expect(item.sentence.length).toBeGreaterThan(0);
			expect(item.sentence.endsWith(".")).toBe(true);
			expect(item.sentence.slice(0, -1)).not.toContain(".");
		}
	});

	test("the section names the Work Ledger, OMP-411 and the ADR 0005 link", async () => {
		const section = [HEADING, ...sectionLines(await readRepoText(MASTER_REL))].join("\n");
		expect(section).toContain("Work Ledger");
		expect(section).toContain("OMP-411");
		expect(section).toContain(`Decisions: ${ADR_LINK}.`);
		expect(section).toContain("sophisticated inside, invisible outside");
		expect(section).toContain("No OMP plumbing in normal use");
	});

	test("unresolvedLinks for the section is empty", async () => {
		const section = [HEADING, ...sectionLines(await readRepoText(MASTER_REL))].join("\n");
		expect(unresolvedLinks(MASTER_REL, section)).toEqual([]);
	});

	test("no section line matches the cockpit claim pattern or contains PASS", async () => {
		const lines = [HEADING, ...sectionLines(await readRepoText(MASTER_REL))];
		const hits = lines.filter(line => COCKPIT_CLAIM_PATTERN.test(line) || line.includes("PASS"));
		expect(hits).toEqual([]);
	});
});
