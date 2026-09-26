import {
	assertCpkSentinelLabels,
	type CpkSentinelLabel,
	CpkSupervisionError,
	type CpkSupervisionProposal,
	type CpkSupervisionRuleClass,
	isCpkSupervisionRuleClass,
	parseCpkSupervisionProposal,
} from "./supervision-proposal";

export interface CpkSupervisionReplaySession<E = unknown> {
	id: string;
	events: readonly E[];
	expected: readonly CpkSentinelLabel[];
}

export interface CpkSupervisionArmInstance<E = unknown> {
	onEvent(event: E, index: number): unknown[] | Promise<unknown[]>;
}

export interface CpkSupervisionArm<E = unknown> {
	name: string;
	start(sessionId: string): CpkSupervisionArmInstance<E> | Promise<CpkSupervisionArmInstance<E>>;
}

export interface ReplayCpkSupervisionOptions<E = unknown> {
	classes: readonly CpkSupervisionRuleClass[];
	sessions: readonly CpkSupervisionReplaySession<E>[];
	legacy: CpkSupervisionArm<E>;
	candidate: CpkSupervisionArm<E>;
}

export type CpkSupervisionReplayStatus = "insufficient_evidence" | "regressed" | "noisier" | "qualified";

export interface CpkSupervisionArmInvocations {
	legacy: number;
	candidate: number;
}

export interface CpkSupervisionArmTally {
	caught: number;
	missed: number;
	falsePositives: number;
	invalid: number;
}

export interface CpkSupervisionClassReport {
	expected: number;
	legacy: CpkSupervisionArmTally;
	candidate: CpkSupervisionArmTally;
}

export interface CpkSupervisionReportElement {
	sessionId: string;
	ruleClass: CpkSupervisionRuleClass;
	transcriptIndex: number;
}

export interface CpkSupervisionReplayReport {
	status: CpkSupervisionReplayStatus;
	zeroRegression: boolean;
	invocations: CpkSupervisionArmInvocations;
	classes: Record<CpkSupervisionRuleClass, CpkSupervisionClassReport>;
	regressions: CpkSupervisionReportElement[];
	corrections: CpkSupervisionReportElement[];
	uncaught: CpkSupervisionReportElement[];
}

function processInvalidOutput(
	item: unknown,
	uniqueClasses: readonly CpkSupervisionRuleClass[],
	classReports: Record<string, CpkSupervisionClassReport>,
	armRole: "legacy" | "candidate",
): void {
	const namedClass =
		typeof item === "object" && item !== null && !Array.isArray(item)
			? (item as Record<string, unknown>).ruleClass
			: undefined;

	if (typeof namedClass === "string" && uniqueClasses.includes(namedClass as CpkSupervisionRuleClass)) {
		classReports[namedClass][armRole].invalid++;
	} else if (typeof namedClass === "string" && isCpkSupervisionRuleClass(namedClass)) {
		// Canonical untracked class: ignored
	} else {
		// Non-canonical class or no class named: counts once for every class in classes
		for (const cls of uniqueClasses) {
			classReports[cls][armRole].invalid++;
		}
	}
}

/**
 * Replays sentinel-labelled sessions through a candidate supervision arm and a
 * legacy baseline, reporting per-class tallies, regressions, and a verdict.
 */
