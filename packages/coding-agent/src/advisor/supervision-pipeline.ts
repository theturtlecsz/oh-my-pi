import type { AgentTool, AgentToolContext, AgentToolResult, AgentToolUpdateCallback } from "@oh-my-pi/pi-agent-core";
import { logger } from "@oh-my-pi/pi-utils";
import {
	type AdviseDetails,
	type AdviseParams,
	AdviseTool,
	type AdvisorCategory,
	type AdvisorSeverity,
} from "./advise-tool";
import { AdvisorEmissionGuard } from "./emission-guard";
import { AdvisorSupervisionGate, type AdvisorSupervisionReport } from "./supervision-gate";

export const ADVISOR_SUPERVISION_PATHS = ["legacy", "shadow", "canary", "structured"] as const;
export type AdvisorSupervisionPath = (typeof ADVISOR_SUPERVISION_PATHS)[number];

/** Which arm's buffer and tool result are delivered. */
export type AdvisorSupervisionAuthority = "legacy" | "structured";

export interface AdvisorSupervisionPipelineOptions {
	path: AdvisorSupervisionPath;
	canaryMaxDivergences: number;
	/** Sampled by each arm at `execute`, and kept on a deferred note until flush. */
	transcriptIndex: () => number;
	deliver: (note: string, severity?: AdvisorSeverity, category?: AdvisorCategory, transcriptIndex?: number) => void;
	/** Structured arm's gate. When omitted, that arm builds a fresh {@link AdvisorSupervisionGate}. */
	structuredGate?: AdvisorSupervisionGate;
}

export interface AdvisorSupervisionPipelineReport {
	path: AdvisorSupervisionPath;
	authority: AdvisorSupervisionAuthority;
	divergences: number;
	canaryBudgetExceeded: boolean;
	invocations: { legacy: number; structured: number };
	/** Structured gate tallies, or `{}` when that arm was not built. */
	classes: AdvisorSupervisionReport;
}

interface BufferedNote {
	note: string;
	severity?: AdvisorSeverity;
	category?: AdvisorCategory;
	transcriptIndex?: number;
}

interface Arm {
	tool: AdviseTool;
	buffer: BufferedNote[];
}

function sameNotes(left: readonly BufferedNote[], right: readonly BufferedNote[]): boolean {
	if (left.length !== right.length) return false;
	for (let i = 0; i < left.length; i++) {
		const a = left[i];
		const b = right[i];
		if (!a || !b) return false;
		if (
			a.note !== b.note ||
			a.severity !== b.severity ||
			a.category !== b.category ||
			a.transcriptIndex !== b.transcriptIndex
		) {
			return false;
		}
	}
	return true;
}

/**
 * Compares the legacy rank-map plus {@link AdvisorEmissionGuard} with the
 * structured {@link AdvisorSupervisionGate}, and delivers one of them.
 *
 * `legacy` and `shadow` deliver the legacy arm. `structured` and `canary`
 * deliver the structured arm until a canary divergence count exceeds
 * `canaryMaxDivergences`. That call, and every later call, delivers the legacy
 * buffer and result for the pipeline's life, including across {@link reset}.
 *
 * An arm's `onAdvice` only buffers. {@link AdvisorSupervisionPipeline.beginUpdate}
 * flushes synchronously — legacy tool, then its guard — and compares those
 * buffers the same way. One call or flush whose buffers differ is one divergence.
 * `invocations` counts `execute` calls, not flushes.
 */
export class AdvisorSupervisionPipeline {
	readonly tool: AgentTool<any, AdviseDetails>;
	readonly #path: AdvisorSupervisionPath;
	readonly #canaryMaxDivergences: number;
	readonly #deliver: AdvisorSupervisionPipelineOptions["deliver"];
	readonly #legacyGuard?: AdvisorEmissionGuard;
	readonly #structuredGate?: AdvisorSupervisionGate;
	readonly #legacy?: Arm;
	readonly #structured?: Arm;
	#divergences = 0;
	#canaryBudgetExceeded = false;
	#legacyInvocations = 0;
	#structuredInvocations = 0;

