import { describe, expect, test } from "bun:test";
import { LOCK_COLUMNS, REPORT_DIR, parseLockRows, unresolvedLinks } from "./fixtures/lockdown-report.ts";

const FLOOD_DOC = "flood.md";

function factsBody(md: string): string {
	const heading = "## Facts";
	const start = md.indexOf(heading);
	if (start < 0) return "";
	const rest = md.slice(start + heading.length);
	const next = rest.search(/\n## /);
	return next === -1 ? rest : rest.slice(0, next);
}

describe("unattended lockdown report - flood", () => {
	const floodPath = `${REPORT_DIR}/${FLOOD_DOC}`;

	test("parses L-FL-01..04 with non-empty cells", async () => {
		const md = await Bun.file(floodPath).text();
		const rows = parseLockRows(md);

		expect(rows.map(r => r.ID)).toEqual(["L-FL-01", "L-FL-02", "L-FL-03", "L-FL-04"]);

		for (const row of rows) {
			for (const col of LOCK_COLUMNS) {
				expect(row[col]).toBeDefined();
				expect(row[col].trim().length).toBeGreaterThan(0);
			}
		}
	});

	test("every location is external flood.json and every divergence is none", async () => {
		const md = await Bun.file(floodPath).text();
		const rows = parseLockRows(md);

		for (const row of rows) {
			expect(row.Location.startsWith("external:")).toBe(true);
			expect(row.Location.startsWith("external: flood.json")).toBe(true);
			expect(row["Upstream divergence"].trim()).toBe("none");
		}
	});

	test("L-FL-01 and L-FL-02 locks end with their decisions; L-FL-03 and L-FL-04 have none", async () => {
		const md = await Bun.file(floodPath).text();
		const rows = parseLockRows(md);

		expect(rows[0].Lock.trim().endsWith("(decided: D15, OMP-402)")).toBe(true);
		expect(rows[1].Lock.trim().endsWith("(decided: D17, OMP-404)")).toBe(true);

		for (const row of rows.slice(2)) {
			for (const col of LOCK_COLUMNS) {
				expect(row[col].includes("decided")).toBe(false);
			}
		}
	});

	test("facts name grok-reviewer and claude-planner, and links resolve", async () => {
		const md = await Bun.file(floodPath).text();
		const facts = factsBody(md);

		expect(facts).toContain("grok-reviewer");
		expect(facts).toContain("claude-planner");
		expect(unresolvedLinks(FLOOD_DOC, md)).toEqual([]);
	});
});
