import { describe, expect, test } from "bun:test";
import { readRepoText, unresolvedLinks } from "./fixtures/programme-claims";

const ADR_REL = "docs/adr/0005-mission-first-architecture.md";

const LOCKS_HEADING = "Locks";
const D41_HEADING = "Merges to protected or default branches (D41)";
const D36_HEADING = "Scope: OMP runtime only (D36)";

const D40 =
	"D40: D35 supersedes D16's classes where they conflict; an action no tier lists is tier 3.";

const Q =
	"D35 governs OMP's own runtime and unattended execution behavior. Flood is a separate build/implementation system used to develop OMP. It is not part of the OMP product control plane and is not governed by D35. Do not retrofit OMP runtime approval, mission, standing-policy, merge, deployment, or safety-envelope rules onto flood unless I separately authorize that as a flood-specific policy. Flood may continue operating under its own controls while implementing OMP.";

const D41_PHRASES = ["tier 3", "normal decision", "Not flood"];

const EXPECTED_HEADER = ["Lock", "Enforced by", "Test"];

/** The twelve D35 locks, held as data, in document order. */
const EXPECTED_ROWS: string[][] = [
	["1. Repository/path allowlist", "OMP-417", "outside write refused"],
	["2. Isolated worktree/sandbox", "OMP-417", "other worktrees unreadable"],
	["3. No source credentials in workers", "OMP-417", "credentials unreadable"],
	["4. Single mutation authority", "OMP-421, OMP-417", "worker write refused"],
	["5. Lease + idempotency enforcement", "OMP-417, OMP-400", "no duplicate after crash"],
	["6. Independent verification", "OMP-417, OMP-420", "unverified: not accepted"],
	["7. Budget caps", "OMP-418, OMP-413, OMP-430", "spend past ceiling refused"],
	["8. Network egress policy", "OMP-431", "unlisted destination refused"],
	["9. Protected-action gate", "OMP-403, OMP-418", "unsigned tier 3 refused"],
	["10. Stop means pause", "OMP-405, OMP-430, OMP-425, OMP-417", "stop: nothing dispatches"],
	["11. Audit trail", "OMP-417, OMP-415", "edited event fails check"],
	["12. Fail closed", "OMP-421, OMP-403", "ambiguity refused"],
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

describe("ADR 0005: Locks (D35)", () => {
	test("locks table equals the expected header and rows cell by cell in order", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, LOCKS_HEADING);
		const rows = parseTableRows(section);

		expect(rows[0]).toEqual(EXPECTED_HEADER);
		expect(rows.slice(1)).toEqual(EXPECTED_ROWS);
		expect(rows.slice(1).length).toBe(12);
	});

	test("every Enforced-by cell names an OMP item and every Test cell has text", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, LOCKS_HEADING);
		const rows = parseTableRows(section).slice(1);

		for (const [, enforcedBy, testText] of rows) {
			for (const id of enforcedBy.split(",").map(c => c.trim())) {
				expect(id).toMatch(/^OMP-\d+$/);
			}
			expect(testText.length).toBeGreaterThan(0);
		}
	});

	test("locks table sits after the D40 line and before the D41 heading", async () => {
		const content = await readRepoText(ADR_REL);
		const d40Index = content.indexOf(D40);
		const locksIndex = content.indexOf(`## ${LOCKS_HEADING}`);
		const d41Index = content.indexOf(`## ${D41_HEADING}`);

		expect(d40Index).toBeGreaterThan(-1);
		expect(locksIndex).toBeGreaterThan(d40Index);
		expect(d41Index).toBeGreaterThan(locksIndex);
	});

	test("D41 section states tier 3, normal decision, and Not flood", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, D41_HEADING);

		for (const phrase of D41_PHRASES) {
			expect(section).toContain(phrase);
		}
	});

	test("D36 section holds Q verbatim as a blockquote", async () => {
		const content = await readRepoText(ADR_REL);
		const section = extractSection(content, D36_HEADING);

		expect(blockquoteText(section)).toBe(Q);
	});

	test("unresolvedLinks is empty", async () => {
		const content = await readRepoText(ADR_REL);
		expect(unresolvedLinks(ADR_REL, content)).toEqual([]);
	});
});
