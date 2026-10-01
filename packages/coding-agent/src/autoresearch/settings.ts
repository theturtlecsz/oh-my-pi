/**
 * Settings declared by autoresearch tournament judging (see `config/registry.ts`). Hidden from the
 * UI; consumed by the tournament runner.
 */
import { register } from "../config/registry";

export const cfgAutoresearchTournamentJudgeModel = register({
	id: "autoresearch.tournament.judgeModel",
	type: "string",
	default: "@smol",
});

export const cfgAutoresearchTournamentSecondJudgeModel = register({
	id: "autoresearch.tournament.secondJudgeModel",
	type: "string",
	default: undefined,
});

export const cfgAutoresearchTournamentSwissRoundCap = register({
	id: "autoresearch.tournament.swissRoundCap",
	type: "number",
	default: 5,
});

export const cfgAutoresearchTournamentMaxJudgeChars = register({
	id: "autoresearch.tournament.maxJudgeChars",
	type: "number",
	default: 6000,
});
