import * as crypto from "node:crypto";
import * as path from "node:path";
import { type } from "@oh-my-pi/omptype";
import type { Usage } from "@oh-my-pi/pi-ai";
import { Text } from "@oh-my-pi/pi-tui";
import { getDefault, isSettingsInitialized, type Settings, settings } from "../../config/settings";
import type { ExtensionContext, ToolDefinition } from "../../extensibility/extensions";
import type { Theme } from "../../modes/theme/theme";
import { replaceTabs, TRUNCATE_LENGTHS, truncateToWidth } from "../../tools/render-utils";
import { addUsageTotals } from "../../utils/usage-totals";
import { createModelJudge, createModelSummarizer } from "../tournament/model-judge";
import { type HypothesisInput, prepareHypotheses, type Summarizer } from "../tournament/prepare";
import { runTournament } from "../tournament/runner";
import { TOURNAMENT_LABEL, type TournamentJudge, type TournamentResult } from "../tournament/types";
import type { AutoresearchToolFactoryOptions } from "../types";

const hypothesisTournamentSchema = type({
	input_path: type("string").describe("Path to input JSON file containing question and hypotheses"),
	"seed?": type("number.integer").describe("Optional random seed for deterministic tournament evaluation"),
});

export interface HypothesisTournamentDetails {
	usage: Usage;
	tournamentPath: string;
	result: TournamentResult;
}

export interface TournamentInputPayload {
	question: string;
	hypotheses: HypothesisInput[];
}

export interface HypothesisTournamentDeps {
	createJudges?: (
		ctx: ExtensionContext,
		seed: number,
		options?: { signal?: AbortSignal },
	) => Promise<TournamentJudge[]> | TournamentJudge[];
	createSummarizer?: (ctx: ExtensionContext, options?: { signal?: AbortSignal }) => Promise<Summarizer> | Summarizer;
}

async function defaultCreateJudges(
	ctx: ExtensionContext,
	seed: number,
	signal?: AbortSignal,
): Promise<TournamentJudge[]> {
	const activeSettings = (ctx as { settings?: Settings }).settings ?? (isSettingsInitialized() ? settings : undefined);
	const judgeModelSpec =
		activeSettings?.get("autoresearch.tournament.judgeModel") ??
		getDefault("autoresearch.tournament.judgeModel") ??
		"@smol";
	const secondJudgeModelSpec =
		activeSettings?.get("autoresearch.tournament.secondJudgeModel") ??
		getDefault("autoresearch.tournament.secondJudgeModel");

	const primaryModel = ctx.models.resolve(judgeModelSpec);
	if (!primaryModel) {
		throw new Error(`Failed to resolve tournament judge model: "${judgeModelSpec}"`);
	}
	const sessionId = ctx.sessionManager.getSessionId();
	const primaryApiKey = await ctx.modelRegistry.getApiKey(primaryModel, sessionId);
	const primaryFamily = ctx.models.family(primaryModel);

	const judges: TournamentJudge[] = [
		createModelJudge({
			id: primaryModel.id,
			family: primaryFamily,
			model: primaryModel,
			apiKey: primaryApiKey,
			seed,
			signal,
		}),
	];

	if (secondJudgeModelSpec) {
		const secondaryModel = ctx.models.resolve(secondJudgeModelSpec);
		if (!secondaryModel) {
			throw new Error(`Failed to resolve tournament second judge model: "${secondJudgeModelSpec}"`);
		}
		const secondaryApiKey = await ctx.modelRegistry.getApiKey(secondaryModel, sessionId);
		const secondaryFamily = ctx.models.family(secondaryModel);
		judges.push(
			createModelJudge({
				id: secondaryModel.id,
				family: secondaryFamily,
				model: secondaryModel,
				apiKey: secondaryApiKey,
				seed: (seed + 1) | 0,
				signal,
			}),
		);
	}

	return judges;
}

async function defaultCreateSummarizer(ctx: ExtensionContext): Promise<Summarizer> {
	const activeSettings = (ctx as { settings?: Settings }).settings ?? (isSettingsInitialized() ? settings : undefined);
	const judgeModelSpec =
		activeSettings?.get("autoresearch.tournament.judgeModel") ??
		getDefault("autoresearch.tournament.judgeModel") ??
		"@smol";
	const model = ctx.models.resolve(judgeModelSpec);
	if (!model) {
		throw new Error(`Failed to resolve tournament summarizer model: "${judgeModelSpec}"`);
	}
	const sessionId = ctx.sessionManager.getSessionId();
	const apiKey = await ctx.modelRegistry.getApiKey(model, sessionId);
	return createModelSummarizer({ model, apiKey });
}

