import { describe, expect, test } from "bun:test";
import { readRepoText } from "./fixtures/programme-claims";

const DOCTRINE_REL = "session-system/agents/omp-AGENTS.md";

const CHRIS_WORDS =
	"we're only working on OMP here. Media-discovery is a completely seperate project and we should have better guardrails here. Lets not confuse the 2 projects...It's ledger items should be seperate";

function h2Headings(markdown: string): string[] {
	return [...markdown.matchAll(/^## .+$/gm)].map(match => match[0]);
}

function sectionBody(markdown: string, headingLine: string): string {
	const lines = markdown.split("\n");
	const start = lines.findIndex(line => line === headingLine);
	if (start === -1) throw new Error(`missing heading: ${headingLine}`);
	const body: string[] = [];
	for (let i = start + 1; i < lines.length; i++) {
		if (lines[i].startsWith("## ")) break;
		body.push(lines[i]);
	}
	return body.join("\n");
}

function sentences(text: string): string[] {
	return text
		.replace(/\s+/g, " ")
		.trim()
		.split(/(?<=[.!?])\s+/)
		.map(sentence => sentence.trim())
		.filter(Boolean);
}

describe("project separation doctrine", () => {
	test("exactly one Project separation heading follows Issue tracking law", async () => {
		const headings = h2Headings(await readRepoText(DOCTRINE_REL));
		const project = headings.filter(heading => heading.startsWith("## Project separation"));
		expect(project).toEqual(["## Project separation (owner directive, 2026-10-02, OMP-527)"]);

		const issue = headings.findIndex(heading => heading.startsWith("## Issue tracking law"));
		expect(issue).toBeGreaterThanOrEqual(0);
		expect(headings[issue + 1]).toBe(project[0]);
	});

	test("one sentence names read, change and ask unless Chris names Media Discovery", async () => {
		const doc = await readRepoText(DOCTRINE_REL);
		const heading = h2Headings(doc).find(line => line.startsWith("## Project separation"));
		expect(heading).toBeDefined();
		const hits = sentences(sectionBody(doc, heading!)).filter(
			sentence =>
				/\bread\b/.test(sentence) &&
				/\bchange\b/.test(sentence) &&
				/\bask\b/.test(sentence) &&
				sentence.includes("unless Chris names Media Discovery"),
		);
		expect(hits).toHaveLength(1);
	});

	test("section quotes Chris's words exactly", async () => {
		const doc = await readRepoText(DOCTRINE_REL);
		const heading = h2Headings(doc).find(line => line.startsWith("## Project separation"));
		expect(heading).toBeDefined();
		expect(sectionBody(doc, heading!)).toContain(CHRIS_WORDS);
	});
});
