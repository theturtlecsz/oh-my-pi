import { describe, expect, test } from "bun:test";
import { readRepoText, unresolvedLinks } from "./fixtures/programme-claims";

const ADR_REL = "docs/adr/0005-mission-first-architecture.md";

const SECTION_HEADING = "Run Owner invariants";

const INTRO =
	"From MANDATE-VALIDATION.md (2026-09-28). ADR 0004 is unchanged (D28). Under A10, OMP-421 builds these checks before the OMP-417 orchestrator; they are not built yet.";

const EXPECTED_HEADER = ["Invariant", "Control-plane check", "Lands in", "Test"];

/** The Run Owner invariants table, held as data, in document order. */
const EXPECTED_ROWS: string[][] = [
	[
		"Single mutation authority",
		"Only WorkService writes; workers hold no write scope and return proposals",
		"OMP-402, OMP-403, OMP-421",
		"A worker's write command is refused",
	],
	[
		"Admission control",
		"Needs approved mission scope, effort field, budget reservation, free capacity",
		"OMP-417, OMP-420, OMP-421",
		"A job missing scope, effort or reservation is refused",
	],
	[
		"Budget policy",
		"Reservation before dispatch; usage per job; threshold event; overrun pauses and raises a decision",
		"OMP-404, OMP-413, OMP-417",
		"Dispatch without reservation refused; overrun pauses",
	],
	[
		"Worker lifecycle",
		"Register, lease, renew, fence; after reclaim read state, re-dispatch once",
		"OMP-400, OMP-417",
		"Fenced write refused; reclaimed job resumes once",
	],
	[
		"Acceptance semantics",
		"Sealed criteria, evidence per criterion, independent reviewer PASS",
		"OMP-417, OMP-420, OMP-421",
		"Close without PASS or with self-review refused",
	],
	[
		"Authoritative state",
		"WorkService only; sessions and pending files are caches",
		"OMP-413, OMP-417",
		"Restart test reads only WorkService",
	],
	[
		"Contract freeze",
		"Mission and stop-control contract versions need Chris's approval (D30)",
		"OMP-405, OMP-413 to OMP-416",
		"Version without approval record does not activate",
	],
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

describe("ADR 0005: Run Owner invariants", () => {
	test("Run Owner invariants section follows Contract versions (D30) and quotes MANDATE-VALIDATION.md", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, SECTION_HEADING);

		expect(section).toContain("MANDATE-VALIDATION.md");
		expect(section).toContain(INTRO);
		expect(content.indexOf(`## ${SECTION_HEADING}`)).toBeGreaterThan(
			content.indexOf("## Contract versions (D30)"),
		);
	});

	test("invariants table equals the expected rows cell by cell in order", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, SECTION_HEADING);
		const rows = parseTableRows(section);

		expect(rows[0]).toEqual(EXPECTED_HEADER);
		expect(rows.slice(1)).toEqual(EXPECTED_ROWS);
	});

	test("unresolvedLinks is empty", async () => {
		const content = await readRepoText(ADR_REL);
		expect(unresolvedLinks(ADR_REL, content)).toEqual([]);
	});
});
