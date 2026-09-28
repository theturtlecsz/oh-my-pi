import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import {
	REPO_ROOT,
	SOURCE_DELIVERED_LABEL,
	readRepoText,
	unresolvedLinks,
} from "./fixtures/programme-claims";

const M0_DOC_REL = "docs/programme/M0-ACCEPTANCE.md";

describe("programme claims: M0 acceptance record", () => {
	test("both status lines hold the source delivered label", async () => {
		const content = await readRepoText(M0_DOC_REL);

		const headerStatusMatch = content.match(/^-\s*\*\*Status\*\*:\s*(.+)$/m);
		expect(headerStatusMatch).not.toBeNull();
		const headerStatus = headerStatusMatch![1];
		expect(headerStatus).toContain(SOURCE_DELIVERED_LABEL);
		expect(headerStatus).toContain("§3 component tests only");
		expect(headerStatus).toContain("[R00](R00-BASELINE-2026-09-25.md)");

		const criteriaStatusMatch = content.match(/^-\s*\*\*Milestone Criteria Status\*\*:\s*(.+)$/m);
		expect(criteriaStatusMatch).not.toBeNull();
		const criteriaStatus = criteriaStatusMatch![1];
		expect(criteriaStatus).toContain(SOURCE_DELIVERED_LABEL);
		expect(criteriaStatus).toContain("§3 component tests only");
		expect(criteriaStatus).toContain("[R00](R00-BASELINE-2026-09-25.md)");
	});

	test("unresolvedLinks for M0 acceptance record is empty", async () => {
		const content = await readRepoText(M0_DOC_REL);
		const missing = unresolvedLinks(M0_DOC_REL, content);
		expect(missing).toEqual([]);
	});

	test("pinned subset count N equals the file count under session-system/ecc/mirror", async () => {
		const content = await readRepoText(M0_DOC_REL);

		const mirrorDir = path.resolve(REPO_ROOT, "session-system/ecc/mirror");
		const entries = await fs.readdir(mirrorDir, { recursive: true, withFileTypes: true });
		const fileCount = entries.filter(e => e.isFile()).length;

		const subsetMatch = content.match(/Pinned subset:\s*(\d+)\s*files under `session-system\/ecc\/mirror\/`/);
		expect(subsetMatch).not.toBeNull();
		const declaredN = Number(subsetMatch![1]);

		expect(declaredN).toBe(fileCount);
		expect(fileCount).toBeGreaterThan(0);
	});

	test("unproven acceptance claims are absent", async () => {
		const content = await readRepoText(M0_DOC_REL);

		expect(content).not.toContain("Full pinned mirror");
		expect(content).not.toContain("Status**: Accepted");
	});

	test("unresolvedLinks helper correctly ignores external, absolute, and anchor links and strips frag/line", () => {
		const sampleMarkdown = [
			"- [HTTP](http://example.com)",
			"- [HTTPS](https://example.com/spec)",
			"- [Absolute](/var/log/syslog)",
			"- [Anchor](#milestone-criteria-status)",
			"- [Valid with frag](R00-BASELINE-2026-09-25.md#section-1)",
			"- [Valid with line](R00-BASELINE-2026-09-25.md:20)",
			"- [Valid with line:col](R00-BASELINE-2026-09-25.md:20:5)",
		].join("\n");

		expect(unresolvedLinks(M0_DOC_REL, sampleMarkdown)).toEqual([]);

		const brokenMarkdown = "- [Missing](nonexistent-acceptance-record.md)";
		expect(unresolvedLinks(M0_DOC_REL, brokenMarkdown)).toEqual(["nonexistent-acceptance-record.md"]);
	});
});
