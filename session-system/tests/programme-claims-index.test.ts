import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import {
	REPO_ROOT,
	SOURCE_DELIVERED_LABEL,
	readRepoText,
	unresolvedLinks,
} from "./fixtures/programme-claims";

const README_REL = "docs/programme/README.md";
const SECTION_HEADING = "## Claim status (D10)";

/** Body of the claim-status section, up to the next level-2 heading or end of file. */
function claimStatusSection(markdown: string): string {
	const lines = markdown.split(/\r?\n/);
	const start = lines.findIndex(line => line.trim() === SECTION_HEADING);
	if (start < 0) return "";
	const body: string[] = [];
	for (const line of lines.slice(start + 1)) {
		if (line.startsWith("## ")) break;
		body.push(line);
	}
	return body.join("\n");
}

/** In-repo markdown link targets named inside `section`, resolved against docs/programme. */
function inRepoMarkdownLinks(section: string): string[] {
	const programmeDir = path.resolve(REPO_ROOT, "docs/programme");
	const targets: string[] = [];
	for (const match of section.matchAll(/!?\[(?:[^\]]*)\]\(([^)]+)\)/g)) {
		const raw = match[1].trim().split(/\s+/)[0];
		if (!raw || /^https?:\/\//i.test(raw) || raw.startsWith("/") || raw.startsWith("#")) continue;
		const targetPath = raw.split("#")[0];
		if (!targetPath.endsWith(".md")) continue;
		targets.push(path.resolve(programmeDir, targetPath));
	}
	return targets;
}

describe("programme claims: README claim-status index", () => {
	test("the claim-status section carries the source delivered label and points at the Work Ledger", async () => {
		const content = await readRepoText(README_REL);
		const section = claimStatusSection(content);

		expect(section).not.toBe("");
		expect(section).toContain(SOURCE_DELIVERED_LABEL);
		expect(section).toContain("Work Ledger");
	});

	test("unresolvedLinks for README is empty", async () => {
		const content = await readRepoText(README_REL);
		expect(unresolvedLinks(README_REL, content)).toEqual([]);
	});

	test("every in-repo markdown file linked from the section contains the source delivered label", async () => {
		const section = claimStatusSection(await readRepoText(README_REL));
		const linked = inRepoMarkdownLinks(section);

		// The sweep must observe the linked records, otherwise it proves nothing.
		expect(linked.length).toBeGreaterThan(0);

		const unlabelled: string[] = [];
		for (const target of linked) {
			const text = await Bun.file(target).text();
			if (!text.includes(SOURCE_DELIVERED_LABEL)) {
				unlabelled.push(path.relative(REPO_ROOT, target));
			}
		}

		expect(unlabelled).toEqual([]);
	});
});
