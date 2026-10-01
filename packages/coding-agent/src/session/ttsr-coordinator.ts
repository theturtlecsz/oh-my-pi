import * as os from "node:os";
import * as path from "node:path";
import {
	type AfterToolCallContext,
	type AfterToolCallResult,
	type Agent,
	type AgentEvent,
	type AgentMessage,
	type AgentTool,
	type AgentToolContext,
	type AgentToolResult,
	type BeforeToolCallContext,
	type BeforeToolCallResult,
	createToolScopedAbortReason,
} from "@oh-my-pi/pi-agent-core";
import type { AssistantMessage, Judge, ToolCall } from "@oh-my-pi/pi-ai";
import { logger, prompt, relativePathWithinRoot, untilAborted, withTimeout } from "@oh-my-pi/pi-utils";
import type { Rule } from "../capability/rule";
import type { Settings } from "../config/settings";
import { judgeRules, type TtsrManager, type TtsrMatchContext, type TtsrOutput } from "../export/ttsr";
import ttsrInterruptTemplate from "../prompts/system/ttsr-interrupt.md" with { type: "text" };
import ttsrToolReminderTemplate from "../prompts/system/ttsr-tool-reminder.md" with { type: "text" };
import ttsrWarningTemplate from "../prompts/system/ttsr-warning.md" with { type: "text" };
import type { AgentSessionEvent } from "./agent-session-events";
import type { SessionManager } from "./session-manager";
import { TtsrToolInspector } from "./ttsr-outputs";

type TtsrContinueSkipReason =
	| "aborted"
	| "stale-generation"
	| "session-unavailable"
	| "should-continue-false"
	| "post-restore-unavailable";

/** How long a finishing run waits for in-flight judgments; later verdicts still arrive as asides. */
const JUDGED_SETTLE_TIMEOUT_MS = 5_000;

interface TtsrContinueOptions {
	source: string;
	delayMs?: number;
	generation?: number;
	shouldContinue?: () => boolean;
	onSkip?: (reason: TtsrContinueSkipReason) => void;
	onError?: () => void;
}

interface InterruptedAttempt {
	generation: number;
	timestamp: number;
	coreIdle: Promise<void>;
	cancellation: AbortController;
	resume: Promise<void>;
	resolve: () => void;
}

interface EventOwnership {
	generation: number;
	signal: AbortSignal;
}

interface AssistantProcessing {
	message: AssistantMessage;
	outcome: Promise<boolean>;
}

/** Capabilities the TTSR coordinator borrows from its owning session. */
export interface TtsrCoordinatorHost {
	agent: Agent;
	sessionManager: SessionManager;
	settings: Settings;
	emitSessionEvent(event: AgentSessionEvent): Promise<void>;
	schedulePostPromptTask(
		task: (signal: AbortSignal) => Promise<void>,
		options?: { delayMs?: number; generation?: number; onSkip?: () => void },
	): void;
	emitNotice(level: "warning", message: string, source: string): void;
	scheduleAgentContinue(options: TtsrContinueOptions): void;
	promptGeneration(): number;
	/** Judge for `question` rules, or `undefined` while judged rules are off (`ttsr.judge`). */
	ruleJudge(): Judge | undefined;
	/** Delivers a judged-rule warning without interrupting the run. */
	deliverRuleWarning(content: string, ruleNames: string[]): Promise<void>;
	/** Changes when the session is replaced; verdicts from an older generation are dropped. */
	sessionGeneration(): number;
}

/** Coordinates TTSR stream matching, interruption, injection, and resume gates. */
export class TtsrCoordinator {
	readonly #host: TtsrCoordinatorHost;
	readonly #manager: TtsrManager | undefined;
	readonly #inspector: TtsrToolInspector;
	#pendingInjections: Rule[] = [];
	#perToolInjections = new Map<string, Rule[]>();
	#deferredReservations = new Map<string, number>();
	#nextDeferredDeliveryId = 0;
	#abortPending = false;
	#attempt: InterruptedAttempt | undefined;
	#assistantProcessing = new Map<number, AssistantProcessing>();
	#recorded = new WeakSet<AssistantMessage>();
	#eventOwnership = new WeakMap<AgentEvent, EventOwnership>();
	#matchingCancellation = new AbortController();
	#resumePromise: Promise<void> | undefined;
	#resumeResolve: (() => void) | undefined;
	/** Rule names already announced per stream key: a delta match re-confirmed
	 *  at finalization must not emit a second `ttsr_triggered` (#12184). */
	#emittedTriggerRules = new Map<string, Set<string>>();
	/** In-flight judged-rule checks, each already guarded against rejection. */
	#pendingJudgments = new Set<Promise<void>>();

