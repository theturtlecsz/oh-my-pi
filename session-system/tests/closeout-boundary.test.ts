/**
 * HOME-114 regression (WEB-2 sequence): always-injected session guidance must
 * never instruct auto-running /summary or closeout, and must carry the
 * explicit-command boundary. These strings are delivered verbatim to agents.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { CHECKPOINT_CONTRACT, CLOSE_CONTRACT, CLOSEOUT_BOUNDARY, STOP_REMINDER_BOUNDARY } from "../extensions/linear-now";

const ss = path.resolve(import.meta.dir, "..");
const md = (rel: string) => fs.readFileSync(path.join(ss, rel), "utf8");
const injected: Record<string, string> = {
	"rules/linear-plan.md": md("rules/linear-plan.md"),
	"skills/summary/SKILL.md": md("skills/summary/SKILL.md"),
	"agents/AGENTS.md": md("agents/AGENTS.md"),
	CHECKPOINT_CONTRACT,
	CLOSE_CONTRACT,
	STOP_REMINDER_BOUNDARY,
};

// The exact instructions that caused the WEB-2 auto-closeout.
const BANNED = [
	"run /summary before new work",
	"Session closing → run /summary",
	"run /summary first",
	"asks to close out a session",
];

describe("HOME-114 closeout boundary", () => {
	test("no injected guidance instructs auto-running closeout", () => {
		const offenders = Object.entries(injected).flatMap(([name, text]) =>
			BANNED.filter(phrase => text.includes(phrase)).map(phrase => `${name}: ${phrase}`),
		);
		expect(offenders).toEqual([]);
	});
	test("digest contracts and stop reminder carry the explicit-command boundary", () => {
		expect(CHECKPOINT_CONTRACT).toContain(CLOSEOUT_BOUNDARY);
		expect(CLOSE_CONTRACT).toContain("owner-entered /summary");
		expect(STOP_REMINDER_BOUNDARY).toContain("literally enters /summary or /done");
	});
	test("summary skill gates on literal invocation", () => {
		expect(injected["skills/summary/SKILL.md"]).toContain("literally entered /summary");
	});
});
