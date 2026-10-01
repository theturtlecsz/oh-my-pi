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

/** Upstream-owned settings registry modules a lockdown proposal may link without a fork patch. */
const UPSTREAM_SETTINGS_MODULES = new Set(["packages/coding-agent/src/tools/settings.ts"]);

describe("unattended lockdown report - runtime defaults", () => {
	const readmePath = path.join(REPORT_DIR, "README.md");
	const runtimePath = path.join(REPORT_DIR, "runtime-defaults.md");
	const inventoryPath = path.join(REPO_ROOT, "docs/upstream-fork-inventory.tsv");

	const EXPECTED_CODE_FILES = [
		"packages/coding-agent/src/tools/settings.ts",
		"packages/coding-agent/src/task/executor.ts",
		"packages/coding-agent/src/session/agent-session.ts",
		"packages/coding-agent/src/extensibility/extensions/runner.ts",
		"packages/coding-agent/src/extensibility/extensions/loader.ts",
		"packages/metaharness/src/server.ts",
	];

	test("report files exist on disk", () => {
		expect(fs.existsSync(readmePath)).toBe(true);
		expect(fs.existsSync(runtimePath)).toBe(true);
	});

	test("unresolvedLinks is empty for both README.md and runtime-defaults.md", async () => {
		const readmeMd = await Bun.file(readmePath).text();
		const runtimeMd = await Bun.file(runtimePath).text();

		expect(unresolvedLinks("README.md", readmeMd)).toEqual([]);
		expect(unresolvedLinks("runtime-defaults.md", runtimeMd)).toEqual([]);
	});

	test("parses exactly 5 runtime default lock rows with non-empty cells and valid IDs L-RT-01..05", async () => {
		const runtimeMd = await Bun.file(runtimePath).text();
		const rows = parseLockRows(runtimeMd);

		expect(rows).toHaveLength(5);

		const expectedIds = ["L-RT-01", "L-RT-02", "L-RT-03", "L-RT-04", "L-RT-05"];
		expect(rows.map(r => r.ID)).toEqual(expectedIds);

		for (const row of rows) {
			for (const col of LOCK_COLUMNS) {
				expect(row[col]).toBeDefined();
				expect(row[col].trim().length).toBeGreaterThan(0);
			}
		}
	});

	test("divergence is adds for 01-04, modified for 05, with (applied: OMP-396-s02) on 05 only", async () => {
		const runtimeMd = await Bun.file(runtimePath).text();
		const rows = parseLockRows(runtimeMd);

		expect(rows).toHaveLength(5);

		for (let i = 0; i < 4; i++) {
			const div = rows[i]["Upstream divergence"].trim();
			expect(div).toBe("adds");
			expect(div).not.toContain("OMP-396-s02");
		}

		const div05 = rows[4]["Upstream divergence"].trim();
		expect(div05.startsWith("modified")).toBe(true);
		expect(div05.endsWith("(applied: OMP-396-s02)")).toBe(true);

		for (let i = 0; i < 5; i++) {
			const hasApplied = rows[i]["Upstream divergence"].includes("(applied: OMP-396-s02)");
			expect(hasApplied).toBe(i === 4);
		}
	});

	test("Affected runs never mentions flood", async () => {
		const runtimeMd = await Bun.file(runtimePath).text();
		const rows = parseLockRows(runtimeMd);

		for (const row of rows) {
			expect(row["Affected runs"].toLowerCase()).not.toContain("flood");
		}
	});

	test("links cover all 6 code files and each has a shared row in upstream-fork-inventory.tsv", async () => {
		const runtimeMd = await Bun.file(runtimePath).text();
		const rows = parseLockRows(runtimeMd);

		const linkRegex = /\[(?:[^\]]*)\]\(([^)]+)\)/g;
		const linkedFiles: string[] = [];

		for (const row of rows) {
			let match: RegExpExecArray | null;
			while ((match = linkRegex.exec(row.Location)) !== null) {
				const raw = match[1].split("#")[0].split("?")[0].trim();
				const absPath = path.resolve(REPORT_DIR, raw);
				const relToRepo = path.relative(REPO_ROOT, absPath);
				linkedFiles.push(relToRepo);
			}
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

		const packageLinks = linkedFiles.filter(f => f.startsWith("packages/"));
		expect(packageLinks.length).toBeGreaterThanOrEqual(6);

		// Upstream 18.x declares settings in per-domain registry modules the fork does
		// not patch, so a settings default proposal links an upstream-owned file.
		for (const pkgFile of packageLinks.filter(f => !UPSTREAM_SETTINGS_MODULES.has(f))) {
			expect(sharedRows.has(pkgFile)).toBe(true);
		}
	});
});
