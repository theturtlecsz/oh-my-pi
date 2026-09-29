import { describe, expect, test } from "bun:test";
import { readRepoText, unresolvedLinks } from "./fixtures/programme-claims";

const ADR_REL = "docs/adr/0005-mission-first-architecture.md";

const SECTION_HEADING = "Unattended safety envelope (D35)";

const P =
	'I would not make "unattended" synonymous with "fully autonomous." The right model is unattended operation inside a pre-approved safety envelope.';

const D40 =
	"D40: D35 supersedes D16's classes where they conflict; an action no tier lists is tier 3.";

/** Tier action lists, held as data, in document order. */
const EXPECTED_TIERS: { heading: string; bullets: string[] }[] = [
	{
		heading: "Tier 1: autonomous",
		bullets: [
			"read repository/project state",
			"create isolated worktrees",
			"modify files inside the approved repository/path envelope",
			"run tests, linters, builds, static analysis",
			"create commits on isolated branches",
			"perform research",
			"create/update internal artifacts",
			"retry/recover workers",
			"reroute models/providers",
			"pause/resume work",
			"update mission state",
			"emit events",
			"produce candidate changes for review",
		],
	},
	{
		heading: "Tier 2: standing policy",
		bullets: [
			"push branches to approved repositories",
			"create pull requests",
			"update non-production external systems",
			"spend beyond a defined mission/project budget threshold",
			"perform bounded network access required by the mission",
			"create or delete disposable cloud/dev resources",
		],
	},
	{
		heading: "Tier 3: explicit high-risk authorization",
		bullets: [
			"merge to protected/default branches",
			"production deployment",
			"destructive infrastructure changes",
			"credential/security-policy changes",
			"deleting persistent data",
			"modifying billing/payment/account ownership",
			"publishing externally as you",
			"broadening repository/project scope",
			"accessing secrets outside the approved mission envelope",
			"disabling safety, audit, or verification mechanisms",
		],
	},
];

function extractSection(content: string, heading: string): string {
	const lines = content.split("\n");
	const startIndex = lines.findIndex(line => line.trim() === `## ${heading}`);
	if (startIndex === -1) {
		throw new Error(`Section "## ${heading}" not found`);
	}
	const sectionLines: string[] = [];
	for (let i = startIndex + 1; i < lines.length; i++) {
		if (/^##\s+/.test(lines[i])) break;
		sectionLines.push(lines[i]);
	}
	return sectionLines.join("\n");
}

/** Blockquote body of a section, one string per `>` paragraph. */
function blockquoteText(sectionContent: string): string {
	return sectionContent
		.split("\n")
		.filter(line => line.startsWith(">"))
		.map(line => line.replace(/^>\s?/, ""))
		.join("\n");
}

/** Bullets under each `###` heading, in document order. */
function parseTierBullets(sectionContent: string): { heading: string; bullets: string[] }[] {
	const tiers: { heading: string; bullets: string[] }[] = [];
	let current: { heading: string; bullets: string[] } | null = null;

	for (const line of sectionContent.split("\n")) {
		const heading = line.match(/^###\s+(.+)$/);
		if (heading) {
			current = { heading: heading[1], bullets: [] };
			tiers.push(current);
			continue;
		}
		const bullet = line.match(/^-\s+(.+)$/);
		if (bullet && current) current.bullets.push(bullet[1]);
	}

	return tiers;
}

describe("ADR 0005: Unattended safety envelope (D35)", () => {
	test("section follows Run Owner invariants and holds P as a blockquote", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, SECTION_HEADING);

		expect(content.indexOf(`## ${SECTION_HEADING}`)).toBeGreaterThan(
			content.indexOf("## Run Owner invariants"),
		);
		expect(blockquoteText(section)).toBe(P);
	});

	test("each tier's bullets equal its list in order", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, SECTION_HEADING);
		const tiers = parseTierBullets(section);

		expect(tiers.map(tier => tier.heading)).toEqual(EXPECTED_TIERS.map(tier => tier.heading));
		expect(tiers.map(tier => tier.bullets)).toEqual(EXPECTED_TIERS.map(tier => tier.bullets));
		expect(tiers.map(tier => tier.bullets.length)).toEqual([13, 6, 10]);
	});

	test("last line is D40 verbatim", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, SECTION_HEADING);
		const lines = section.split("\n").filter(line => line.trim() !== "");

		expect(lines[lines.length - 1]).toBe(D40);
	});

	test("unresolvedLinks is empty", async () => {
		const content = await readRepoText(ADR_REL);
		expect(unresolvedLinks(ADR_REL, content)).toEqual([]);
	});
});
