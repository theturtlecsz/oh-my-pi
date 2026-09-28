import { describe, expect, test } from "bun:test";
import {
	SOURCE_DELIVERED_LABEL,
	readRepoText,
	unresolvedLinks,
} from "./fixtures/programme-claims";

const PACKET_REL = "docs/programme/E1-CONTRACT-APPROVAL-PACKET.md";
const APPROVAL_REL = "python/omp-work/src/omp_work/contracts/v1/approval.json";

/**
 * Text of the first paragraph after the document title, before the next heading.
 */
function paragraphUnderTitle(markdown: string): string {
	const lines = markdown.split(/\r?\n/);
	const titleAt = lines.findIndex(line => line.startsWith("# "));
	if (titleAt < 0) return "";
	const collected: string[] = [];
	for (const line of lines.slice(titleAt + 1)) {
		if (/^#{1,6}\s/.test(line)) break;
		if (line.trim() === "") {
			if (collected.length > 0) break;
			continue;
		}
		collected.push(line);
	}
	return collected.join("\n");
}

/**
 * Body of a level-2 section, up to the next level-2 heading.
 */
function sectionUnderHeading(markdown: string, heading: string): string {
	const lines = markdown.split(/\r?\n/);
	const start = lines.findIndex(line => line === `## ${heading}`);
	if (start < 0) return "";
	const body: string[] = [];
	for (const line of lines.slice(start + 1)) {
		if (line.startsWith("## ")) break;
		body.push(line);
	}
	return body.join("\n");
}

describe("programme claims: E1 contract approval packet", () => {
	test("the paragraph under the title holds the source delivered label", async () => {
		const content = await readRepoText(PACKET_REL);
		const paragraph = paragraphUnderTitle(content);
		expect(paragraph).toContain("**Status:**");
		expect(paragraph).toContain(SOURCE_DELIVERED_LABEL);
	});

	test("unresolvedLinks for the packet is empty", async () => {
		const content = await readRepoText(PACKET_REL);
		expect(unresolvedLinks(PACKET_REL, content)).toEqual([]);
	});

	test("the Decision requested digest differs from approval.json contract_sha256", async () => {
		const content = await readRepoText(PACKET_REL);
		const section = sectionUnderHeading(content, "Decision requested");
		const named = section.match(/\b[0-9a-f]{64}\b/)?.[0];
		expect(named).toMatch(/^[0-9a-f]{64}$/);

		const approval = JSON.parse(await readRepoText(APPROVAL_REL)) as { contract_sha256?: unknown };
		expect(approval.contract_sha256).toMatch(/^[0-9a-f]{64}$/);
		expect(named).not.toBe(approval.contract_sha256);
	});
});
