import { describe, expect, test } from "bun:test";
import { readRepoText } from "./fixtures/programme-claims";

const ADR_REL = "docs/adr/0006-on-demand-roles.md";

const EXPECTED_ROLES = [
	"Programme Design",
	"Status Desk",
	"Loop Auditor",
	"Deep Research",
	"Deterministic supervisor",
	"Native reviewer (`@audit`)",
	"Persistent planners",
];

/** The three roles this slice adds, held as data, in document order. */
const NEW_ROWS: string[][] = [
	[
		"Deterministic supervisor",
		"Service: the mission orchestrator (software, OMP-417)",
		"No",
		"One coherent assignment per cycle; failures preserved unchanged; no model decides stage order or coordinates workers (D33)",
		"A process must claim jobs, but its state is durable, so it is a restartable service, not an agent",
	],
	[
		"Native reviewer (`@audit`)",
		"Evaluator, spawned per candidate",
		"No",
		"Maker/checker separation, checked by the control plane's acceptance_semantics check (OMP-421); no new model-family rule",
		"Review is per change",
	],
	[
		"Persistent planners",
		"Temporary worker (same as Programme Design)",
		"No",
		"Plan stamped before execution",
		"Plans are per mission; nothing needs to persist between them",
	],
];

const TARGET_FORM_PREFIXES = [
	"Capability",
	"Evaluator",
	"Scheduled job",
	"Temporary worker",
	"Report",
	"Query",
	"Service",
];

const D33_LINE =
	"D33 (2026-09-28) supersedes the reconciliation tab's model for supervisor coordination judgment.";

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

describe("ADR 0006: role target forms", () => {
	test("each new role, found by Role, equals the parsed row cell by cell", async () => {
		const section = extractSection(await readRepoText(ADR_REL), "## Role target forms");
		const byRole = new Map(parseTableRows(section).slice(1).map(cells => [cells[0], cells]));

		for (const expected of NEW_ROWS) {
			expect(byRole.get(expected[0])).toEqual(expected);
		}
	});

	test("Role column is the seven roles in order", async () => {
		const section = extractSection(await readRepoText(ADR_REL), "## Role target forms");
		const roles = parseTableRows(section).slice(1).map(cells => cells[0]);
		expect(roles).toEqual(EXPECTED_ROLES);
	});

	test("every row has Standing LLM agent No, a non-empty Reason, and a known Target form", async () => {
		const section = extractSection(await readRepoText(ADR_REL), "## Role target forms");
		for (const cells of parseTableRows(section).slice(1)) {
			expect(cells).toHaveLength(5);
			expect(cells[2]).toBe("No");
			expect(cells[4].length).toBeGreaterThan(0);
			expect(TARGET_FORM_PREFIXES.some(prefix => cells[1].startsWith(prefix))).toBe(true);
		}
	});

	test("D33 supersession line is present", async () => {
		const content = await readRepoText(ADR_REL);
		expect(content).toContain(D33_LINE);
	});
});