export function createHypothesisTournamentTool(
	_options: AutoresearchToolFactoryOptions,
	deps?: HypothesisTournamentDeps,
): ToolDefinition<typeof hypothesisTournamentSchema, HypothesisTournamentDetails> {
	return {
		name: "hypothesis_tournament",
		label: "Hypothesis Tournament",
		description:
			"Run a blinded pairwise tournament across candidate hypotheses to evaluate and rank them under Bradley-Terry aggregation.",
		parameters: hypothesisTournamentSchema,
		defaultInactive: true,
		async execute(_toolCallId, params, signal, _onUpdate, ctx) {
			const resolvedInputPath = path.isAbsolute(params.input_path)
				? params.input_path
				: path.resolve(ctx.cwd, params.input_path);

			const file = Bun.file(resolvedInputPath);
			if (!(await file.exists())) {
				throw new Error(`Tournament input file not found: ${resolvedInputPath}`);
			}

			let payload: TournamentInputPayload;
			try {
				const content = await file.text();
				payload = JSON.parse(content) as TournamentInputPayload;
			} catch (err) {
				throw new Error(
					`Failed to parse tournament input file at ${resolvedInputPath}: ${err instanceof Error ? err.message : String(err)}`,
				);
			}

			if (!payload || typeof payload !== "object" || !Array.isArray(payload.hypotheses)) {
				throw new Error(`Tournament input at ${resolvedInputPath} must contain a "hypotheses" array`);
			}

			if (typeof payload.question !== "string" || payload.question.trim().length === 0) {
				throw new Error(`Tournament input at ${resolvedInputPath} must contain a non-empty "question" string`);
			}

			const seed = params.seed !== undefined ? params.seed : Math.floor(Math.random() * 0x7fffffff) | 0;

			const activeSettings =
				(ctx as { settings?: Settings }).settings ?? (isSettingsInitialized() ? settings : undefined);
			const swissRoundCap =
				activeSettings?.get("autoresearch.tournament.swissRoundCap") ??
				getDefault("autoresearch.tournament.swissRoundCap");
			const maxJudgeChars =
				activeSettings?.get("autoresearch.tournament.maxJudgeChars") ??
				getDefault("autoresearch.tournament.maxJudgeChars");

			const summarizer = deps?.createSummarizer
				? await deps.createSummarizer(ctx, { signal })
				: await defaultCreateSummarizer(ctx);

			const judges = deps?.createJudges
				? await deps.createJudges(ctx, seed, { signal })
				: await defaultCreateJudges(ctx, seed, signal);

			const { hypotheses, usage: prepareUsage } = await prepareHypotheses(payload.hypotheses, {
				maxJudgeChars,
				summarizer,
				signal,
			});

			const tournamentId = crypto.randomUUID();
			const tournament = await runTournament({
				id: tournamentId,
				question: payload.question,
				hypotheses,
				judges,
				swissRoundCap,
				seed,
				signal,
			});
			addUsageTotals(tournament.usage, prepareUsage);

			const tournamentPath = resolvedInputPath.endsWith(".json")
				? `${resolvedInputPath.slice(0, -5)}.tournament.json`
				: `${resolvedInputPath}.tournament.json`;

			await Bun.write(tournamentPath, JSON.stringify(tournament, null, 2));

			const lines: string[] = [TOURNAMENT_LABEL];
			for (const entry of tournament.result.ranking) {
				lines.push(
					`${entry.rank}. ${entry.title} (score: ${entry.score}, spread: ${entry.spread}, comparisons: ${entry.comparisons})`,
				);
			}
			const ruleDesc =
				tournament.schedule.kind === "round-robin"
					? "Schedule rule: round-robin"
					: `Schedule rule: swiss (round cap: ${tournament.schedule.roundCap})`;
			lines.push(ruleDesc);
			if (tournament.result.familyAgreement) {
				const agreement = tournament.result.familyAgreement;
				lines.push(
					`Family agreement: ${(agreement.agreement * 100).toFixed(1)}% across ${agreement.pairs} pairs (${agreement.families.join(" vs ")})`,
				);
			}
			lines.push(`Output: ${tournamentPath}`);

			return {
				content: [
					{
						type: "text",
						text: lines.join("\n"),
					},
				],
				details: {
					usage: tournament.usage,
					tournamentPath,
					result: tournament.result,
				},
			};
		},
		renderCall(args, _options, theme): Text {
			const preview = truncateToWidth(replaceTabs(args.input_path ?? ""), TRUNCATE_LENGTHS.LONG);
			return new Text(
				`${theme.fg("toolTitle", theme.bold("hypothesis_tournament"))} ${theme.fg("muted", preview)}`,
				0,
				0,
			);
		},
		renderResult(result, _options, theme: Theme): Text {
			const text = result.content.find(part => part.type === "text")?.text ?? "";
			const sanitized = text
				.split("\n")
				.map(line => truncateToWidth(replaceTabs(line), TRUNCATE_LENGTHS.LINE))
				.join("\n");
			return new Text(theme.fg("muted", sanitized), 0, 0);
		},
	};
}
