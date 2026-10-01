import { describe, expect, test } from "bun:test";
import type { ChangelogEntry } from "../../src/utils/changelog";
import { selectStartupChangelog } from "../../src/utils/changelog";

describe("startup changelog lex cap", () => {
	test("counts the capped prefix of one huge release and drops text past the cap", () => {
		const content = `### Added\n\n- A\n- B\n- ${"x".repeat(1024 * 1024)}\n- AFTER-CAP`;
		const entries: ChangelogEntry[] = [{ major: 2, minor: 0, patch: 0, content }];
		const selection = selectStartupChangelog(entries, "1.0.0", "2.0.0");

		expect(selection.changeCount).toBe(3);
		expect(selection.categoryCounts).toEqual({ Added: 3 });
		expect(selection.truncated).toBe(true);
	});
});
