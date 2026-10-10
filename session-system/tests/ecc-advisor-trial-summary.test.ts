import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import {
	DEFAULT_ROOT,
	tally,
	validateCase,
	type CaseRow,
	type TallyCase,
	type TrialSummary,
} from "../../scripts/ecc-advisor-trial/tally.ts";

const ROOT = path.resolve(import.meta.dir, "../..", DEFAULT_ROOT);
const RUN_FILES = ["prompt.md", "advisor-transcript.jsonl", "notes.json"] as const;

interface Loaded {
	row: CaseRow;
	tallyCase: TallyCase;
	input: Parameters<typeof validateCase>[0];
}

async function loadAll(): Promise<Loaded[]> {
	const rows = (await Bun.file(path.join(ROOT, "cases.json")).json()) as CaseRow[];
	const out: Loaded[] = [];
	for (const row of rows) {
		const runDir = path.join(ROOT, "runs", row.id);
		const files: Record<string, Uint8Array> = {};
		for (const name of RUN_FILES) files[name] = await Bun.file(path.join(runDir, name)).bytes();
		const sha256sums = await Bun.file(path.join(runDir, "SHA256SUMS")).bytes();
		const labelsText = await Bun.file(path.join(ROOT, "labels", `${row.id}.json`)).text();
		const notes = JSON.parse(new TextDecoder().decode(files["notes.json"])) as { notes: { n: number }[] };
		const labels = JSON.parse(labelsText) as { labels: TallyCase["labels"][number][] };
		out.push({
			row,
			input: { caseRow: row, files, sha256sums, labelsText },
			tallyCase: {
				id: row.id,
				commit: row.commit,
				kind: row.kind,
				notes: notes.notes.map(note => ({ n: note.n })),
				labels: labels.labels.map(label => ({
					n: label.n,
					label: label.label,
					matches_known_defect: label.matches_known_defect === true,
				})),
			},
		});
	}
	return out;
}

describe("ECC advisor trial summary", () => {
	test("summary.json equals tally() over the files on disk and the rule", async () => {
		const loaded = await loadAll();
		const summary = (await Bun.file(path.join(ROOT, "summary.json")).json()) as TrialSummary;
		expect(summary).toEqual(tally(loaded.map(entry => entry.tallyCase)));
		expect(["keep", "drop"]).toContain(summary.recommendation);
		const expected =
			summary.useful >= summary.false_alarm && summary.known_defects_caught >= 1 ? "keep" : "drop";
		expect(summary.recommendation).toBe(expected);
	});

	test("every case validates and has a run dir and labels file", async () => {
		const loaded = await loadAll();
		const summary = (await Bun.file(path.join(ROOT, "summary.json")).json()) as TrialSummary;
		expect(summary.cases).toBeGreaterThanOrEqual(5);
		expect(summary.known_bad_cases).toBeGreaterThanOrEqual(1);
		for (const entry of loaded) {
			expect(validateCase(entry.input)).toEqual([]);
			expect((await fs.stat(path.join(ROOT, "runs", entry.row.id))).isDirectory()).toBe(true);
			expect((await fs.stat(path.join(ROOT, "labels", `${entry.row.id}.json`))).isFile()).toBe(true);
		}
	});
});
