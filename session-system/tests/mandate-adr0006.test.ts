import { describe, expect, test } from "bun:test";
import { readRepoText, unresolvedLinks } from "./fixtures/programme-claims";

const ADR_REL = "docs/adr/0006-on-demand-roles.md";
const ADR0004_REL = "docs/adr/0004-hard-padlock-control-plane.md";

const EXPECTED_HEADINGS = [
	"# ADR 0006: On-demand roles",
	"## Context",
	"## Decision",
	"### 1. Ephemeral by default",
	"### 2. ADR 0004 unchanged",
	"## Role target forms",
];

const ADR0004_LINK = "[ADR 0004](0004-hard-padlock-control-plane.md)";

/** ADR 0004 §1 body, verbatim. This ADR must not alter it. */
const SOLE_MUTATOR_BODY =
	"Run Owner is the only principal holding mutate grants and exercises them **only through WorkService** (FULL-PROGRAM v1.1 §3). Advisors (Programme Design, Status Desk, Loop Auditor, Deep Research) are read-only / blueprint-only.";

const EXPECTED_HEADER = ["Role", "Target form", "Standing LLM agent", "Invariant kept", "Reason"];

/** The seven roles the mandate names, held as data, in document order. */
const EXPECTED_ROWS: string[][] = [
	[
		"Programme Design",
		"Temporary worker: the lifecycle's plan stage",
		"No",
		"Blueprint-only: its output is a plan proposal the control plane stamps",
		"Plans are per mission; nothing needs to persist between them",
	],
	[
		"Status Desk",
		"Query (`project.get_status`, `mission.status`) plus report (daily digest, OMP-406)",
		"No",
		"Presents native state only: derived from ledger records (D6)",
		"A query cannot drift from the ledger",
	],
	[
		"Loop Auditor",
		"Scheduled job running an evaluator over recent missions, filing findings",
		"No",
		"Read-only: files findings, never mutates",
		"A periodic check needs no standing identity",
	],
	[
		"Deep Research",
		"Capability (`research.run`) executed by temporary workers under an admitted campaign",
		"No",
		"Cannot start a campaign alone: admission via WorkService",
		"Research runs per campaign",
	],
];

/** Collect the lines under `heading`, stopping at the next heading at or above its level. */
function extractSection(content: string, heading: string): string {
	const lines = content.split("\n");
	const startIndex = lines.findIndex(line => line.trim() === heading);
	if (startIndex === -1) {
		throw new Error(`Section "${heading}" not found`);
	}
	const level = heading.match(/^#+/)?.[0].length ?? 1;
	const sectionLines: string[] = [];
	for (let i = startIndex + 1; i < lines.length; i++) {
		const next = lines[i].match(/^(#+)\s/);
		if (next && next[1].length <= level) break;
		sectionLines.push(lines[i]);
	}
	return sectionLines.join("\n");
}

/** Split every markdown table row of a section into trimmed cells. */
function parseTableRows(sectionContent: string): string[][] {
	const rows: string[][] = [];
	let inTable = false;

	for (const line of sectionContent.split("\n")) {
		const trimmed = line.trim();
		if (!trimmed.startsWith("|")) continue;
		const cells = trimmed
			.split("|")
			.slice(1, -1)
			.map(c => c.trim());

		// Separator row (---) starts the body; header row keeps the first table line.
		if (cells.every(c => /^-+$/.test(c))) {
			inTable = true;
			continue;
		}
		if (!inTable) {
			rows.push(cells); // header
			continue;
		}
		rows.push(cells);
	}

	return rows;
}

describe("ADR 0006: On-demand roles", () => {
	test("holds the required title, status, date, and structural headings", async () => {
		const content = await readRepoText(ADR_REL);
		for (const heading of EXPECTED_HEADINGS) {
			expect(content).toContain(heading);
		}
		expect(content).toMatch(/^\*\*Status:\*\*\s*Accepted;\s*target forms not built/m);
		expect(content).toMatch(/^\*\*Date:\*\*\s*2026-09-28/m);
	});

	test("Context cites owner decision D28 and mandate section 13", async () => {
		const context = extractSection(await readRepoText(ADR_REL), "## Context");
		expect(context).toContain("D28");
		expect(context).toContain("section 13");
		expect(context).toContain("Fewer persistent agent roles");
	});

	test("Decision names the ephemeral default and links ADR 0004 unchanged", async () => {
		const content = await readRepoText(ADR_REL);
		const decision = extractSection(content, "## Decision");

		expect(decision).toContain("### 1. Ephemeral by default");
		expect(decision).toContain("### 2. ADR 0004 unchanged");
		expect(decision).toContain(ADR0004_LINK);
		expect(decision).toContain("sole-mutator");
	});

	test("role target forms table equals the expected header and rows cell by cell", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, "## Role target forms");
		const rows = parseTableRows(section);

		expect(rows[0]).toEqual(EXPECTED_HEADER);
		// Appended roles follow these four; lock the original prefix.
		expect(rows.slice(1, 1 + EXPECTED_ROWS.length)).toEqual(EXPECTED_ROWS);
	});

	test("each expected role is found by name with equal cells", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, "## Role target forms");
		const byRole = new Map(parseTableRows(section).slice(1).map(cells => [cells[0], cells]));

		for (const expected of EXPECTED_ROWS) {
			expect(byRole.get(expected[0])).toEqual(expected);
		}
	});

	test("no role runs as a standing LLM agent", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, "## Role target forms");
		for (const cells of parseTableRows(section).slice(1)) {
			expect(cells[2]).toBe("No");
		}
	});

	test("unresolvedLinks is empty and the ADR 0004 link resolves", async () => {
		const content = await readRepoText(ADR_REL);
		expect(content).toContain(ADR0004_LINK);
		expect(unresolvedLinks(ADR_REL, content)).toEqual([]);
	});

	test("ADR 0004 §1 Sole mutator body is unchanged verbatim", async () => {
		const adr0004 = await readRepoText(ADR0004_REL);
		const body = extractSection(adr0004, "### 1. Sole mutator");
		expect(body.trim()).toBe(SOLE_MUTATOR_BODY);
	});

	test("no line matches /grok ?bot|cockpit_verbs/i", async () => {
		const content = await readRepoText(ADR_REL);
		for (const line of content.split("\n")) {
			expect(line).not.toMatch(/grok ?bot|cockpit_verbs/i);
		}
	});
});
