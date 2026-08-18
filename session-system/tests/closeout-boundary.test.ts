/** HOME-114/HOME-122 contract strings injected verbatim into agent context. */
import { describe, expect, test } from "bun:test";
import { CLOSEOUT_BOUNDARY, STOP_REMINDER_BOUNDARY, WORKFLOW_SEQUENCE } from "../extensions/work-now";

const injected = { CLOSEOUT_BOUNDARY, STOP_REMINDER_BOUNDARY, WORKFLOW_SEQUENCE };
const BANNED = ["run /summary before new work", "Session closing → run /summary", "asks to close out a session"];

describe("injected workflow boundary", () => {
	test("never instructs inferred closeout", () => {
		const offenders = Object.entries(injected).flatMap(([name, text]) =>
			BANNED.filter(phrase => text.includes(phrase)).map(phrase => `${name}: ${phrase}`),
		);
		expect(offenders).toEqual([]);
	});

	test("carries the canonical owner workflow and literal close boundary", () => {
		expect(WORKFLOW_SEQUENCE).toContain("/intake creates and selects → /plan approves, stamps, and executes");
		expect(WORKFLOW_SEQUENCE).toContain("/summary reviews → /done closes");
		expect(CLOSEOUT_BOUNDARY).toContain("Chris literally enters them");
		expect(STOP_REMINDER_BOUNDARY).toContain("Never narrate bookkeeping");
	});
});
