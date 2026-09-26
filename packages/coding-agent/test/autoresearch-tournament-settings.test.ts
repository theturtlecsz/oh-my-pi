import { describe, expect, it } from "bun:test";
import { Settings } from "../src/config/settings";

describe("autoresearch tournament settings", () => {
	it("exposes the judge defaults the tournament runner consumes", () => {
		const settings = Settings.isolated();

		expect(settings.get("autoresearch.tournament.judgeModel")).toBe(" @smol");
		expect(settings.get("autoresearch.tournament.secondJudgeModel")).toBeUndefined();
		expect(settings.get("autoresearch.tournament.swissRoundCap")).toBe(5);
		expect(settings.get("autoresearch.tournament.maxJudgeChars")).toBe(6000);
	});

	it("accepts a configured override in place of the default", () => {
		const settings = Settings.isolated({ "autoresearch.tournament.swissRoundCap": 3 });

		expect(settings.get("autoresearch.tournament.swissRoundCap")).toBe(3);
	});
});
