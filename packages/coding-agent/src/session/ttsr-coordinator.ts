import * as os from "node:os";
import * as path from "node:path";
import {
	type AfterToolCallContext,
	type AfterToolCallResult,
	type Agent,
	type AgentEvent,
	type AgentMessage,
	createToolScopedAbortReason,
} from "@oh-my-pi/pi-agent-core";
import type { AssistantMessage, ToolCall } from "@oh-my-pi/pi-ai";
import { isRecord, prompt, relativePathWithinRoot, untilAborted } from "@oh-my-pi/pi-utils";
import type { Rule } from "../capability/rule";
import type { Settings } from "../config/settings";
import type { TtsrManager, TtsrMatchContext } from "../export/ttsr";
import ttsrInterruptTemplate from "../prompts/system/ttsr-interrupt.md" with { type: "text" };
import ttsrToolReminderTemplate from "../prompts/system/ttsr-tool-reminder.md" with { type: "text" };
import type { AgentSessionEvent } from "./agent-session-events";
import type { SessionManager } from "./session-manager";

interface TtsrContinueOptions {
	delayMs?: number;
	generation?: number;
	shouldContinue?: () => boolean;
	onSkip?: () => void;
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
}

/** Coordinates TTSR stream matching, interruption, injection, and resume gates. */
export class TtsrCoordinator {
	readonly #host: TtsrCoordinatorHost;
	readonly #manager: TtsrManager | undefined;
	#pendingInjections: Rule[] = [];
	#perToolInjections = new Map<string, Rule[]>();
	#abortPending = false;
	#attempt: InterruptedAttempt | undefined;
	#assistantProcessing = new Map<number, AssistantProcessing>();
	#recorded = new WeakSet<AssistantMessage>();
	#eventOwnership = new WeakMap<AgentEvent, EventOwnership>();
	#matchingCancellation = new AbortController();
	#resumePromise: Promise<void> | undefined;
	#resumeResolve: (() => void) | undefined;

