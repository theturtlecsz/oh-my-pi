import type { CpkSupervisionProposal } from "../extensibility/cpk/supervision-proposal";
import { isCpkSupervisionRuleClass, proposalFromAdvisorNote } from "../extensibility/cpk/supervision-proposal";
import { type AdvisorCategory, type AdvisorSeverity, advisorNoteDedupeKey, advisorSeverityRank } from "./advise-tool";
import { type AdvisorEmissionClassification, AdvisorEmissionGuard } from "./emission-guard";

/**
 * One supervision decision. `proposal` is set only when the note is delivered.
 * `duplicate-rank` is AdviseTool's whitespace-key escalation check; the other
 * suppression reasons are the emission guard's, plus `invalid` when the note
 * cannot be parsed into a CPK-6 proposal.
 */
export type AdvisorSupervisionReason = "delivered" | "duplicate-rank" | "noise" | "invalid" | "duplicate" | "budget";

export interface AdvisorSupervisionDecision {
	deliver: boolean;
	reason: AdvisorSupervisionReason;
	proposal?: CpkSupervisionProposal;
}

export interface AdvisorSupervisionInput {
	note: string;
	severity?: AdvisorSeverity;
	category?: string;
	transcriptIndex: number;
}

/** Per rule-class tally. Missing category (or a non-canonical one) is `unclassified`. */
export interface AdvisorSupervisionClassStats {
	proposed: number;
	delivered: number;
	suppressed: Partial<Record<Exclude<AdvisorSupervisionReason, "delivered">, number>>;
}

export type AdvisorSupervisionClass = AdvisorCategory | "unclassified";

export type AdvisorSupervisionReport = Partial<Record<AdvisorSupervisionClass, AdvisorSupervisionClassStats>>;

/**
 * Composes AdviseTool's rank dedupe, CPK-6 proposal parsing, and the emission
 * guard into one decision.
 *
 * Order: whitespace-key rank (recorded even when a later stage suppresses) →
 * empty-after-trim noise, with no parse → `proposalFromAdvisorNote` error as
 * `invalid` (never thrown) → emission-guard classify → otherwise delivered
 * with the proposal.
 *
 * Tallies accumulate until {@link AdvisorSupervisionGate.reset}. `beginUpdate`
 * only reopens the emission guard's per-update budget, after any deferred
 * flush the tool has already run. {@link AdvisorSupervisionGate.report}
 * returns a fresh object.
 */
export class AdvisorSupervisionGate {
	readonly #guard = new AdvisorEmissionGuard();
	/** Highest rank passed through for each whitespace-collapsed note. */
	#ranks = new Map<string, number>();
	#stats = new Map<AdvisorSupervisionClass, AdvisorSupervisionClassStats>();

	decide(input: AdvisorSupervisionInput): AdvisorSupervisionDecision {
		const decision = this.#decide(input);
		this.#tally(input.category, decision);
		return decision;
	}

	/** Reopen the per-update emission budget. Rank history and tallies stay. */
	beginUpdate(): void {
		this.#guard.beginUpdate();
	}

	/** Drop rank history, emission-guard state, and tallies. */
	reset(): void {
		this.#guard.reset();
		this.#ranks.clear();
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
		const key = advisorNoteDedupeKey(input.note);
		const rank = advisorSeverityRank(input.severity);
		const previous = this.#ranks.get(key) ?? 0;
		if (rank <= previous) return { deliver: false, reason: "duplicate-rank" };
		// Record before later stages so a suppressed note still occupies the rank.
		this.#ranks.set(key, rank);

		if (input.note.trim().length === 0) return { deliver: false, reason: "noise" };

		let proposal: CpkSupervisionProposal;
		try {
			proposal = proposalFromAdvisorNote(input.note, input.severity, input.category, input.transcriptIndex);
		} catch {
			return { deliver: false, reason: "invalid" };
		}

		const verdict: AdvisorEmissionClassification = this.#guard.classify(input.note);
		if (verdict !== "accepted") return { deliver: false, reason: verdict };
		return { deliver: true, reason: "delivered", proposal };
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
