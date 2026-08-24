import { describe, expect, test } from "bun:test";
import { sameSessionSections } from "../extensions/workflow/work";

describe("sameSessionSections", () => {
	test("parses Finding and Verification with Verification as the final section", () => {
		const body = "## Finding\nThe gate failed.\n## Verification\nFixed and the suite passes.\n";
		expect(sameSessionSections(body)).toEqual({
			finding: "The gate failed.",
			verification: "Fixed and the suite passes.",
		});
	});

	test("does not truncate content at embedded z/Z letters", () => {
		// Regression: `\Z` in a JS regex is a literal "z" under /i, not an
		// end-of-input anchor — z-bearing text used to fail or truncate.
		const body = "## Finding\nLazy quartz jargon.\n## Verification\nAnalyzed the fuzz horizon; zero defects.\n";
		expect(sameSessionSections(body)).toEqual({
			finding: "Lazy quartz jargon.",
			verification: "Analyzed the fuzz horizon; zero defects.",
		});
	});

	test("Finding stops at the next heading and Verification runs to end of input", () => {
		const body = "## Finding\nfirst\n## Verification\nsecond\nline two\n";
		const sections = sameSessionSections(body);
		expect(sections?.finding).toBe("first");
		expect(sections?.verification).toBe("second\nline two");
	});

	test("returns null when a section is missing or empty", () => {
		expect(sameSessionSections("## Finding\nonly finding\n")).toBeNull();
		expect(sameSessionSections("## Verification\nonly verification\n")).toBeNull();
		expect(sameSessionSections("## Finding\nx\n## Verification\n")).toBeNull();
		expect(sameSessionSections("no headings at all")).toBeNull();
	});
});
