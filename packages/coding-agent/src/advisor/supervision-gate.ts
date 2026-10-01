import type { CpkSupervisionProposal } from "../extensibility/cpk/supervision-proposal";
import { isCpkSupervisionRuleClass, proposalFromAdvisorNote } from "../extensibility/cpk/supervision-proposal";
import {
	type AdvisorAdmissionAuthority,
	type AdvisorCategory,
	type AdvisorSeverity,
	advisorSeverityRank,
} from "./advise-tool";
import {
	type AdvisorAdmission,
	AdvisorEmissionGuard,
	type AdvisorSuppressionReason,
	screenAdvisorNote,
} from "./emission-guard";

/**
 * One supervision decision. `proposal` is set only when the note is delivered.
 * `empty`, `noise`, `duplicate` (rank-aware) and `budget` are the emission
 * guard's verdicts; `invalid` is the gate's own, when the note cannot be parsed
 * into a CPK-6 proposal.
 */
export type AdvisorSupervisionReason = "delivered" | "empty" | "noise" | "invalid" | "duplicate" | "budget";

export interface AdvisorSupervisionDecision {
	deliver: boolean;
	reason: AdvisorSupervisionReason;
	proposal?: CpkSupervisionProposal;
	/** Still-pending note of the same update displaced by this admission (see {@link AdvisorAdmission}). */
	displacedKey?: string;
}

export interface AdvisorSupervisionInput {
	note: string;
	severity?: AdvisorSeverity;
	category?: string;
	transcriptIndex: number;
	/** Withheld behind an in-progress primary turn (displaceable) rather than routed now. */
	pending?: boolean;
}

/** Per rule-class tally. Missing category (or a non-canonical one) is `unclassified`. */
export interface AdvisorSupervisionClassStats {
	proposed: number;
	delivered: number;
	suppressed: Partial<Record<Exclude<AdvisorSupervisionReason, "delivered">, number>>;
}

export type AdvisorSupervisionClass = AdvisorCategory | "unclassified";

export type AdvisorSupervisionReport = Partial<Record<AdvisorSupervisionClass, AdvisorSupervisionClassStats>>;

const SUPPRESSION_ACK_REASON: Record<Exclude<AdvisorSupervisionReason, "delivered">, AdvisorSuppressionReason> = {
	empty: "empty",
	noise: "noise",
	invalid: "invalid",
	duplicate: "duplicate",
	budget: "rate-limit",
};

/**
 * Composes CPK-6 proposal parsing with the stock emission guard into one
 * decision, and serves as an {@link AdviseTool}'s admission authority.
 *
 * The emission guard stays the single dedupe/budget authority, so the gate
 * admits and acknowledges exactly what the legacy arm does for every
 * well-formed note (the canary compares the two). Order: the guard's stateless
 * empty/noise screen, with no parse → `proposalFromAdvisorNote` error as
 * `invalid` (never thrown, no guard state touched) → emission-guard admission
 * (rank-aware duplicate, per-update budget) → otherwise delivered with the
 * proposal.
 *
 * Tallies accumulate until {@link AdvisorSupervisionGate.reset}. `beginUpdate`
 * only reopens the emission guard's per-update budget. {@link AdvisorSupervisionGate.report}
 * returns a fresh object.
 */
export class AdvisorSupervisionGate implements AdvisorAdmissionAuthority {
	readonly #guard: AdvisorEmissionGuard;
	#stats = new Map<AdvisorSupervisionClass, AdvisorSupervisionClassStats>();

	constructor(opts: { budgetPerUpdate?: number } = {}) {
		this.#guard = new AdvisorEmissionGuard({ budgetPerUpdate: opts.budgetPerUpdate });
	}

	decide(input: AdvisorSupervisionInput): AdvisorSupervisionDecision {
		const decision = this.#decide(input);
		this.#tally(input.category, decision);
		return decision;
	}

	admit(
		note: string,
		opts: {
			rank: number;
			pending: boolean;
			severity?: AdvisorSeverity;
			category?: AdvisorCategory;
			transcriptIndex?: number;
		},
	): AdvisorAdmission {
		const decision = this.decide({
			note,
			severity: opts.severity,
			category: opts.category,
			transcriptIndex: opts.transcriptIndex ?? 0,
			pending: opts.pending,
		});
		if (decision.deliver) {
			return decision.displacedKey === undefined
				? { accepted: true }
				: { accepted: true, displacedKey: decision.displacedKey };
		}
		return {
			accepted: false,
			reason: SUPPRESSION_ACK_REASON[decision.reason as Exclude<AdvisorSupervisionReason, "delivered">],
		};
	}

	escalatePending(note: string, rank: number): void {
		this.#guard.escalatePending(note, rank);
	}

	markRouted(note: string): void {
		this.#guard.markRouted(note);
	}

	/** Reopen the per-update emission budget. Dedupe history and tallies stay. */
	beginUpdate(): void {
		this.#guard.beginUpdate();
	}

	/** Drop emission-guard state and tallies. */
	reset(): void {
		this.#guard.reset();
		this.#stats.clear();
	}

	/** Snapshot of tallies since the last {@link reset}. */
	report(): AdvisorSupervisionReport {
		const out: AdvisorSupervisionReport = {};
		for (const [key, stats] of this.#stats) {
			out[key] = {
				proposed: stats.proposed,
				delivered: stats.delivered,
				suppressed: { ...stats.suppressed },
			};
		}
		return out;
	}

	#decide(input: AdvisorSupervisionInput): AdvisorSupervisionDecision {
		const screened = screenAdvisorNote(input.note);
		if (screened) return { deliver: false, reason: screened };

		let proposal: CpkSupervisionProposal;
		try {
			proposal = proposalFromAdvisorNote(input.note, input.severity, input.category, input.transcriptIndex);
		} catch {
			return { deliver: false, reason: "invalid" };
		}

		const admission = this.#guard.admit(input.note, {
			rank: advisorSeverityRank(input.severity),
			pending: input.pending ?? false,
		});
		if (!admission.accepted) {
			return {
				deliver: false,
				reason: admission.reason === "rate-limit" ? "budget" : (admission.reason ?? "duplicate"),
			};
		}
		return admission.displacedKey === undefined
			? { deliver: true, reason: "delivered", proposal }
			: { deliver: true, reason: "delivered", proposal, displacedKey: admission.displacedKey };
	}

	#tally(category: string | undefined, decision: AdvisorSupervisionDecision): void {
		const key: AdvisorSupervisionClass = isCpkSupervisionRuleClass(category) ? category : "unclassified";
		let stats = this.#stats.get(key);
		if (!stats) {
			stats = { proposed: 0, delivered: 0, suppressed: {} };
			this.#stats.set(key, stats);
		}
		stats.proposed += 1;
		if (decision.deliver) {
			stats.delivered += 1;
			return;
		}
		if (decision.reason === "delivered") return;
		stats.suppressed[decision.reason] = (stats.suppressed[decision.reason] ?? 0) + 1;
	}
}
