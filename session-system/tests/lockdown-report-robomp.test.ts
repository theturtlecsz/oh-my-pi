import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import {
	LOCK_COLUMNS,
	REPORT_DIR,
	REPO_ROOT,
	parseLockRows,
	unresolvedLinks,
} from "./fixtures/lockdown-report.ts";

describe("unattended lockdown report - robomp", () => {
	const robompPath = path.join(REPORT_DIR, "robomp.md");
	const inventoryPath = path.join(REPO_ROOT, "docs/upstream-fork-inventory.tsv");

	const EXPECTED_CODE_FILES = [
		"python/robomp/src/worker.py",
		"python/omp-rpc/src/omp_rpc/client.py",
		"python/robomp/src/git_ops.py",
		"packages/coding-agent/src/tools/approval.ts",
		"python/robomp/docker-compose.yml",
		"python/robomp/entrypoint.sh",
	];

	test("report file exists on disk", () => {
		expect(fs.existsSync(robompPath)).toBe(true);
	});

	test("unresolvedLinks is empty for robomp.md", async () => {
		const robompMd = await Bun.file(robompPath).text();
		expect(unresolvedLinks("robomp.md", robompMd)).toEqual([]);
	});

	test("parses exactly 3 robomp lock rows with non-empty cells and valid IDs L-RB-01..03", async () => {
		const robompMd = await Bun.file(robompPath).text();
		const rows = parseLockRows(robompMd);

		expect(rows).toHaveLength(3);

		const expectedIds = ["L-RB-01", "L-RB-02", "L-RB-03"];
		expect(rows.map(r => r.ID)).toEqual(expectedIds);

		for (const row of rows) {
			for (const col of LOCK_COLUMNS) {
				expect(row[col]).toBeDefined();
				expect(row[col].trim().length).toBeGreaterThan(0);
			}
		}
	});

	test("divergences are new/new/none with no (decided:/(applied: suffix", async () => {
		const robompMd = await Bun.file(robompPath).text();
		const rows = parseLockRows(robompMd);

		expect(rows).toHaveLength(3);
		expect(rows[0]["Upstream divergence"].trim()).toBe("new");
		expect(rows[1]["Upstream divergence"].trim()).toBe("new");
		expect(rows[2]["Upstream divergence"].trim()).toBe("none");

		for (const row of rows) {
			const div = row["Upstream divergence"];
			expect(div).not.toContain("(decided:");
			expect(div).not.toContain("(applied:");
		}
	});

	test("L-RB-03 Location is external:, others contain links", async () => {
		const robompMd = await Bun.file(robompPath).text();
		const rows = parseLockRows(robompMd);

		expect(rows).toHaveLength(3);
		expect(rows[2].Location.trim()).toBe("external:");

		const linkRegex = /\[(?:[^\]]*)\]\(([^)]+)\)/;
		expect(linkRegex.test(rows[0].Location)).toBe(true);
		expect(linkRegex.test(rows[1].Location)).toBe(true);
		expect(linkRegex.test(rows[2].Location)).toBe(false);
	});

	test("links include worker.py, client.py, git_ops.py, approval.ts, docker-compose.yml, entrypoint.sh", async () => {
		const robompMd = await Bun.file(robompPath).text();
		const linkRegex = /\[(?:[^\]]*)\]\(([^)]+)\)/g;
		const linkedFiles: string[] = [];

		let match: RegExpExecArray | null;
		while ((match = linkRegex.exec(robompMd)) !== null) {
			const raw = match[1].split("#")[0].split("?")[0].trim();
			const absPath = path.resolve(REPORT_DIR, raw);
			const relToRepo = path.relative(REPO_ROOT, absPath);
			linkedFiles.push(relToRepo);
		}

		for (const codeFile of EXPECTED_CODE_FILES) {
			expect(linkedFiles).toContain(codeFile);
		}
	});

	test("L-RB-02 Breaks names submit_pr_review and says edits still run", async () => {
		const robompMd = await Bun.file(robompPath).text();
		const rows = parseLockRows(robompMd);

		const row02 = rows.find(r => r.ID === "L-RB-02");
		expect(row02).toBeDefined();
		expect(row02!.Breaks).toContain("submit_pr_review");
		expect(row02!.Breaks.toLowerCase()).toContain("edits still run");
	});

	test("python/robomp/src/worker.py has one shared inventory row, not a fork-only row", async () => {
		const robompWorker = "python/robomp/src/worker.py";
		const rowIsRobompWorker = (line: string) => line.split("\t")[0]?.trim() === robompWorker;

		const inventoryText = await Bun.file(inventoryPath).text();
		const rows = inventoryText.split("\n").filter(line => rowIsRobompWorker(line));
		expect(rows).toHaveLength(1);
		const cells = rows[0].split("\t");
		expect(cells[1]).toBe("shared");
		expect(cells[2]).toBe("modified");

		const otherWorkerLine = "python/omp-work/src/omp_work/jobs/worker.py\tfork-only\tadded";
		expect(rowIsRobompWorker(otherWorkerLine)).toBe(false);

		const robompWorkerLine = `${robompWorker}\tfork-only\tadded`;
		expect(rowIsRobompWorker(robompWorkerLine)).toBe(true);
	});

	test("Section ## Can a slot user read agent-home credentials? 1st line starts Code answer: yes", async () => {
		const robompMd = await Bun.file(robompPath).text();
		const sectionHeader = "## Can a slot user read agent-home credentials?";
		expect(robompMd).toContain(sectionHeader);

		const afterHeader = robompMd.slice(robompMd.indexOf(sectionHeader) + sectionHeader.length);
		const firstLine = afterHeader
			.split("\n")
			.map(line => line.trim())
			.find(line => line.length > 0);

		expect(firstLine).toBeDefined();
		expect(firstLine!.startsWith("Code answer: yes")).toBe(true);
	});
});
