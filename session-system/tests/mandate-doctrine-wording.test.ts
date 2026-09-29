import { describe, expect, test } from "bun:test";
import { readRepoText } from "./fixtures/programme-claims";

const PROPOSAL_REL = "docs/programme/DOCTRINE-WORDING-OMP-412.md";
const DOCTRINE_REL = "session-system/agents/omp-AGENTS.md";

const SECTION_TITLES = [
	"Intake routing",
	"Routine ledger self-confirmation",
	"Close asymmetry part 2",
	"Autonomous execution authority",
] as const;

/** Retired gate language that must not survive into a Proposed block. */
const RETIRED_PROPOSED_PHRASES = [
	"literal /summary",
	"explicit-command gates",
	"Everything else stays visibly owner-confirmed",
];

/** Same sweep programme-claims-adr0004 uses. A wording proposal must not trip it. */
const COCKPIT_CLAIM_PATTERN = /grok ?bot|cockpit_verbs/i;

function normalizeWs(text: string): string {
	return text.replace(/\s+/g, " ").trim();
}

function sectionBodies(markdown: string): Map<string, string> {
	const parts = markdown.split(/^## /m).slice(1);
	const bodies = new Map<string, string>();
	for (const part of parts) {
		const breakAt = part.indexOf("\n");
		const title = (breakAt === -1 ? part : part.slice(0, breakAt)).trim();
		const body = breakAt === -1 ? "" : part.slice(breakAt + 1);
		bodies.set(title, body);
	}
	return bodies;
}

function labeledFence(body: string, label: "Current" | "Proposed"): string {
	const match = body.match(new RegExp(String.raw`(?:^|\n)${label}:[ \t]*\n+` + "```[^\\n]*\\n([\\s\\S]*?)\\n?```"));
	expect(match, `${label} fence`).not.toBeNull();
	return match![1];
}

describe("mandate doctrine wording proposal", () => {
	test("intro is an unapplied proposal and cites the ADR as code, plus the mandate sources", async () => {
		const doc = await readRepoText(PROPOSAL_REL);
		const intro = doc.split(/^## /m)[0];

		expect(intro.toLowerCase()).toContain("unapplied proposal");
		expect(intro).toContain("implementing `docs/adr/0005-mission-first-architecture.md`");
		expect(intro).not.toMatch(/\[[^\]]*\]\([^)]*0005-mission-first-architecture\.md[^)]*\)/);
		expect(intro).toContain("/home/thetu/master-report/MANDATE-RECONCILIATION.md");
		expect(intro).toContain("(C1, C2, C4, C6)");
		expect(intro).toContain("DECISIONS.md");
	});

	test("exactly four sections, in order, each with a Why line of decision ids", async () => {
		const doc = await readRepoText(PROPOSAL_REL);
		const headings = [...doc.matchAll(/^## (.+)$/gm)].map(match => match[1].trim());
		expect(headings).toEqual([...SECTION_TITLES]);

		const bodies = sectionBodies(doc);
		for (const title of SECTION_TITLES) {
			const why = bodies.get(title)?.match(/^Why: (.+)$/m);
			expect(why, title).not.toBeNull();
			expect(why![1]).toMatch(/\b(?:C\d+|D\d+|E\d+)\b/);
		}
	});

	test("each Current or Proposed stays a substring of omp-AGENTS.md, and Proposed differs", async () => {
		const doc = await readRepoText(PROPOSAL_REL);
		const doctrineNorm = normalizeWs(await readRepoText(DOCTRINE_REL));
		const bodies = sectionBodies(doc);

		for (const title of SECTION_TITLES) {
			const body = bodies.get(title);
			expect(body, title).toBeDefined();
			const current = normalizeWs(labeledFence(body!, "Current"));
			const proposed = normalizeWs(labeledFence(body!, "Proposed"));

			expect(current.length, title).toBeGreaterThan(0);
			expect(proposed.length, title).toBeGreaterThan(0);
			expect(proposed, title).not.toBe(current);
			// Current matches before s08 pastes the wording. Proposed matches after.
			// Either anchor keeps this check green across that sync.
			expect(
				doctrineNorm.includes(current) || doctrineNorm.includes(proposed),
				title,
			).toBe(true);
		}
	});

	test("no Proposed block keeps the retired gate phrases", async () => {
		const bodies = sectionBodies(await readRepoText(PROPOSAL_REL));

		for (const title of SECTION_TITLES) {
			const proposed = normalizeWs(labeledFence(bodies.get(title)!, "Proposed"));
			for (const phrase of RETIRED_PROPOSED_PHRASES) {
				expect(proposed, `${title}: ${phrase}`).not.toContain(phrase);
			}
		}
	});

	test("live doctrine has no question-format heading and no automatic-machinery clause", async () => {
		const doctrine = await readRepoText(DOCTRINE_REL);
		expect(doctrine).not.toMatch(/^#+ Question format/m);
		expect(doctrine).not.toContain("no automatic machinery");
	});

	test("no proposal line matches the cockpit claim pattern", async () => {
		const lines = (await readRepoText(PROPOSAL_REL)).split("\n");
		const hits = lines.filter(line => COCKPIT_CLAIM_PATTERN.test(line));
		expect(hits).toEqual([]);
	});
});