export async function replayCpkSupervision<E = unknown>(
	options: ReplayCpkSupervisionOptions<E>,
): Promise<CpkSupervisionReplayReport> {
	if (typeof options !== "object" || options === null || Array.isArray(options)) {
		throw new CpkSupervisionError("not_object", "replay options must be a non-null object");
	}
	if (!Array.isArray(options.classes)) {
		throw new CpkSupervisionError("not_object", "classes must be an array");
	}
	if (!Array.isArray(options.sessions)) {
		throw new CpkSupervisionError("not_object", "sessions must be an array");
	}
	if (
		typeof options.legacy !== "object" ||
		options.legacy === null ||
		typeof options.candidate !== "object" ||
		options.candidate === null
	) {
		throw new CpkSupervisionError("not_object", "legacy and candidate arms must be non-null objects");
	}

	const uniqueClasses: CpkSupervisionRuleClass[] = [];
	for (const cls of options.classes) {
		if (!isCpkSupervisionRuleClass(cls)) {
			throw new CpkSupervisionError("invalid_class", `ruleClass must be canonical, got ${String(cls)}`);
		}
		if (!uniqueClasses.includes(cls)) {
			uniqueClasses.push(cls);
		}
	}

	// Validate all sessions' labels before any arm is started
	const validatedSessions: Array<{ id: string; events: readonly E[]; expected: CpkSentinelLabel[] }> = [];
	for (const session of options.sessions) {
		if (typeof session !== "object" || session === null || Array.isArray(session)) {
			throw new CpkSupervisionError("not_object", "session must be a non-null object");
		}
		const events = session.events ?? [];
		const labels = assertCpkSentinelLabels(session.expected, events.length);
		for (const label of labels) {
			if (!uniqueClasses.includes(label.ruleClass)) {
				throw new CpkSupervisionError(
					"invalid_sentinel",
					`sentinel label ruleClass "${label.ruleClass}" is not in target classes [${uniqueClasses.join(", ")}]`,
				);
			}
		}
		validatedSessions.push({ id: session.id, events, expected: labels });
	}

	const classReports: Record<string, CpkSupervisionClassReport> = {};
	for (const cls of uniqueClasses) {
		classReports[cls] = {
			expected: 0,
			legacy: { caught: 0, missed: 0, falsePositives: 0, invalid: 0 },
			candidate: { caught: 0, missed: 0, falsePositives: 0, invalid: 0 },
		};
	}
	for (const session of validatedSessions) {
		for (const label of session.expected) {
			classReports[label.ruleClass].expected++;
		}
	}

	const invocations: CpkSupervisionArmInvocations = {
		legacy: 0,
		candidate: 0,
	};

	const regressions: CpkSupervisionReportElement[] = [];
	const corrections: CpkSupervisionReportElement[] = [];
	const uncaught: CpkSupervisionReportElement[] = [];

	for (const session of validatedSessions) {
		const legacyInstance = await options.legacy.start(session.id);
		const candidateInstance = await options.candidate.start(session.id);

		// Map (ruleClass:transcriptIndex) -> index within session.expected
		const labelIndexMap = new Map<string, number>();
		for (let i = 0; i < session.expected.length; i++) {
			const label = session.expected[i];
			labelIndexMap.set(`${label.ruleClass}:${label.transcriptIndex}`, i);
		}

		const legacyCaughtIndices = new Set<number>();
		const candidateCaughtIndices = new Set<number>();

		for (let index = 0; index < session.events.length; index++) {
			const event = session.events[index];

			const legacyRaw = await legacyInstance.onEvent(event, index);
			invocations.legacy++;

			const candidateRaw = await candidateInstance.onEvent(event, index);
			invocations.candidate++;

			const legacyItems = Array.isArray(legacyRaw) ? legacyRaw : legacyRaw == null ? [] : [legacyRaw];
			for (const item of legacyItems) {
				let proposal: CpkSupervisionProposal;
				try {
					proposal = parseCpkSupervisionProposal(item);
				} catch {
					processInvalidOutput(item, uniqueClasses, classReports, "legacy");
					continue;
				}
				if (!uniqueClasses.includes(proposal.ruleClass)) {
					continue;
				}
				const targetIdx = labelIndexMap.get(`${proposal.ruleClass}:${proposal.transcriptIndex}`);
				if (targetIdx !== undefined && !legacyCaughtIndices.has(targetIdx)) {
					legacyCaughtIndices.add(targetIdx);
					classReports[proposal.ruleClass].legacy.caught++;
				} else {
					classReports[proposal.ruleClass].legacy.falsePositives++;
				}
			}

			const candidateItems = Array.isArray(candidateRaw) ? candidateRaw : candidateRaw == null ? [] : [candidateRaw];
			for (const item of candidateItems) {
				let proposal: CpkSupervisionProposal;
				try {
					proposal = parseCpkSupervisionProposal(item);
				} catch {
					processInvalidOutput(item, uniqueClasses, classReports, "candidate");
					continue;
				}
				if (!uniqueClasses.includes(proposal.ruleClass)) {
					continue;
				}
				const targetIdx = labelIndexMap.get(`${proposal.ruleClass}:${proposal.transcriptIndex}`);
				if (targetIdx !== undefined && !candidateCaughtIndices.has(targetIdx)) {
					candidateCaughtIndices.add(targetIdx);
					classReports[proposal.ruleClass].candidate.caught++;
				} else {
					classReports[proposal.ruleClass].candidate.falsePositives++;
				}
			}
		}

		// Discrepancies in session order, then label order as given
		for (let i = 0; i < session.expected.length; i++) {
			const label = session.expected[i];
			const legacyCaught = legacyCaughtIndices.has(i);
			const candidateCaught = candidateCaughtIndices.has(i);
			const element: CpkSupervisionReportElement = {
				sessionId: session.id,
				ruleClass: label.ruleClass,
				transcriptIndex: label.transcriptIndex,
			};

			if (legacyCaught && !candidateCaught) {
				regressions.push(element);
			} else if (!legacyCaught && candidateCaught) {
				corrections.push(element);
			} else if (!legacyCaught && !candidateCaught) {
				uncaught.push(element);
			}
		}
	}

	for (const cls of uniqueClasses) {
		const report = classReports[cls];
		report.legacy.missed = report.expected - report.legacy.caught;
		report.candidate.missed = report.expected - report.candidate.caught;
	}

	const allCovered = uniqueClasses.length > 0 && uniqueClasses.every(cls => classReports[cls].expected >= 1);
	const zeroRegression = regressions.length === 0 && uncaught.length === 0 && allCovered;

	let status: CpkSupervisionReplayStatus;
	if (validatedSessions.length === 0 || !allCovered) {
		status = "insufficient_evidence";
	} else if (regressions.length > 0 || uncaught.length > 0) {
		status = "regressed";
	} else if (
		uniqueClasses.some(cls => classReports[cls].candidate.falsePositives > classReports[cls].legacy.falsePositives)
	) {
		status = "noisier";
	} else {
		status = "qualified";
	}

	return {
		status,
		zeroRegression,
		invocations,
		classes: classReports as Record<CpkSupervisionRuleClass, CpkSupervisionClassReport>,
		regressions,
		corrections,
		uncaught,
	};
}
