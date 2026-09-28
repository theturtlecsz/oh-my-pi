import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import {
	REPO_ROOT,
	SOURCE_DELIVERED_LABEL,
	readRepoText,
	unresolvedLinks,
} from "./fixtures/programme-claims";

const ADR_REL = "docs/adr/0004-hard-padlock-control-plane.md";

// Any mention of the cockpit harness or a Grok Bot must be labelled as a demo.
const COCKPIT_CLAIM_PATTERN = /cockpit_verbs|grok ?bot/i;
const COCKPIT_CLAIM_LABEL = "not a Grok Bot integration";

/** Markdown files whose cockpit/Grok Bot claims must carry the demo label. */
async function labelledClaimDocs(): Promise<string[]> {
	const programmeDir = path.resolve(REPO_ROOT, "docs/programme");
	const adrDir = path.resolve(REPO_ROOT, "docs/adr");

	const mdFiles = async (dir: string): Promise<string[]> => {
		const entries = await fs.readdir(dir);
		return entries.filter(name => name.endsWith(".md")).map(name => path.join(dir, name));
	};

	return [
		path.resolve(REPO_ROOT, "MASTER.md"),
		...(await mdFiles(programmeDir)),
		...(await mdFiles(adrDir)),
	];
}

describe("programme claims: ADR 0004 horizon evidence", () => {
	test("status line and evidence heading hold the source delivered label", async () => {
		const content = await readRepoText(ADR_REL);

		const statusMatch = content.match(/^\*\*Status:\*\*\s*(.+)$/m);
		expect(statusMatch).not.toBeNull();
		expect(statusMatch![1]).toContain(SOURCE_DELIVERED_LABEL);

		const headingMatch = content.match(/^##\s*Evidence cited \(A\+B\)(.*)$/m);
		expect(headingMatch).not.toBeNull();
		expect(headingMatch![1]).toContain(SOURCE_DELIVERED_LABEL);
	});

	test("unresolvedLinks for ADR 0004 is empty", async () => {
		const content = await readRepoText(ADR_REL);
		expect(unresolvedLinks(ADR_REL, content)).toEqual([]);
	});

	test("superseded claim language is absent", async () => {
		const content = await readRepoText(ADR_REL);
		expect(content.toLowerCase()).not.toContain("proved");
		expect(content.toLowerCase()).not.toContain("grokbot");
	});

	test("every cockpit_verbs / Grok Bot claim is labelled as a demo", async () => {
		const docs = await labelledClaimDocs();
		const matches: string[] = [];
		const unlabelled: string[] = [];

		for (const doc of docs) {
			const lines = (await Bun.file(doc).text()).split("\n");
			for (const line of lines) {
				if (!COCKPIT_CLAIM_PATTERN.test(line)) continue;
				matches.push(`${path.relative(REPO_ROOT, doc)}: ${line}`);
				if (!line.includes(COCKPIT_CLAIM_LABEL)) {
					unlabelled.push(`${path.relative(REPO_ROOT, doc)}: ${line}`);
				}
			}
		}

		// The sweep must observe the labelled claim, otherwise it proves nothing.
		expect(matches.length).toBeGreaterThan(0);
		expect(unlabelled).toEqual([]);
	});
});
