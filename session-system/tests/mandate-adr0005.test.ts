import { describe, expect, test } from "bun:test";
import { readRepoText, unresolvedLinks } from "./fixtures/programme-claims";

const ADR_REL = "docs/adr/0005-mission-first-architecture.md";
const OMP_AGENTS_REL = "session-system/agents/omp-AGENTS.md";

const EXPECTED_HEADINGS = [
	"# ADR 0005: Mission-first architecture",
	"## Decision",
	"### 1. Mission API",
	"### 2. Control plane validates LLM proposals",
	"### 3. Commands become internal operations",
	"## Supersedes",
	"## Mission approval rules (D29)",
	"## Contract versions (D30)",
];

const DOCTRINE_PREFIXES = [
	"Intake routing",
	"Routine ledger self-confirmation",
	"Close asymmetry",
	"Autonomous execution authority",
];

const OTHER_SUPERSEDES_TERMS = [
	"Question format",
	"no automatic machinery",
	"D16",
	"D23",
	"D25",
];

const D29_QUOTE =
	"New or materially changed mission scope requires Chris's confirmation. Routine work within an already approved mission or standing project mandate should not require repeated ceremonial approval. OMP may autonomously pause, retry, reroute and reschedule work according to policy. OMP may not permanently abandon, cancel or materially redefine an approved mission without an explicit policy basis or Chris's approval.";

const D29_EXPECTED_ROW_IDS = [
	"OMP-426",
	"OMP-413 / OMP-418",
	"OMP-413 / OMP-417",
	"OMP-420 / OMP-417",
	"OMP-414",
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

function parseTableRows(sectionContent: string): { rule: string; landsIn: string }[] {
	const lines = sectionContent.split("\n");
	const rows: { rule: string; landsIn: string }[] = [];
	let inTable = false;

	for (const line of lines) {
		const trimmed = line.trim();
		if (!trimmed.startsWith("|")) continue;
		const cells = trimmed
			.split("|")
			.slice(1, -1)
			.map(c => c.trim());
		if (cells.length < 2) continue;

		// Header or separator row
		if (cells[0] === "Rule" || cells[0].startsWith("---")) {
			inTable = true;
			continue;
		}

		if (inTable) {
			rows.push({ rule: cells[0], landsIn: cells[1] });
		}
	}

	return rows;
}

describe("ADR 0005: Mission-first architecture", () => {
	test("holds required title, status, date, and structural headings", async () => {
		const content = await readRepoText(ADR_REL);
		for (const heading of EXPECTED_HEADINGS) {
			expect(content).toContain(heading);
		}
		expect(content).toMatch(/^\*\*Status:\*\*\s*Accepted;\s*enforcement not built/m);
		expect(content).toMatch(/^\*\*Date:\*\*\s*2026-09-28/m);
	});

	test("Supersedes section names doctrine headings matching omp-AGENTS.md prefixes and superseded decisions", async () => {
		const content = await readRepoText(ADR_REL);
		const ompAgents = await readRepoText(OMP_AGENTS_REL);
		const supersedes = extractSection(content, "Supersedes");

		for (const prefix of DOCTRINE_PREFIXES) {
			expect(ompAgents).toMatch(new RegExp(`^##\\s+${prefix}`, "m"));
			expect(supersedes).toContain(prefix);
		}

		for (const term of OTHER_SUPERSEDES_TERMS) {
			expect(supersedes).toContain(term);
		}
	});

	test("Mission approval rules (D29) holds verbatim blockquote and table row ids in order", async () => {
		const content = await readRepoText(ADR_REL);
		expect(content).toContain(D29_QUOTE);

		const d29Section = extractSection(content, "Mission approval rules (D29)");
		const rows = parseTableRows(d29Section);
		expect(rows.map(r => r.landsIn)).toEqual(D29_EXPECTED_ROW_IDS);
	});

	test("Contract versions (D30) names required mandate and stop-control items", async () => {
		const content = await readRepoText(ADR_REL);
		const d30Section = extractSection(content, "Contract versions (D30)");
		expect(d30Section).toContain("OMP-405");
		expect(d30Section).toContain("OMP-411");
		expect(d30Section).toContain("OMP-426");
	});

	test("no line matches /grok ?bot|cockpit_verbs/i", async () => {
		const content = await readRepoText(ADR_REL);
		const lines = content.split("\n");
		for (const line of lines) {
			expect(line).not.toMatch(/grok ?bot|cockpit_verbs/i);
		}
	});

	test("unresolvedLinks is empty", async () => {
		const content = await readRepoText(ADR_REL);
		expect(unresolvedLinks(ADR_REL, content)).toEqual([]);
	});
});