	constructor(host: TtsrCoordinatorHost, manager: TtsrManager | undefined) {
		this.#host = host;
		this.#manager = manager;
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

	/** Advances repeat-after-gap tracking at turn end. */
	onTurnEnd(): void {
		this.#manager?.incrementMessageCount();
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
		let matchContext: TtsrMatchContext | undefined;
		let streamingToolCall: ToolCall | undefined;
		if (assistantEvent.type === "text_delta") {
			matchContext = { source: "text" };
		} else if (assistantEvent.type === "thinking_delta") {
			matchContext = { source: "thinking" };
		} else if (assistantEvent.type === "toolcall_delta") {
			streamingToolCall = this.#getStreamingToolCallBlock(event.message, assistantEvent.contentIndex);
			matchContext = this.#getToolMatchContext(streamingToolCall, assistantEvent.contentIndex);
		}
		if (!matchContext || !("delta" in assistantEvent)) return false;
		const targetMessageTimestamp = event.message.role === "assistant" ? event.message.timestamp : undefined;
		const matches = this.#checkStream(assistantEvent.delta, matchContext, streamingToolCall);
		if (!ownsEvent()) return false;
		if (matches.length > 0 && this.#handleMatches(matches, matchContext, targetMessageTimestamp)) return true;
		// AST rules use the reconstructed edit/write snapshot and are awaited so
		// the manager self-throttles native matching.
		if (matchContext.source === "tool" && this.#manager.hasAstRules()) {
			let astMatches: Rule[];
			try {
				astMatches = await untilAborted(ownership.signal, this.#checkAstStream(matchContext, streamingToolCall));
			} catch (error) {
				if (!ownsEvent()) return false;
				throw error;
			}
			if (!ownsEvent()) return false;
			if (astMatches.length > 0 && this.#handleMatches(astMatches, matchContext, targetMessageTimestamp))
				return true;
		}
		return false;
	}

	/** Settles the previous resume gate and queues any deferred injection. */
	onAssistantMessageEnd(message: AssistantMessage): void {
		// Gate on abortPending, not stopReason: unrelated aborts have no TTSR continuation.
		if (!this.#attempt && !this.#abortPending) this.#resolveDeferred(this.#resumeResolve);
		this.#queueDeferredInjectionIfNeeded(message);
	}

	/** Marks names persisted with a delivered TTSR injection as injected. */
	markInjectedFromDetails(details: unknown): void {
		if (!details || typeof details !== "object" || Array.isArray(details)) return;
		const rules = "rules" in details ? details.rules : undefined;
		if (!Array.isArray(rules)) return;
		this.#markInjected(rules.filter((ruleName): ruleName is string => typeof ruleName === "string"));
	}

	/** Folds per-tool reminders into the matched tool's result. */
	afterToolCall(ctx: AfterToolCallContext): AfterToolCallResult | undefined {
		const rules = this.#perToolInjections.get(ctx.toolCall.id);
		if (!rules || rules.length === 0) return undefined;
		this.#perToolInjections.delete(ctx.toolCall.id);
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
		if (ruleNames.length > 0) this.#host.sessionManager.appendTtsrInjection(ruleNames);
		return { content: [{ type: "text", text: reminder }, ...ctx.result.content] };
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
			if (seen.has(rule.name)) continue;
			this.#pendingInjections.push(rule);
			seen.add(rule.name);
		}
	}

	#extractToolCallId(matchContext: TtsrMatchContext): string | undefined {
		if (matchContext.source !== "tool") return undefined;
		const key = matchContext.streamKey;
		if (typeof key !== "string" || !key.startsWith("toolcall:")) return undefined;
		const id = key.slice("toolcall:".length);
		return id.length > 0 ? id : undefined;
	}

	#addPerToolInjections(toolCallId: string, rules: Rule[]): void {
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
		if (newlyAdded.length > 0) this.#manager?.markInjectedByNames(newlyAdded);
	}

	#markInjected(ruleNames: string[]): void {
		const uniqueRuleNames = Array.from(
			new Set(ruleNames.map(ruleName => ruleName.trim()).filter(ruleName => ruleName.length > 0)),
		);
		if (uniqueRuleNames.length === 0) return;
		this.#manager?.markInjectedByNames(uniqueRuleNames);
		this.#host.sessionManager.appendTtsrInjection(uniqueRuleNames);
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
		this.#host.agent.followUp({
			role: "custom",
			customType: "ttsr-injection",
			content: injection.content,
			display: false,
			details: { rules: injection.rules.map(rule => rule.name) },
			attribution: "agent",
			timestamp: Date.now(),
		});
		this.#ensureResumePromise();
		const resolve = this.#resumeResolve;
		this.#host.scheduleAgentContinue({
			delayMs: 1,
			generation: this.#host.promptGeneration(),
			onSkip: () => this.#resolveDeferred(resolve),
			shouldContinue: () => {
				if (this.#host.agent.state.isStreaming || !this.#host.agent.hasQueuedMessages()) {
					this.#resolveDeferred(resolve);
					return false;
				}
				return true;
			},
			onError: () => this.#resolveDeferred(resolve),
		});
	}

	#getStreamingToolCallBlock(message: AgentMessage, contentIndex: number): ToolCall | undefined {
		if (message.role !== "assistant") return undefined;
		const content = message.content;
		if (!Array.isArray(content) || contentIndex < 0 || contentIndex >= content.length) return undefined;
		const block = content[contentIndex];
		return block && typeof block === "object" && block.type === "toolCall" ? (block as ToolCall) : undefined;
	}

	#getToolMatchContext(toolCall: ToolCall | undefined, contentIndex: number): TtsrMatchContext {
		const context: TtsrMatchContext = { source: "tool" };
		if (!toolCall) return context;
		context.toolName = toolCall.name;
		context.streamKey = toolCall.id ? `toolcall:${toolCall.id}` : `tool:${toolCall.name}:${contentIndex}`;
		context.filePaths = this.#extractToolFilePaths(toolCall);
		return context;
	}

	#extractToolFilePaths(toolCall: ToolCall): string[] | undefined {
		const args = toolCall.arguments ?? {};
		const tool = this.#resolveTool(toolCall);
		const toolPaths = tool?.matcherPaths?.(args);
		if (toolPaths && toolPaths.length > 0) {
			const normalized = toolPaths.flatMap(filePath => this.#normalizePathCandidates(filePath));
			if (normalized.length > 0) return Array.from(new Set(normalized));
		}
		return this.#extractFilePathsFromArgs(args);
	}

	#checkStream(delta: string, matchContext: TtsrMatchContext, toolCall: ToolCall | undefined): Rule[] {
		if (!this.#manager) return [];
		const entries = this.#resolveMatcherEntries(toolCall);
		if (entries) {
			const matches: Rule[] = [];
			for (const entry of entries) {
				matches.push(...this.#manager.checkSnapshot(entry.digest, this.#perFileContext(matchContext, entry.path)));
			}
			return matches;
		}
		const digest = this.#resolveMatcherDigest(toolCall);
		return digest !== undefined
			? this.#manager.checkSnapshot(digest, matchContext)
			: this.#manager.checkDelta(delta, matchContext);
	}

	#resolveMatcherDigest(toolCall: ToolCall | undefined): string | undefined {
		const tool = this.#resolveTool(toolCall);
		return tool?.matcherDigest?.(toolCall?.arguments ?? {});
	}

	#resolveMatcherEntries(toolCall: ToolCall | undefined): readonly { path: string; digest: string }[] | undefined {
		const tool = this.#resolveTool(toolCall);
		const entries = tool?.matcherEntries?.(toolCall?.arguments ?? {});
		return entries && entries.length > 0 ? entries : undefined;
	}

	#resolveTool(toolCall: ToolCall | undefined) {
		if (!toolCall) return undefined;
		const tools = this.#host.agent.state.tools;
		return (
			tools.find(tool => tool.name === toolCall.name) ??
			tools.find(tool => tool.customWireName !== undefined && tool.customWireName === toolCall.name)
		);
	}

	#perFileContext(base: TtsrMatchContext, filePath: string): TtsrMatchContext {
		const filePaths = this.#normalizePathCandidates(filePath);
		return {
			...base,
			filePaths: filePaths.length > 0 ? filePaths : [filePath],
			streamKey: base.streamKey ? `${base.streamKey}#${filePath}` : undefined,
		};
	}

	async #checkAstStream(matchContext: TtsrMatchContext, toolCall: ToolCall | undefined): Promise<Rule[]> {
		if (!this.#manager) return [];
		const entries = this.#resolveMatcherEntries(toolCall);
		if (entries) {
			const matches: Rule[] = [];
			for (const entry of entries) {
				matches.push(
					...(await this.#manager.checkAstSnapshot(entry.digest, this.#perFileContext(matchContext, entry.path))),
				);
			}
			return matches;
		}
		const digest = this.#resolveMatcherDigest(toolCall);
		return digest === undefined ? [] : this.#manager.checkAstSnapshot(digest, matchContext);
	}

	#handleMatches(matches: Rule[], matchContext: TtsrMatchContext, targetTimestamp: number | undefined): boolean {
		const shouldInterrupt = this.#shouldInterrupt(matches, matchContext);
		const matchedToolId = this.#extractToolCallId(matchContext);
		const perToolId = shouldInterrupt ? undefined : matchedToolId;
		if (perToolId) {
			this.#addPerToolInjections(perToolId, matches);
			this.#host.emitSessionEvent({ type: "ttsr_triggered", rules: matches }).catch(() => {});
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
			this.#host.emitSessionEvent({ type: "ttsr_triggered", rules: matches }).catch(() => {});
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
		this.#host.emitSessionEvent({ type: "ttsr_triggered", rules: matches }).catch(() => {});
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
					this.#settleAttempt(attempt);
				}
			},
			{ delayMs: 50, generation: attempt.generation, onSkip: () => this.#settleAttempt(attempt) },
		);
		return true;
	}

	#extractFilePathsFromArgs(args: unknown): string[] | undefined {
		if (!isRecord(args)) return undefined;
		const rawPaths: string[] = [];
		for (const key in args) {
			const value = args[key];
			const normalizedKey = key.toLowerCase();
			if (typeof value === "string" && (normalizedKey === "path" || normalizedKey.endsWith("path"))) {
				rawPaths.push(value);
				continue;
			}
			if (Array.isArray(value) && (normalizedKey === "paths" || normalizedKey.endsWith("paths"))) {
				for (const candidate of value) if (typeof candidate === "string") rawPaths.push(candidate);
			}
		}
		const normalizedPaths = rawPaths.flatMap(filePath => this.#normalizePathCandidates(filePath));
		return normalizedPaths.length === 0 ? undefined : Array.from(new Set(normalizedPaths));
	}

	#normalizePathCandidates(rawPath: string): string[] {
		const trimmed = rawPath.trim();
		if (trimmed.length === 0) return [];
		const normalizedInput = trimmed.replaceAll("\\", "/");
		const candidates = new Set<string>([normalizedInput]);
		if (normalizedInput.startsWith("./")) candidates.add(normalizedInput.slice(2));
		const cwd = this.#host.sessionManager.getCwd();
		const absolutePath = path.isAbsolute(trimmed) ? path.normalize(trimmed) : path.resolve(cwd, trimmed);
		candidates.add(absolutePath.replaceAll("\\", "/"));
		const relative = path.relative(cwd, absolutePath).replaceAll("\\", "/");
		if (relative && relative !== "." && !relative.startsWith("../") && relative !== "..") candidates.add(relative);
		return Array.from(candidates);
	}
}
