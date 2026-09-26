import {
	CPK_SUPERVISION_RULE_CLASSES,
	type CpkSupervisionProposal,
	proposalFromAdvisorNote,
} from "../extensibility/cpk/supervision-proposal";
import {
	type CpkSupervisionArm,
	type CpkSupervisionArmInstance,
	type CpkSupervisionReplayReport,
	type CpkSupervisionReplaySession,
	replayCpkSupervision,
} from "../extensibility/cpk/supervision-replay";
import type { AdvisorCategory } from "./advise-tool";
import type { AdvisorSupervisionGate } from "./supervision-gate";
import type { AdvisorReplayEvent } from "./supervision-history";
import {
	type AdvisorSupervisionPath,
	AdvisorSupervisionPipeline,
	type AdvisorSupervisionPipelineReport,
} from "./supervision-pipeline";

export interface CreateAdvisorReplayArmOptions {
	structuredGate?: AdvisorSupervisionGate | (() => AdvisorSupervisionGate);
	canaryMaxDivergences?: number;
}

export interface AdvisorReplayArm extends CpkSupervisionArm<AdvisorReplayEvent> {
	reports(): Array<{ sessionId: string; report: AdvisorSupervisionPipelineReport }>;
}

export interface QualifyAdvisorSupervisionOptions {
	structuredGate?: AdvisorSupervisionGate | (() => AdvisorSupervisionGate);
}

export interface AdvisorSupervisionQualificationResult {
	structured: CpkSupervisionReplayReport;
	canary: CpkSupervisionReplayReport;
	canaryBudgetExceeded: string[];
}

function resolveStructuredGate(
	gate?: AdvisorSupervisionGate | (() => AdvisorSupervisionGate),
): AdvisorSupervisionGate | undefined {
	if (!gate) return undefined;
	if (typeof gate === "function") return gate();
	try {
		return new (gate.constructor as new () => AdvisorSupervisionGate)();
	} catch {
		return gate;
	}
}

/**
 * Creates a {@link replayCpkSupervision} arm driven by replaying {@link AdvisorReplayEvent}s
 * through an {@link AdvisorSupervisionPipeline}.
 *
 * Per session: builds a fresh pipeline. Delivery maps delivered advisor notes to CPK-6
 * proposals via {@link proposalFromAdvisorNote}. Deferred flushes preserve their original
 * advise-event transcript index.
 */
export function createAdvisorReplayArm(
	path: AdvisorSupervisionPath,
	opts?: CreateAdvisorReplayArmOptions,
): AdvisorReplayArm {
	const sessionPipelines: Array<{ sessionId: string; pipeline: AdvisorSupervisionPipeline }> = [];

	return {
		name: path,
		start(sessionId: string): CpkSupervisionArmInstance<AdvisorReplayEvent> {
			let currentEventIndex = 0;
			const buffer: CpkSupervisionProposal[] = [];

			const pipeline = new AdvisorSupervisionPipeline({
				path,
				canaryMaxDivergences: opts?.canaryMaxDivergences ?? 0,
				transcriptIndex: () => currentEventIndex,
				deliver: (note, severity, category, transcriptIndex) => {
					buffer.push(proposalFromAdvisorNote(note, severity, category, transcriptIndex ?? currentEventIndex));
				},
				...(opts?.structuredGate ? { structuredGate: resolveStructuredGate(opts.structuredGate) } : {}),
			});

			sessionPipelines.push({ sessionId, pipeline });

			return {
				async onEvent(event: AdvisorReplayEvent, index: number): Promise<unknown[]> {
					currentEventIndex = index;
					buffer.length = 0;
					if (event.type === "update") {
						pipeline.beginUpdate(event.inProgress);
					} else if (event.type === "advise") {
						await pipeline.tool.execute(`advise-${index}`, {
							note: event.note,
							...(event.severity !== undefined ? { severity: event.severity } : {}),
							category: event.category as AdvisorCategory,
						});
					}
					return [...buffer];
				},
			};
		},
		reports(): Array<{ sessionId: string; report: AdvisorSupervisionPipelineReport }> {
			return sessionPipelines.map(({ sessionId, pipeline }) => ({
				sessionId,
				report: pipeline.report(),
			}));
		},
	};
}

/**
 * Qualifies structured and canary advisor supervision paths against historical
 * sentinel-labelled sessions.
 *
 * Runs {@link replayCpkSupervision} across {@link CPK_SUPERVISION_RULE_CLASSES} comparing:
 * 1. Legacy baseline arm vs. structured candidate arm
 * 2. Legacy baseline arm vs. canary candidate arm (max divergences = 0)
 *
 * Reports replay outcomes and the list of session IDs whose canary pipeline exceeded budget.
 */
export async function qualifyAdvisorSupervision(
	sessions: readonly CpkSupervisionReplaySession<AdvisorReplayEvent>[],
	opts?: QualifyAdvisorSupervisionOptions,
): Promise<AdvisorSupervisionQualificationResult> {
	const legacyForStructured = createAdvisorReplayArm("legacy");
	const structuredArm = createAdvisorReplayArm("structured", {
		structuredGate: opts?.structuredGate,
	});

	const legacyForCanary = createAdvisorReplayArm("legacy");
	const canaryArm = createAdvisorReplayArm("canary", {
		structuredGate: opts?.structuredGate,
		canaryMaxDivergences: 0,
	});

	const [structured, canary] = await Promise.all([
		replayCpkSupervision({
			classes: CPK_SUPERVISION_RULE_CLASSES,
			sessions,
			legacy: legacyForStructured,
			candidate: structuredArm,
		}),
		replayCpkSupervision({
			classes: CPK_SUPERVISION_RULE_CLASSES,
			sessions,
			legacy: legacyForCanary,
			candidate: canaryArm,
		}),
	]);

	const canaryBudgetExceeded = canaryArm
		.reports()
		.filter(entry => entry.report.canaryBudgetExceeded)
		.map(entry => entry.sessionId);

	return {
		structured,
		canary,
		canaryBudgetExceeded,
	};
}