	constructor(host: TtsrCoordinatorHost, manager: TtsrManager | undefined) {
		this.#host = host;
		this.#manager = manager;
		this.#inspector = new TtsrToolInspector(
			() => host.agent.state.tools,
			() => host.sessionManager.getCwd(),
		);
	}

	/** Configured TTSR manager, when stream rules are enabled. */
	get manager(): TtsrManager | undefined {
		return this.#manager;
	}

	/** Whether a TTSR-triggered stream abort is awaiting its continuation. */
	get abortPending(): boolean {
		return this.#abortPending;
	}

	/** Current resume gate awaited by post-prompt recovery. */
	get resumeGate(): Promise<void> | undefined {
		return this.#attempt?.resume ?? this.#resumePromise;
	}

	/** Resets stream buffers at turn start. */
	onTurnStart(): void {
		this.#manager?.resetBuffer();
	}

	/** Capture cancellation/generation before event dispatch can queue behind authority or hooks. */
	onEventEntry(event: AgentEvent): void {
		if (event.type === "message_update")
			this.#eventOwnership.set(event, {
				generation: this.#host.promptGeneration(),
				signal: this.#matchingCancellation.signal,
			});
	}

	/** Observe the existing handler, including any read-authority queue ahead of dispatch. */
	observeProcessing(event: AgentEvent, processing: Promise<void>): void {
		if (
			event.type !== "message_end" ||
			event.message.role !== "assistant" ||
			event.message.stopReason !== "aborted" ||
			!this.#manager?.hasRules()
		)
			return;
		this.#assistantProcessing.set(event.message.timestamp, {
			message: event.message,
			outcome: processing.then(
				() => true,
				() => false,
			),
		});
	}

	/** Called only by the permitted assistant append path; handler success is separate. */
	onAssistantRecorded(message: AssistantMessage): void {
		this.#recorded.add(message);
	}

	/** Scope structural suppression to the interrupted assistant and owning generation. */
	ownsInterruptedMessage(message: AssistantMessage): boolean {
		return (
			this.#abortPending &&
			this.#attempt?.timestamp === message.timestamp &&
			this.#attempt.generation === this.#host.promptGeneration()
		);
	}

	/**
	 * Resets stream buffers when an assistant message begins. The agent loop
	 * turns the first provider `start` of every response into `message_start`,
	 * so this is the boundary between two responses inside one turn (an aborted
	 * response and its retry, or a continuation after an interruption); without
	 * it, text from the earlier response would combine with the later one.
	 */
	onAssistantMessageStart(): void {
		this.#manager?.resetBuffer();
	}

	/** Advances repeat-after-gap tracking at turn end. */
	onTurnEnd(): void {
		this.#manager?.incrementMessageCount();
		this.#emittedTriggerRules.clear();
	}
	/** Checks one streamed message update and reports whether TTSR consumed it by aborting. */
	async checkMessageUpdate(event: AgentEvent): Promise<boolean> {
		if (event.type !== "message_update" || !this.#manager?.hasRules()) return false;
		const ownership = this.#eventOwnership.get(event) ?? {
			generation: this.#host.promptGeneration(),
			signal: this.#matchingCancellation.signal,
		};
		const ownsEvent = () => !ownership.signal.aborted && ownership.generation === this.#host.promptGeneration();
		if (!ownsEvent()) return false;
		const assistantEvent = event.assistantMessageEvent;
		// A later `start` inside one response restarts its partial; the buffers
		// describe the discarded attempt and must not survive it.
		if (assistantEvent.type === "start") {
			this.#manager.resetBuffer();
			return false;
		}
		let matchContext: TtsrMatchContext | undefined;
		let streamingToolCall: ToolCall | undefined;
		let delta: string | undefined;
		if (assistantEvent.type === "text_delta") {
			matchContext = { source: "text" };
			delta = assistantEvent.delta;
		} else if (assistantEvent.type === "thinking_delta") {
			matchContext = { source: "thinking" };
			delta = assistantEvent.delta;
		} else if (assistantEvent.type === "toolcall_delta") {
			streamingToolCall = this.#getStreamingToolCallBlock(event.message, assistantEvent.contentIndex);
			matchContext = this.#inspector.matchContext(streamingToolCall, assistantEvent.contentIndex);
			delta = assistantEvent.delta;
		} else if (assistantEvent.type === "toolcall_end") {
			streamingToolCall = assistantEvent.toolCall;
			matchContext = this.#inspector.matchContext(streamingToolCall, assistantEvent.contentIndex);
			delta = "";
		}
		if (!matchContext || delta === undefined) return false;
		const targetMessageTimestamp = event.message.role === "assistant" ? event.message.timestamp : undefined;
		const matches = this.#checkStream(delta, matchContext, streamingToolCall, assistantEvent.type === "toolcall_end");
		if (!ownsEvent()) return false;
		if (matches.length > 0 && this.#handleMatches(matches, matchContext, targetMessageTimestamp)) return true;
		return false;
	}

	/** AST parsing runs once on finalized arguments, before execution, not in
	 * fire-and-forget stream listeners or on partial deltas. */
	async beforeToolCall(ctx: BeforeToolCallContext): Promise<BeforeToolCallResult | undefined> {
		if (!this.#manager?.hasAstRules()) return undefined;
		const toolCall = { ...ctx.toolCall, arguments: ctx.args };
		const matchContext = this.#inspector.matchContext(toolCall, 0);
		const generation = this.#host.promptGeneration();
		const matches = await this.#checkAstStream(matchContext, toolCall);
		if (matches.length === 0) return undefined;
		// A verdict that lands after its prompt was aborted or superseded still keeps the
		// matched call from running, but must not schedule recovery against the next prompt (OMP-272).
		if (this.#host.promptGeneration() !== generation)
			return { block: true, reason: this.#formatAbortReason(matches) };
		// The assistant message already ended and was recorded with its tool use, so it is the
		// settled continuation target an interrupt's attempt verifies (OMP-272).
		this.#assistantProcessing.set(ctx.assistantMessage.timestamp, {
			message: ctx.assistantMessage,
			outcome: Promise.resolve(true),
		});
		if (this.#handleMatches(matches, matchContext, ctx.assistantMessage.timestamp)) {
			// Generation already ended: stop the tool turn before TTSR recovery retries it.
			const reason = this.#formatAbortReason(matches);
			ctx.assistantMessage.stopReason = "aborted";
			ctx.assistantMessage.errorMessage = reason;
			return { block: true, reason };
		}
		return undefined;
	}

	/** Checks finalized arguments for a tool call issued through eval or another non-loop bridge. */
	async beforeBridgedToolCall(
		toolCallId: string,
		tool: AgentTool,
		args: unknown,
	): Promise<{ block?: boolean; reason?: string } | undefined> {
		if (!this.#manager?.hasRules()) return undefined;
		const toolCall = { type: "toolCall", id: toolCallId, name: tool.name, arguments: args } as ToolCall;
		const matchContext = this.#inspector.matchContext(toolCall, 0);
		let matches: Rule[];
		try {
			matches = [
				...this.#checkStream("", matchContext, toolCall, true),
				...(await this.#checkAstStream(matchContext, toolCall)),
			].filter((rule, index, all) => all.findIndex(candidate => candidate.name === rule.name) === index);
		} finally {
			if (matchContext.streamKey) this.#manager.clearStream(matchContext.streamKey);
		}
		if (matches.length === 0) return undefined;

		this.#emitTriggerOnce(matchContext, matches);
		if (!this.#shouldInterrupt(matches, matchContext)) {
			this.#addPerToolInjections(toolCallId, matches, { markInjected: false });
			return undefined;
		}

		const reminder = matches
			.map(rule =>
				prompt.render(ttsrInterruptTemplate, {
					name: rule.name,
					path: this.#displayRulePath(rule.path),
					content: rule.content,
				}),
			)
			.join("\n\n");
		this.#markInjected(matches.map(rule => rule.name));
		return { block: true, reason: this.#formatAbortReason(matches) + "\n" + reminder };
	}

	/** Settles the previous resume gate, queues any deferred injection, and starts judged-rule checks. */
	onAssistantMessageEnd(message: AssistantMessage): void {
		// Gate on abortPending, not stopReason: unrelated aborts have no TTSR continuation.
		if (!this.#attempt && !this.#abortPending) this.#resolveDeferred(this.#resumeResolve);
		this.#queueDeferredInjectionIfNeeded(message);
		this.#judgeCompletedMessage(message);
	}

	/**
	 * Waits (bounded) for in-flight judged-rule checks. The session runs this
	 * before the agent yields, so warnings about the final output join the run
	 * as asides instead of reopening an idle session.
	 */
	async settleJudgments(): Promise<void> {
		if (this.#pendingJudgments.size === 0) return;
		try {
			await withTimeout(Promise.all(this.#pendingJudgments), JUDGED_SETTLE_TIMEOUT_MS, "judged rules still pending");
		} catch (error) {
			logger.debug("TTSR judged rules unsettled at yield", {
				error: error instanceof Error ? error.message : String(error),
			});
		}
	}

	/** Marks names persisted with a delivered TTSR injection as injected. */
	markInjectedFromDetails(details: unknown): void {
		if (!details || typeof details !== "object" || Array.isArray(details)) return;
		const rules = "rules" in details ? details.rules : undefined;
		if (!Array.isArray(rules)) return;
		const ruleNames = rules.filter((ruleName): ruleName is string => typeof ruleName === "string");
		this.#markInjected(ruleNames);
		this.releaseDeferredReservationFromDetails(details);
	}

	/** Releases a queued delivery that was discarded before persistence. */
	releaseDeferredReservationFromDetails(details: unknown): void {
		if (!details || typeof details !== "object" || Array.isArray(details)) return;
		const rules = "rules" in details ? details.rules : undefined;
		const deliveryId = "deliveryId" in details ? details.deliveryId : undefined;
		if (!Array.isArray(rules) || typeof deliveryId !== "number") return;
		const ruleNames = rules.filter((ruleName): ruleName is string => typeof ruleName === "string");
		this.#releaseDeferredReservation(deliveryId, ruleNames);
	}

	/** Delivers per-tool reminders through the trusted passive-context channel. */
	afterToolCall(ctx: AfterToolCallContext): AfterToolCallResult | undefined {
		const reminder = this.#buildToolReminder(ctx.toolCall.id);
		return reminder ? { additionalContext: reminder } : undefined;
	}

	/**
	 * Bridged calls (Cursor exec handlers, eval) bypass the agent loop's `afterToolCall`. When the caller
	 * installed a passive-context sink the reminder goes there and the result stays untouched; without one
	 * (eval-bridged calls) it is folded into the result as a leading block, the only channel left.
	 */
	afterBridgedToolCall(
		toolCallId: string,
		result: AgentToolResult,
		context?: AgentToolContext,
	): AgentToolResult | undefined {
		const reminder = this.#buildToolReminder(toolCallId);
		if (!reminder) return undefined;
		if (context?.addAdditionalContext) {
			context.addAdditionalContext(reminder);
			return undefined;
		}
		return { ...result, content: [{ type: "text", text: reminder }, ...result.content] };
	}

	cancelBridgedToolCall(toolCallId: string): void {
		this.#perToolInjections.delete(toolCallId);
	}

	#buildToolReminder(toolCallId: string): string | undefined {
		const rules = this.#perToolInjections.get(toolCallId);
		if (!rules || rules.length === 0) return undefined;
		this.#perToolInjections.delete(toolCallId);
		const reminder = rules
			.map(rule =>
				prompt.render(ttsrToolReminderTemplate, {
					name: rule.name,
					path: this.#displayRulePath(rule.path),
					content: rule.content,
				}),
			)
			.join("\n\n");
		const ruleNames = rules.map(rule => rule.name.trim()).filter(name => name.length > 0);
		if (ruleNames.length > 0) this.#markInjected(ruleNames);
		return reminder;
	}

	/** Resolves and clears the current resume gate. */
	resolveResume(): void {
		this.#matchingCancellation.abort();
		this.#matchingCancellation = new AbortController();
		if (this.#attempt) this.#settleAttempt(this.#attempt);
		this.#assistantProcessing.clear();
		this.#resolveDeferred(this.#resumeResolve);
	}

	#resolveDeferred(resolve: (() => void) | undefined): void {
		if (!resolve) return;
		resolve();
		if (this.#resumeResolve !== resolve) return;
		this.#resumeResolve = undefined;
		this.#resumePromise = undefined;
	}

	#settleAttempt(attempt: InterruptedAttempt): void {
		attempt.cancellation.abort();
		attempt.resolve();
		if (this.#attempt !== attempt) return;
		this.#attempt = undefined;
		this.#assistantProcessing.delete(attempt.timestamp);
		this.#abortPending = false;
		this.#pendingInjections = [];
		this.#perToolInjections.clear();
	}

	#ensureResumePromise(): void {
		if (this.#resumePromise) return;
		const { promise, resolve } = Promise.withResolvers<void>();
		this.#resumePromise = promise;
		this.#resumeResolve = resolve;
	}

	#formatAbortReason(rules: Rule[]): string {
		const label = rules.length === 1 ? "rule" : "rules";
		return `TTSR matched ${label}: ${rules.map(rule => rule.name).join(", ")}`;
	}

	#getInjectionContent(): { content: string; rules: Rule[] } | undefined {
		if (this.#pendingInjections.length === 0) return undefined;
		const rules = this.#pendingInjections;
		const content = rules
			.map(rule =>
				prompt.render(ttsrInterruptTemplate, {
					name: rule.name,
					path: this.#displayRulePath(rule.path),
					content: rule.content,
				}),
			)
			.join("\n\n");
		this.#pendingInjections = [];
		return { content, rules };
	}

	#displayRulePath(rulePath: string): string {
		const cwd = this.#host.sessionManager.getCwd();
		const cwdRelative = relativePathWithinRoot(cwd, rulePath) ?? this.#displayPathWithinRoot(cwd, rulePath);
		if (cwdRelative) return cwdRelative;
		const homeRelative = relativePathWithinRoot(os.homedir(), rulePath);
		if (homeRelative) return `~/${homeRelative}`;
		return rulePath;
	}

	#displayPathWithinRoot(root: string, candidate: string): string | null {
		const relative = path.relative(path.resolve(root), path.resolve(candidate));
		return relative && !relative.startsWith("..") && !path.isAbsolute(relative) ? relative : null;
	}

	#addPendingInjections(rules: Rule[]): void {
		const seen = new Set(this.#pendingInjections.map(rule => rule.name));
		for (const rule of rules) {
			if (seen.has(rule.name) || this.#deferredReservations.has(rule.name)) continue;
			this.#pendingInjections.push(rule);
			seen.add(rule.name);
		}
	}

	#reserveDeferredInjection(rules: Rule[]): number {
		const deliveryId = ++this.#nextDeferredDeliveryId;
		for (const rule of rules) this.#deferredReservations.set(rule.name, deliveryId);
		return deliveryId;
	}

	#releaseDeferredReservation(deliveryId: number, ruleNames: string[]): void {
		for (const ruleName of ruleNames) {
			if (this.#deferredReservations.get(ruleName) === deliveryId) this.#deferredReservations.delete(ruleName);
		}
	}

	#extractToolCallId(matchContext: TtsrMatchContext): string | undefined {
		if (matchContext.source !== "tool") return undefined;
		const key = matchContext.streamKey;
		if (typeof key !== "string" || !key.startsWith("toolcall:")) return undefined;
		const id = key.slice("toolcall:".length);
		return id.length > 0 ? id : undefined;
	}

	#addPerToolInjections(
		toolCallId: string,
		rules: Rule[],
		{ markInjected = true }: { markInjected?: boolean } = {},
	): void {
		const bucket = this.#perToolInjections.get(toolCallId) ?? [];
		const seen = new Set(bucket.map(rule => rule.name));
		const claimedElsewhere = new Set<string>();
		for (const [otherId, otherBucket] of this.#perToolInjections) {
			if (otherId === toolCallId) continue;
			for (const rule of otherBucket) claimedElsewhere.add(rule.name);
		}
		const newlyAdded: string[] = [];
		for (const rule of rules) {
			if (seen.has(rule.name) || claimedElsewhere.has(rule.name)) continue;
			bucket.push(rule);
			seen.add(rule.name);
			newlyAdded.push(rule.name);
		}
		if (bucket.length === 0) return;
		this.#perToolInjections.set(toolCallId, bucket);
		if (markInjected && newlyAdded.length > 0) this.#manager?.markInjectedByNames(newlyAdded);
	}

	#markInjected(ruleNames: string[]): void {
		const uniqueRuleNames = Array.from(
			new Set(ruleNames.map(ruleName => ruleName.trim()).filter(ruleName => ruleName.length > 0)),
		);
		if (uniqueRuleNames.length === 0) return;
		this.#manager?.markInjectedByNames(uniqueRuleNames);
		this.#host.sessionManager.appendTtsrInjection(uniqueRuleNames);
	}

	/**
	 * Announce a trigger unless this stream already announced these rules.
	 * A delta match re-confirmed at `toolcall_end` evaluates the same buffer
	 * twice before the message_end cooldown commits; subscribers must see one
	 * event per violation, not one per evaluation.
	 */
	#emitTriggerOnce(matchContext: TtsrMatchContext, matches: Rule[]): void {
		const key = matchContext.streamKey;
		if (key) {
			let seen = this.#emittedTriggerRules.get(key);
			if (matches.every(match => seen?.has(match.name))) return;
			if (!seen) {
				seen = new Set();
				this.#emittedTriggerRules.set(key, seen);
			}
			for (const match of matches) seen.add(match.name);
		}
		this.#host.emitSessionEvent({ type: "ttsr_triggered", rules: matches }).catch(() => {});
	}
	#shouldInterrupt(matches: Rule[], matchContext: TtsrMatchContext): boolean {
		const globalMode = this.#manager?.getSettings().interruptMode ?? "always";
		for (const rule of matches) {
			const mode = rule.interruptMode ?? globalMode;
			if (mode === "never") continue;
			if (mode === "prose-only" && (matchContext.source === "text" || matchContext.source === "thinking")) {
				return true;
			}
			if (mode === "tool-only" && matchContext.source === "tool") return true;
			if (mode === "always") return true;
		}
		return false;
	}

	#queueDeferredInjectionIfNeeded(message: AssistantMessage): void {
		if (message.stopReason === "aborted" || message.stopReason === "error") this.#perToolInjections.clear();
		if (this.#abortPending || this.#pendingInjections.length === 0) return;
		if (message.stopReason === "aborted" || message.stopReason === "error") {
			this.#pendingInjections = [];
			return;
		}
		const injection = this.#getInjectionContent();
		if (!injection) return;
		const ruleNames = injection.rules.map(rule => rule.name);
		const deliveryId = this.#reserveDeferredInjection(injection.rules);
		try {
			this.#host.agent.followUp({
				role: "custom",
				customType: "ttsr-injection",
				content: injection.content,
				display: false,
				details: { rules: ruleNames, deliveryId },
				attribution: "agent",
				timestamp: Date.now(),
			});
		} catch (error) {
			this.#releaseDeferredReservation(deliveryId, ruleNames);
			throw error;
		}
		this.#ensureResumePromise();
		const resolve = this.#resumeResolve;
		const releaseReservation = () => {
			this.#releaseDeferredReservation(deliveryId, ruleNames);
			this.#resolveDeferred(resolve);
		};
		this.#host.scheduleAgentContinue({
			source: "ttsr-injection",
			delayMs: 1,
			generation: this.#host.promptGeneration(),
			onSkip: reason => {
				if (reason !== "should-continue-false") releaseReservation();
			},
			shouldContinue: () => {
				// A running agent may already have taken the queued message. In that
				// case message_end remains the authority for committing the cooldown.
				if (this.#host.agent.state.isStreaming) {
					this.#resolveDeferred(resolve);
					return false;
				}
				if (!this.#host.agent.hasQueuedMessages()) {
					releaseReservation();
					return false;
				}
				return true;
			},
			onError: releaseReservation,
		});
	}

	/**
	 * Asks the judge about each completed output of `message` in the background.
	 * Aborted and failed messages are skipped: their output never took effect.
	 */
	#judgeCompletedMessage(message: AssistantMessage): void {
		if (!this.#manager?.hasJudgedRules() || message.stopReason === "aborted" || message.stopReason === "error") {
			return;
		}
		const generation = this.#host.sessionGeneration();
		for (const output of this.#inspector.outputs(message)) {
			const pending: Promise<void> = this.#judgeOutput(output, generation)
				.catch(error => {
					logger.warn("TTSR judged rule check failed", {
						subject: output.subject,
						error: error instanceof Error ? error.message : String(error),
					});
				})
				.finally(() => this.#pendingJudgments.delete(pending));
			this.#pendingJudgments.add(pending);
		}
	}

	/** One judge request per output: every eligible rule's question shares the billed state. */
	async #judgeOutput(output: TtsrOutput, generation: number): Promise<void> {
		const manager = this.#manager;
		if (!manager) return;
		const candidates = await manager.judgedCandidates(output.content, output.context);
		if (candidates.length === 0) return;
		const judge = this.#host.ruleJudge();
		if (!judge) return;
		const flagged = await judgeRules(judge, output, candidates);
		if (flagged.length === 0 || this.#host.sessionGeneration() !== generation) return;
		const rules = manager.claim(flagged);
		if (rules.length === 0) return;
		this.#host.emitSessionEvent({ type: "ttsr_triggered", rules }).catch(() => {});
		const warning = rules
			.map(rule =>
				prompt.render(ttsrWarningTemplate, {
					name: rule.name,
					path: this.#displayRulePath(rule.path),
					subject: output.subject,
					content: rule.content,
				}),
			)
			.join("\n\n");
		await this.#host.deliverRuleWarning(
			warning,
			rules.map(rule => rule.name),
		);
	}

	#getStreamingToolCallBlock(message: AgentMessage, contentIndex: number): ToolCall | undefined {
		if (message.role !== "assistant") return undefined;
		const content = message.content;
		if (!Array.isArray(content) || contentIndex < 0 || contentIndex >= content.length) return undefined;
		const block = content[contentIndex];
		return block && typeof block === "object" && block.type === "toolCall" ? (block as ToolCall) : undefined;
	}

	#checkStream(
		delta: string,
		matchContext: TtsrMatchContext,
		toolCall: ToolCall | undefined,
		isFinal = false,
	): Rule[] {
		if (!this.#manager) return [];
		const entries = this.#inspector.entries(toolCall);
		if (entries) {
			const matches: Rule[] = [];
			for (const entry of entries) {
				matches.push(
					...this.#manager.checkSnapshot(entry.digest, this.#inspector.perFileContext(matchContext, entry.path)),
				);
			}
			return matches;
		}
		const digest = this.#inspector.digest(toolCall);
		if (digest !== undefined) return this.#manager.checkSnapshot(digest, matchContext);
		// Tools without matcher hooks accumulate raw argument deltas. Providers
		// that emit toolcall_start -> toolcall_end with no intermediate deltas
		// (Cursor exec synthesis, OpenAI lossy-proxy fallback) leave that buffer
		// empty, so the finalized arguments must seed the snapshot themselves.
		const finalArgs = isFinal ? toolCall?.arguments : undefined;
		if (finalArgs !== undefined && finalArgs !== null) {
			const snapshot = typeof finalArgs === "string" ? finalArgs : JSON.stringify(finalArgs);
			return this.#manager.checkSnapshot(snapshot, matchContext);
		}
		return this.#manager.checkDelta(delta, matchContext);
	}

	async #checkAstStream(matchContext: TtsrMatchContext, toolCall: ToolCall | undefined): Promise<Rule[]> {
		if (!this.#manager) return [];
		const entries = this.#inspector.entries(toolCall);
		if (entries) {
			const matches: Rule[] = [];
			for (const entry of entries) {
				matches.push(
					...(await this.#manager.checkAstSnapshot(
						entry.digest,
						this.#inspector.perFileContext(matchContext, entry.path),
					)),
				);
			}
			return matches;
		}
		const digest = this.#inspector.digest(toolCall);
		return digest === undefined ? [] : this.#manager.checkAstSnapshot(digest, matchContext);
	}

	#handleMatches(matches: Rule[], matchContext: TtsrMatchContext, targetTimestamp: number | undefined): boolean {
		const shouldInterrupt = this.#shouldInterrupt(matches, matchContext);
		const matchedToolId = this.#extractToolCallId(matchContext);
		const perToolId = shouldInterrupt ? undefined : matchedToolId;
		if (perToolId) {
			this.#addPerToolInjections(perToolId, matches);
			this.#emitTriggerOnce(matchContext, matches);
			return false;
		}
		if (
			shouldInterrupt &&
			this.#attempt?.generation === this.#host.promptGeneration() &&
			this.#attempt.timestamp === targetTimestamp
		) {
			// Same-timestamp matches during continuation belong to the interrupted response; continuation has its own identity.
			if (!this.#abortPending || this.#attempt.cancellation.signal.aborted) return false;
			this.#addPendingInjections(matches);
			this.#emitTriggerOnce(matchContext, matches);
			return true;
		}
		if (shouldInterrupt && this.#attempt) this.#settleAttempt(this.#attempt);
		if (shouldInterrupt) this.#resolveDeferred(this.#resumeResolve);
		this.#addPendingInjections(matches);
		if (!shouldInterrupt) return false;

		if (targetTimestamp === undefined) return false;
		// Capture the original core request before abort can settle or replace it.
		const { promise: resume, resolve } = Promise.withResolvers<void>();
		const attempt: InterruptedAttempt = {
			generation: this.#host.promptGeneration(),
			timestamp: targetTimestamp,
			coreIdle: this.#host.agent.waitForIdle(),
			cancellation: new AbortController(),
			resume,
			resolve,
		};
		this.#attempt = attempt;
		this.#abortPending = true;
		const abortReason = this.#formatAbortReason(matches);
		this.#host.agent.abort(
			matchedToolId
				? createToolScopedAbortReason(
						abortReason,
						{ [matchedToolId]: abortReason },
						"TTSR interrupt on another tool call",
					)
				: abortReason,
		);
		this.#emitTriggerOnce(matchContext, matches);
		this.#host.schedulePostPromptTask(
			async taskSignal => {
				const signal = AbortSignal.any([taskSignal, attempt.cancellation.signal]);
				const ownsAttempt = () =>
					!signal.aborted && this.#attempt === attempt && this.#host.promptGeneration() === attempt.generation;
				try {
					await untilAborted(signal, attempt.coreIdle);
					if (!ownsAttempt()) return;
					const target = this.#assistantProcessing.get(attempt.timestamp);
					if (!target || !(await untilAborted(signal, target.outcome)) || !this.#recorded.has(target.message)) {
						if (ownsAttempt())
							this.#host.emitNotice(
								"warning",
								"TTSR continuation stopped because the interrupted response was not successfully recorded.",
								"ttsr",
							);
						return;
					}
					if (!ownsAttempt()) return;
					const targetAssistantIndex = this.#host.agent.state.messages.indexOf(target.message);
					if (targetAssistantIndex === -1) {
						this.#host.emitNotice(
							"warning",
							"TTSR continuation stopped because the interrupted response is missing.",
							"ttsr",
						);
						return;
					}
					this.#abortPending = false;
					this.#matchingCancellation.abort();
					this.#matchingCancellation = new AbortController();
					this.#perToolInjections.clear();
					if (this.#manager?.getSettings().contextMode === "discard") {
						this.#host.agent.replaceMessages(this.#host.agent.state.messages.slice(0, targetAssistantIndex));
					}
					const injection = this.#getInjectionContent();
					if (injection) {
						const details = { rules: injection.rules.map(rule => rule.name) };
						this.#host.agent.appendMessage({
							role: "custom",
							customType: "ttsr-injection",
							content: injection.content,
							display: false,
							details,
							attribution: "agent",
							timestamp: Date.now(),
						});
						this.#host.sessionManager.appendCustomMessageEntry(
							"ttsr-injection",
							injection.content,
							false,
							details,
							"agent",
						);
						this.#markInjected(details.rules);
					}
					await untilAborted(signal, this.#host.agent.continue());
				} catch {
					if (ownsAttempt()) this.#host.emitNotice("warning", "TTSR continuation could not complete.", "ttsr");
				} finally {
					if (ownsAttempt() && this.#abortPending) {
						this.#matchingCancellation.abort();
						this.#matchingCancellation = new AbortController();
					}
					this.#settleAttempt(attempt);
				}
			},
			{ delayMs: 50, generation: attempt.generation, onSkip: () => this.#settleAttempt(attempt) },
		);
		return true;
	}
}
