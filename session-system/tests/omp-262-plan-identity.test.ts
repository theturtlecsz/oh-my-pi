import { describe, expect, test } from "bun:test";
import { plannedCandidateId } from "../extensions/workflow/work";

describe("OMP-262 planned candidate identity", () => {
	const fixedWork = "work-fixed";
	const fixedRevision = "revision-fixed";
	const fixedPlanSha = "0".repeat(64);
	const expectedFixedId = "c2fe47bf-1f4c-5571-917f-99164bbf7be4";

	test("fixed vector equals pre-change stableId literal UUID", () => {
		const id = plannedCandidateId(fixedWork, fixedRevision, fixedPlanSha);
		expect(id).toBe(expectedFixedId);
	});

	test("changing only revision gives a different ID", () => {
		const baseId = plannedCandidateId(fixedWork, fixedRevision, fixedPlanSha);
		const changedRevisionId = plannedCandidateId(fixedWork, "revision-changed", fixedPlanSha);
		expect(changedRevisionId).not.toBe(baseId);
	});

	test("changing only plan hash gives a different ID", () => {
		const baseId = plannedCandidateId(fixedWork, fixedRevision, fixedPlanSha);
		const changedPlanId = plannedCandidateId(fixedWork, fixedRevision, "1".repeat(64));
		expect(changedPlanId).not.toBe(baseId);
	});
});
