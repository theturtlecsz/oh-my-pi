import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { LOCK_COLUMNS, REPORT_DIR, REPO_ROOT, parseLockRows, unresolvedLinks } from "./fixtures/lockdown-report.ts";

describe("unattended lockdown report - child budgets", () => {
	const reportPath = path.join(REPORT_DIR, "child-budgets.md");
	const inventoryPath = path.join(REPO_ROOT, "docs/upstream-fork-inventory.tsv");

	const EXPECTED_CODE_FILES = [
		"packages/coding-agent/src/task/executor.ts",
		"packages/coding-agent/src/config/settings-schema.ts",
		"python/robomp/src/config.py",
	];

	test("report file exists on disk", () => {
		expect(fs.existsSync(reportPath)).toBe(true);
	});

	test("unresolvedLinks is empty for child-budgets.md", async () => {
		const md = await Bun.file(reportPath).text();
		expect(unresolvedLinks("child-budgets.md", md)).toEqual([]);
	});

	test("parses exactly 4 child budget lock rows with non-empty cells and valid IDs L-CB-01..04", async () => {
		const md = await Bun.file(reportPath).text();
		const rows = parseLockRows(md);

		expect(rows).toHaveLength(4);
		expect(rows.map(r => r.ID)).toEqual(["L-CB-01", "L-CB-02", "L-CB-03", "L-CB-04"]);

		for (const row of rows) {
			for (const col of LOCK_COLUMNS) {
				expect(row[col]).toBeDefined();
				expect(row[col].trim().length).toBeGreaterThan(0);
			}
		}
	});

	test("every row Location links to at least one existing file", async () => {
		const md = await Bun.file(reportPath).text();
		const rows = parseLockRows(md);
		const linkRegex = /\[(?:[^\]]*)\]\(([^)]+)\)/g;

		for (const row of rows) {
			const targets: string[] = [];
			let match: RegExpExecArray | null;
			while ((match = linkRegex.exec(row.Location)) !== null) {
				targets.push(match[1].split("#")[0].split("?")[0].trim());
			}
			expect(targets.length).toBeGreaterThan(0);
			for (const target of targets) {
				expect(fs.existsSync(path.resolve(REPORT_DIR, target))).toBe(true);
			}
		}
	});

	test("divergence is adds for all rows", async () => {
		const md = await Bun.file(reportPath).text();
		const rows = parseLockRows(md);

		for (const row of rows) {
			expect(row["Upstream divergence"].trim()).toBe("adds");
		}
	});

	test("decided suffix is on L-CB-01, 02, 04 only and L-CB-03 carries no decided marker", async () => {
		const md = await Bun.file(reportPath).text();
		const rows = parseLockRows(md);
		const decidedById = new Map(rows.map(r => [r.ID, r.Lock]));

		for (const id of ["L-CB-01", "L-CB-02", "L-CB-04"]) {
			const lock = decidedById.get(id) ?? "";
			expect(lock.endsWith("(decided: D17, OMP-404)")).toBe(true);
		}

		const lock03 = decidedById.get("L-CB-03") ?? "";
		expect(lock03).not.toContain("decided");
	});

	test("links cover executor.ts, settings-schema.ts and config.py", async () => {
		const md = await Bun.file(reportPath).text();
		const linkRegex = /\[(?:[^\]]*)\]\(([^)]+)\)/g;
		const linkedFiles: string[] = [];

		let match: RegExpExecArray | null;
		while ((match = linkRegex.exec(md)) !== null) {
			const raw = match[1].split("#")[0].split("?")[0].trim();
			if (raw === "" || /^(?:https?|mailto):/i.test(raw) || raw.startsWith("#")) continue;
			linkedFiles.push(path.relative(REPO_ROOT, path.resolve(REPORT_DIR, raw)));
		}

		for (const codeFile of EXPECTED_CODE_FILES) {
			expect(linkedFiles).toContain(codeFile);
		}

		const inventoryText = await Bun.file(inventoryPath).text();
		const sharedRows = new Set<string>();
		for (const line of inventoryText.split("\n")) {
			const parts = line.split("\t");
			if (parts.length >= 2 && parts[1] === "shared") {
				sharedRows.add(parts[0]);
			}
		}
		for (const codeFile of EXPECTED_CODE_FILES) {
			expect(sharedRows.has(codeFile)).toBe(true);
		}
	});

	test("Affected runs never mentions flood", async () => {
		const md = await Bun.file(reportPath).text();
		const rows = parseLockRows(md);

		for (const row of rows) {
			expect(row["Affected runs"].toLowerCase()).not.toContain("flood");
		}
	});

	test("Q4 tie section names D17, OMP-404 and L-CB-03", async () => {
		const md = await Bun.file(reportPath).text();
		const lines = md.split("\n");
		const start = lines.findIndex(line => line.trim() === "## Q4 tie");
		expect(start).toBeGreaterThanOrEqual(0);

		let end = lines.length;
		for (let i = start + 1; i < lines.length; i++) {
			if (lines[i].startsWith("## ")) {
				end = i;
				break;
			}
		}
		const section = lines.slice(start, end).join("\n");

		expect(section).toContain("D17");
		expect(section).toContain("OMP-404");
		expect(section).toContain("L-CB-03");
	});
});