	constructor(opts: AdvisorSupervisionPipelineOptions) {
		this.#path = opts.path;
		this.#canaryMaxDivergences = opts.canaryMaxDivergences;
		this.#deliver = opts.deliver;

		const wantLegacy = opts.path === "legacy" || opts.path === "shadow" || opts.path === "canary";
		const wantStructured = opts.path === "structured" || opts.path === "shadow" || opts.path === "canary";

		if (wantLegacy) {
			const guard = new AdvisorEmissionGuard();
			const buffer: BufferedNote[] = [];
			const tool = new AdviseTool(
				(note, severity, category, transcriptIndex) => {
					if (!guard.accept(note)) return;
					buffer.push({ note, severity, category, transcriptIndex });
				},
				{ transcriptIndex: opts.transcriptIndex },
			);
			this.#legacyGuard = guard;
			this.#legacy = { tool, buffer };
		}

		if (wantStructured) {
			const gate = opts.structuredGate ?? new AdvisorSupervisionGate();
			const buffer: BufferedNote[] = [];
			const tool = new AdviseTool(
				(note, severity, category, transcriptIndex) => {
					buffer.push({ note, severity, category, transcriptIndex });
				},
				{ gate, transcriptIndex: opts.transcriptIndex },
			);
			this.#structuredGate = gate;
			this.#structured = { tool, buffer };
		}

		const prototype = (this.#legacy ?? this.#structured)?.tool;
		if (!prototype) throw new Error(`advisor supervision path ${opts.path} built no arm`);
		this.tool = {
			name: prototype.name,
			label: prototype.label,
			description: prototype.description,
			parameters: prototype.parameters,
			intent: prototype.intent,
			execute: (toolCallId, args, signal, onUpdate, context) =>
				this.#execute(toolCallId, args, signal, onUpdate, context),
		};
	}

	/**
	 * Fan the in-progress flag out to every arm. A completed update flushes
	 * deferred notes before the legacy guard's budget reopens.
	 */
	beginUpdate(inProgress: boolean): void {
		if (this.#legacy) {
			this.#legacy.tool.beginUpdate(inProgress);
			this.#legacyGuard?.beginUpdate();
		}
		this.#structured?.tool.beginUpdate(inProgress);
		this.#compareAndDeliver();
	}

	/** Clear every arm. Divergence count, invocation counts, and the canary latch stay. */
	reset(): void {
		this.#legacy?.tool.resetDeliveredNotes();
		this.#legacyGuard?.reset();
		this.#structured?.tool.resetDeliveredNotes();
	}

	report(): AdvisorSupervisionPipelineReport {
		return {
			path: this.#path,
			authority: this.#authority(),
			divergences: this.#divergences,
			canaryBudgetExceeded: this.#canaryBudgetExceeded,
			invocations: { legacy: this.#legacyInvocations, structured: this.#structuredInvocations },
			classes: this.#structuredGate?.report() ?? {},
		};
	}

	async #execute(
		toolCallId: string,
		args: AdviseParams,
		signal?: AbortSignal,
		onUpdate?: AgentToolUpdateCallback<AdviseDetails>,
		context?: AgentToolContext,
	): Promise<AgentToolResult<AdviseDetails>> {
		let legacyResult: AgentToolResult<AdviseDetails> | undefined;
		let structuredResult: AgentToolResult<AdviseDetails> | undefined;
		if (this.#legacy) {
			this.#legacyInvocations += 1;
			legacyResult = await this.#legacy.tool.execute(toolCallId, args, signal, onUpdate, context);
		}
		if (this.#structured) {
			this.#structuredInvocations += 1;
			structuredResult = await this.#structured.tool.execute(toolCallId, args, signal, onUpdate, context);
		}
		this.#compareAndDeliver();
		const result = this.#authority() === "legacy" ? legacyResult : structuredResult;
		if (!result) throw new Error("advisor supervision pipeline has no authoritative arm");
		return result;
	}

	#authority(): AdvisorSupervisionAuthority {
		if (this.#canaryBudgetExceeded) return "legacy";
		if (this.#path === "legacy" || this.#path === "shadow") return "legacy";
		return "structured";
	}

	#take(arm: Arm | undefined): BufferedNote[] {
		if (!arm || arm.buffer.length === 0) return [];
		return arm.buffer.splice(0);
	}

	#compareAndDeliver(): void {
		const legacyNotes = this.#take(this.#legacy);
		const structuredNotes = this.#take(this.#structured);
		if (this.#legacy && this.#structured && !sameNotes(legacyNotes, structuredNotes)) {
			this.#divergences += 1;
			if (this.#path === "canary" && !this.#canaryBudgetExceeded && this.#divergences > this.#canaryMaxDivergences) {
				this.#canaryBudgetExceeded = true;
				logger.warn("canary_budget_exceeded");
			}
		}
		const notes = this.#authority() === "legacy" ? legacyNotes : structuredNotes;
		for (const item of notes) this.#deliver(item.note, item.severity, item.category, item.transcriptIndex);
	}
}
