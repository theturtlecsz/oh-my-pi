import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { REPORT_DIR, parseLockRows, unresolvedLinks } from "./fixtures/lockdown-report.ts";

const README_DOC = "README.md";
const ROBOMP_DOC = "robomp.md";
const PROBE_PATH = "/home/thetu/master-report/evidence/omp-399-slot-probe.txt";

const SECTION_DOCS = [
	"runtime-defaults.md",
	"robomp.md",
	"child-budgets.md",
	"flood.md",
] as const;

const OPEN_IDS = [
	"L-RT-01",
	"L-RT-02",
	"L-RT-03",
	"L-RT-04",
	"L-RB-01",
	"L-RB-02",
	"L-RB-03",
	"L-CB-03",
	"L-FL-03",
	"L-FL-04",
];

interface IndexRow {
	ID: string;
	Section: string;
	Divergence: string;
	Status: string;
}

function splitCells(line: string): string[] {
	const trimmed = line.trim();
	if (!trimmed.includes("|")) return [];
	let content = trimmed;
	if (content.startsWith("|")) content = content.slice(1);
	if (content.endsWith("|")) content = content.slice(0, -1);

	const cells: string[] = [];
	let current = "";
	let escaped = false;
	for (const char of content) {
		if (escaped) {
			current += char;
			escaped = false;
		} else if (char === "\\") {
			current += char;
			escaped = true;
		} else if (char === "|") {
			cells.push(current.trim());
			current = "";
		} else {
			current += char;
		}
	}
	cells.push(current.trim());
	return cells;
}

function parseIndexTable(md: string): IndexRow[] {
	const rows: IndexRow[] = [];
	let inTable = false;

	for (const raw of md.split("\n")) {
		const line = raw.trim();
		if (!line.includes("|")) {
			inTable = false;
			continue;
		}
		const cells = splitCells(line);
		if (!inTable) {
			if (
				cells.length === 4 &&
				cells[0] === "ID" &&
				cells[1] === "Section" &&
				cells[2] === "Upstream divergence" &&
				cells[3] === "Status"
			) {
				inTable = true;
			}
			continue;
		}
		if (cells.every(c => /^:?-+:?$/.test(c))) continue;
		if (cells.length !== 4) {
			inTable = false;
			continue;
		}
		rows.push({ ID: cells[0], Section: cells[1], Divergence: cells[2], Status: cells[3] });
	}

	return rows;
}

const APPLIED = /\(applied: ([^)]+)\)\s*$/;
const DECIDED = /\(decided: D\d+, (OMP-\d+)\)\s*$/;

/** Re-derive a lock's status from its section row, per the README's documented rule. */
function statusForRow(lock: string, divergence: string): string {
	const applied = divergence.trim().match(APPLIED) ?? lock.trim().match(APPLIED);
	if (applied) return `applied: ${applied[1]}`;
	const decided = lock.trim().match(DECIDED) ?? divergence.trim().match(DECIDED);
	if (decided) return `decided: ${decided[1]}`;
	return "open";
}

/** The taxonomy classification of a divergence cell, dropping any trailing marker. */
function divergenceClass(cell: string): string {
	return cell.trim().replace(/\s*\([^)]*\)\s*$/, "").trim();
}

function sectionHref(cell: string): string {
	const match = cell.match(/\]\(([^)]+)\)/);
	return match ? match[1].split("#")[0].trim() : "";
}

function hrefsIn(md: string): string[] {
	return [...md.matchAll(/\]\(([^)]+)\)/g)].map(m => m[1].split("#")[0].trim());
}

