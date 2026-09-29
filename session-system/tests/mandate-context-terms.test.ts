import { describe, expect, test } from "bun:test";
import { readRepoText } from "./fixtures/programme-claims";

const CONTEXT_DOC_REL = "CONTEXT.md";
const SECTION = "Mission Architecture";
const TERMS = ["Mission", "Decision record", "Client contract", "Capability layer"] as const;

interface TermEntry {
	term: string;
	section: string;
	definition: string;
	avoid: string;
}

const TERM_LINE = /^\*\*(.+?)\*\*:\s*(.*)$/;
const AVOID_LINE = /^_Avoid_:\s*(.*)$/;
const SECTION_LINE = /^###\s+(.*\S)\s*$/;

function parseTermEntries(content: string): TermEntry[] {
	const lines = content.split("\n");
	const entries: TermEntry[] = [];
	let section = "";

	for (let i = 0; i < lines.length; i++) {
		const sectionMatch = SECTION_LINE.exec(lines[i]);
		if (sectionMatch) {
			section = sectionMatch[1];
			continue;
		}

		const termMatch = TERM_LINE.exec(lines[i]);
		if (!termMatch) continue;

		let definition = termMatch[2].trim();
		let avoid = "";
		for (let j = i + 1; j < lines.length; j++) {
			const line = lines[j];
			if (TERM_LINE.test(line) || SECTION_LINE.test(line)) break;

			const avoidMatch = AVOID_LINE.exec(line);
			if (avoidMatch) {
				avoid = avoidMatch[1].trim();
				break;
			}
			const trimmed = line.trim();
			if (trimmed && !definition) definition = trimmed;
		}

		entries.push({ term: termMatch[1].trim(), section, definition, avoid });
	}

	return entries;
}

describe("CONTEXT.md mission architecture terms", () => {
	test("each of the four terms appears once under Mission Architecture with a definition and an _Avoid_ line", async () => {
		const entries = parseTermEntries(await readRepoText(CONTEXT_DOC_REL));

		for (const term of TERMS) {
			const matches = entries.filter(entry => entry.term === term);
			expect(matches).toHaveLength(1);
			expect(matches[0].section).toBe(SECTION);
			expect(matches[0].definition.length).toBeGreaterThan(0);
			expect(matches[0].avoid.length).toBeGreaterThan(0);
		}
	});

	test("no term name repeats anywhere in the file", async () => {
		const entries = parseTermEntries(await readRepoText(CONTEXT_DOC_REL));
		const names = entries.map(entry => entry.term);
		expect(new Set(names).size).toBe(names.length);
	});

	test("mission, decision record and client contract definitions carry their required detail", async () => {
		const entries = parseTermEntries(await readRepoText(CONTEXT_DOC_REL));
		const byTerm = new Map(entries.map(entry => [entry.term, entry]));

		expect(byTerm.get("Mission")?.definition).toContain("acceptance criteria");
		expect(byTerm.get("Decision record")?.definition).toContain("saved state");
		expect(byTerm.get("Client contract")?.definition).toContain("decision records");
	});
});
