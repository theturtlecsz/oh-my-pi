import { describe, expect, it } from "bun:test";
import { Settings } from "../src/config/settings";
import {
	cfgAutoresearchTournamentJudgeModel,
	cfgAutoresearchTournamentMaxJudgeChars,
	cfgAutoresearchTournamentSecondJudgeModel,
	cfgAutoresearchTournamentSwissRoundCap,
} from "../src/autoresearch/settings";

describe("autoresearch tournament settings", () => {
	it("exposes the judge defaults the tournament runner consumes", () => {
		const settings = Settings.isolated();

		expect(cfgAutoresearchTournamentJudgeModel.get(settings)).toBe("@smol");
		expect(cfgAutoresearchTournamentSecondJudgeModel.get(settings)).toBeUndefined();
		expect(cfgAutoresearchTournamentSwissRoundCap.get(settings)).toBe(5);
		expect(cfgAutoresearchTournamentMaxJudgeChars.get(settings)).toBe(6000);
	});

	it("accepts a configured override in place of the default", () => {
		const settings = Settings.isolated({ "autoresearch.tournament.swissRoundCap": 3 });

		expect(cfgAutoresearchTournamentSwissRoundCap.get(settings)).toBe(3);
	});
});
