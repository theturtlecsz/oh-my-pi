/**
 * Settings declared by the Jev (TypeSafe) decision client (see `config/registry.ts`). Hidden from
 * the UI; populate via config.yml or the TYPESAFE_API_KEY env var. `jev.enabled` gates every call;
 * the autoThinking/unexpectedStop/tournamentJudge flags are consumed by their features, not by the
 * client itself.
 */
import { register } from "../config/registry";

export const cfgJevEnabled = register({ id: "jev.enabled", type: "boolean", default: false });

export const cfgJevBaseUrl = register({ id: "jev.baseUrl", type: "string", default: "https://api.typesafe.ai" });

export const cfgJevAutoThinking = register({ id: "jev.autoThinking", type: "boolean", default: false });

export const cfgJevAutoThinkingConfidence = register({
	id: "jev.autoThinkingConfidence",
	type: "number",
	default: 0.5,
});

export const cfgJevAutoThinkingMaxSignal = register({
	id: "jev.autoThinkingMaxSignal",
	type: "number",
	default: 0.7,
});

export const cfgJevUnexpectedStop = register({ id: "jev.unexpectedStop", type: "boolean", default: false });

export const cfgJevUnexpectedStopThreshold = register({
	id: "jev.unexpectedStopThreshold",
	type: "number",
	default: 0.7,
});

export const cfgJevTournamentJudge = register({ id: "jev.tournamentJudge", type: "boolean", default: false });