function headingBody(md: string, heading: string): string {
	const start = md.indexOf(`\n${heading}\n`);
	if (start < 0) return "";
	const rest = md.slice(start + heading.length + 2);
	const next = rest.search(/\n## /);
	return next === -1 ? rest : rest.slice(0, next);
}

function checkboxIds(section: string): string[] {
	const ids: string[] = [];
	for (const line of section.split("\n")) {
		const match = line.trim().match(/^- \[([ xX])\] (.+)$/);
		if (match) ids.push(`${match[1].toLowerCase() === "x" ? "x" : " "}:${match[2].trim()}`);
	}
	return ids;
}

async function readReport(doc: string): Promise<string> {
	return Bun.file(path.join(REPORT_DIR, doc)).text();
}

describe("unattended lockdown report - index", () => {
	test("README unresolvedLinks is empty", async () => {
		const readme = await readReport(README_DOC);
		expect(unresolvedLinks(README_DOC, readme)).toEqual([]);
	});

	test("all-locks rows match the section tables (IDs, order, divergence, section link)", async () => {
		const readme = await readReport(README_DOC);
		const indexRows = parseIndexTable(readme);
		expect(indexRows.length).toBeGreaterThan(0);

		const expected: { id: string; divergence: string; doc: string }[] = [];
		for (const doc of SECTION_DOCS) {
			for (const row of await rows(doc)) {
				expected.push({ id: row.ID, divergence: divergenceClass(row["Upstream divergence"]), doc });
			}
		}

		expect(indexRows.map(r => r.ID)).toEqual(expected.map(e => e.id));

		for (let i = 0; i < indexRows.length; i++) {
			const row = indexRows[i];
			const want = expected[i];
			expect(row.Divergence).toBe(want.divergence);
			expect(path.resolve(REPORT_DIR, sectionHref(row.Section))).toBe(path.resolve(REPORT_DIR, want.doc));
		}
	});

	test("Status matches each section lock row and carries the right OMP refs", async () => {
		const readme = await readReport(README_DOC);
		const indexRows = parseIndexTable(readme);

		const sectionRows = (
			await Promise.all(SECTION_DOCS.map(async doc => (await rows(doc)).map(r => ({ doc, r }))))
		).flat();
		const byId = new Map(sectionRows.map(({ r }) => [r.ID, r]));

		for (const row of indexRows) {
			const source = byId.get(row.ID);
			expect(source).toBeDefined();
			expect(row.Status).toBe(statusForRow(source!.Lock, source!["Upstream divergence"]));
		}

		expect(indexRows.filter(r => r.Status.startsWith("applied:")).map(r => `${r.ID}=${r.Status}`)).toEqual([
			"L-RT-05=applied: OMP-396-s02",
		]);

		expect(indexRows.filter(r => r.Status === "decided: OMP-404").map(r => r.ID)).toEqual([
			"L-CB-01",
			"L-CB-02",
			"L-CB-04",
			"L-FL-02",
		]);

		expect(indexRows.filter(r => r.Status === "decided: OMP-402").map(r => r.ID)).toEqual(["L-FL-01"]);
	});

	test("open locks are the wait-list IDs and nothing is checked off", async () => {
		const readme = await readReport(README_DOC);
		const open = parseIndexTable(readme)
			.filter(r => r.Status === "open")
			.map(r => r.ID);
		expect(open).toEqual(OPEN_IDS);

		const waiting = headingBody(readme, "## Waiting for Chris");
		expect(waiting.length).toBeGreaterThan(0);

		const boxes = checkboxIds(waiting);
		expect(boxes.some(b => b.startsWith("x:"))).toBe(false);
		expect(boxes.map(b => b.slice(2))).toEqual(OPEN_IDS);
	});

	test("Sections nav links all four section reports", async () => {
		const readme = await readReport(README_DOC);
		const sections = headingBody(readme, "## Sections");
		expect(sections.length).toBeGreaterThan(0);

		const resolved = hrefsIn(sections).map(h => path.resolve(REPORT_DIR, h));
		for (const doc of SECTION_DOCS) {
			expect(resolved).toContain(path.resolve(REPORT_DIR, doc));
		}
	});

	test("Q4 tie cites the inventory, the guardrail, L-RT-05, D15/OMP-402 and D17/OMP-404", async () => {
		const readme = await readReport(README_DOC);
		const tie = headingBody(readme, "## Ties to Q4");
		expect(tie.length).toBeGreaterThan(0);

		const resolved = hrefsIn(tie).map(h => path.resolve(REPORT_DIR, h));
		expect(resolved).toContain(path.resolve(REPORT_DIR, "../../upstream-fork-inventory.tsv"));
		expect(resolved).toContain(path.resolve(REPORT_DIR, "../../upstream-guardrail.md"));

		for (const needle of ["OMP-396-s02", "D15", "OMP-402", "D17", "OMP-404", "L-CB-03"]) {
			expect(tie).toContain(needle);
		}
	});
});

describe("unattended lockdown report - robomp live probe", () => {
	test("robomp unresolvedLinks is empty and the live-probe section is present", async () => {
		const robomp = await readReport(ROBOMP_DOC);
		expect(unresolvedLinks(ROBOMP_DOC, robomp)).toEqual([]);
		expect(robomp).toContain("### Live probe (OMP-399-s05)");
	});

	test("one Live answer line follows the rule applied to the quoted probe", async () => {
		const robomp = await readReport(ROBOMP_DOC);
		const section = headingBody(robomp, "### Live probe (OMP-399-s05)");
		expect(section.length).toBeGreaterThan(0);

		const quoted = section.match(/```[^\n]*\n([\s\S]*?)```/);
		expect(quoted).not.toBeNull();
		const probe = quoted![1].trim();

		const readableModels = probe
			.split("\n")
			.some(line => line.includes("readable") && line.includes(".omp/agent/models.yml"));
		const expected = probe.includes("robomp not deployed")
			? "not deployed"
			: readableModels || probe.includes("agent.db readable")
				? "yes"
				: "no";

		const answers = [...robomp.matchAll(/^Live answer: (yes|no|not deployed)$/gm)].map(m => m[1]);
		expect(answers).toEqual([expected]);

		const credentialLines = [...robomp.matchAll(/^Credential-like lines: \d+$/gm)];
		expect(credentialLines.length).toBe(expected === "not deployed" ? 0 : 1);
	});

	test("the quoted probe matches the recorded evidence file verbatim", () => {
		expect(fs.existsSync(PROBE_PATH)).toBe(true);
		const robomp = fs.readFileSync(path.join(REPORT_DIR, ROBOMP_DOC), "utf8");
		const section = headingBody(robomp, "### Live probe (OMP-399-s05)");
		const quoted = section.match(/```[^\n]*\n([\s\S]*?)```/);
		expect(quoted).not.toBeNull();
		const probe = quoted![1].replace(/\n$/, "");
		const evidence = fs.readFileSync(PROBE_PATH, "utf8").replace(/\n$/, "");
		expect(probe).toBe(evidence);
	});
});

async function rows(doc: string) {
	return parseLockRows(await readReport(doc));
}
