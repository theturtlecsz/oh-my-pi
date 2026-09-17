import { describe, expect, it } from "bun:test";
import type { BeforeAgentStartEventResult, CustomMessagePayload, ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { mergeBeforeAgentStartResults } from "../extensions/workflow/host";

interface SendMessageInvocation {
	readonly message: CustomMessagePayload;
	readonly options?: { readonly triggerTurn?: boolean; readonly deliverAs?: "steer" | "followUp" | "nextTurn" };
}

class MockExtensionApi implements Pick<ExtensionAPI, "sendMessage"> {
	readonly calls: SendMessageInvocation[] = [];

	sendMessage<T = unknown>(
		message: CustomMessagePayload<T>,
		options?: { triggerTurn?: boolean; deliverAs?: "steer" | "followUp" | "nextTurn" },
	): void {
		this.calls.push({
			message: message as CustomMessagePayload,
			options,
		});
	}
}

describe("mergeBeforeAgentStartResults", () => {
	const bridgeBundleMessage: CustomMessagePayload = {
		customType: "work-knowledge-bundle",
		content: "{\"mandatory\":{},\"optional\":{}}",
		details: { bundle_id: "00000000-0000-0000-0000-000000000001" },
	};

	const hostDigestMessage: CustomMessagePayload = {
		customType: "work-digest",
		content: "── Current Task / Status ──",
	};

	describe("branch: neither has message", () => {
		it("returns undefined when both results are undefined", () => {
			const pi = new MockExtensionApi();
			const result = mergeBeforeAgentStartResults(undefined, undefined, pi);
			expect(result).toBeUndefined();
			expect(pi.calls).toHaveLength(0);
		});

		it("returns undefined when both results exist but have no message or systemPrompt", () => {
			const pi = new MockExtensionApi();
			const result = mergeBeforeAgentStartResults({}, {}, pi);
			expect(result).toBeUndefined();
			expect(pi.calls).toHaveLength(0);
		});

		it("propagates bridge systemPrompt when neither result has a message", () => {
			const pi = new MockExtensionApi();
			const bridgePrompt = ["bridge system prompt directive"];
			const result = mergeBeforeAgentStartResults({ systemPrompt: bridgePrompt }, undefined, pi);
			expect(result).toEqual({ systemPrompt: bridgePrompt });
			expect(pi.calls).toHaveLength(0);
		});

		it("propagates host systemPrompt when bridge is absent and host has no message", () => {
			const pi = new MockExtensionApi();
			const hostPrompt = ["host system prompt directive"];
			const result = mergeBeforeAgentStartResults(undefined, { systemPrompt: hostPrompt }, pi);
			expect(result).toEqual({ systemPrompt: hostPrompt });
			expect(pi.calls).toHaveLength(0);
		});

		it("prioritizes bridge systemPrompt over host systemPrompt when neither has a message", () => {
			const pi = new MockExtensionApi();
			const bridgePrompt = ["bridge system prompt"];
			const hostPrompt = ["host system prompt"];
			const result = mergeBeforeAgentStartResults(
				{ systemPrompt: bridgePrompt },
				{ systemPrompt: hostPrompt },
				pi,
			);
			expect(result).toEqual({ systemPrompt: bridgePrompt });
			expect(pi.calls).toHaveLength(0);
		});
	});

	describe("branch: bridge-only has message", () => {
		it("returns bridgeResult directly when hostResult is undefined", () => {
			const pi = new MockExtensionApi();
			const bridgeResult: BeforeAgentStartEventResult = { message: bridgeBundleMessage };
			const result = mergeBeforeAgentStartResults(bridgeResult, undefined, pi);
			expect(result).toBe(bridgeResult);
			expect(pi.calls).toHaveLength(0);
		});

		it("returns bridgeResult directly when hostResult has no message", () => {
			const pi = new MockExtensionApi();
			const bridgeResult: BeforeAgentStartEventResult = {
				message: bridgeBundleMessage,
				systemPrompt: ["bridge directive"],
			};
			const hostResult: BeforeAgentStartEventResult = {
				systemPrompt: ["host directive"],
			};
			const result = mergeBeforeAgentStartResults(bridgeResult, hostResult, pi);
			expect(result).toBe(bridgeResult);
			expect(pi.calls).toHaveLength(0);
		});
	});

	describe("branch: host-only has message", () => {
		it("returns hostResult directly when bridgeResult is undefined", () => {
			const pi = new MockExtensionApi();
			const hostResult: BeforeAgentStartEventResult = { message: hostDigestMessage };
			const result = mergeBeforeAgentStartResults(undefined, hostResult, pi);
			expect(result).toBe(hostResult);
			expect(pi.calls).toHaveLength(0);
		});

		it("returns hostResult directly when bridgeResult has no message", () => {
			const pi = new MockExtensionApi();
			const bridgeResult: BeforeAgentStartEventResult = {
				systemPrompt: ["bridge directive"],
			};
			const hostResult: BeforeAgentStartEventResult = {
				message: hostDigestMessage,
				systemPrompt: ["host directive"],
			};
			const result = mergeBeforeAgentStartResults(bridgeResult, hostResult, pi);
			expect(result).toBe(hostResult);
			expect(pi.calls).toHaveLength(0);
		});
	});

	describe("branch: both messages (dual delivery and retention)", () => {
		it("delivers host message via pi.sendMessage nextTurn and returns bridge message", () => {
			const pi = new MockExtensionApi();
			const bridgeResult: BeforeAgentStartEventResult = { message: bridgeBundleMessage };
			const hostResult: BeforeAgentStartEventResult = { message: hostDigestMessage };

			const result = mergeBeforeAgentStartResults(bridgeResult, hostResult, pi);

			// Host message is delivered via pi.sendMessage with { deliverAs: "nextTurn" }
			expect(pi.calls).toHaveLength(1);
			expect(pi.calls[0].message).toBe(hostDigestMessage);
			expect(pi.calls[0].options).toEqual({ deliverAs: "nextTurn" });

			// Bridge message is returned in the result
			expect(result).toEqual({ message: bridgeBundleMessage });

			// Both messages are proven retained: host delivered asynchronously, bridge returned synchronously
			expect(pi.calls[0].message).not.toBe(result?.message);
		});

		it("propagates bridge systemPrompt when bridge provides it and both have messages", () => {
			const pi = new MockExtensionApi();
			const bridgePrompt = ["bridge system prompt"];
			const bridgeResult: BeforeAgentStartEventResult = {
				message: bridgeBundleMessage,
				systemPrompt: bridgePrompt,
			};
			const hostResult: BeforeAgentStartEventResult = {
				message: hostDigestMessage,
			};

			const result = mergeBeforeAgentStartResults(bridgeResult, hostResult, pi);

			expect(pi.calls).toHaveLength(1);
			expect(pi.calls[0].message).toBe(hostDigestMessage);
			expect(pi.calls[0].options).toEqual({ deliverAs: "nextTurn" });

			expect(result).toEqual({
				message: bridgeBundleMessage,
				systemPrompt: bridgePrompt,
			});
		});

		it("propagates host systemPrompt when only host provides it and both have messages", () => {
			const pi = new MockExtensionApi();
			const hostPrompt = ["host fallback prompt"];
			const bridgeResult: BeforeAgentStartEventResult = {
				message: bridgeBundleMessage,
			};
			const hostResult: BeforeAgentStartEventResult = {
				message: hostDigestMessage,
				systemPrompt: hostPrompt,
			};

			const result = mergeBeforeAgentStartResults(bridgeResult, hostResult, pi);

			expect(pi.calls).toHaveLength(1);
			expect(pi.calls[0].message).toBe(hostDigestMessage);
			expect(pi.calls[0].options).toEqual({ deliverAs: "nextTurn" });

			expect(result).toEqual({
				message: bridgeBundleMessage,
				systemPrompt: hostPrompt,
			});
		});

		it("propagates bridge systemPrompt with precedence over host systemPrompt when both provide prompts and messages", () => {
			const pi = new MockExtensionApi();
			const bridgePrompt = ["bridge overriding prompt"];
			const hostPrompt = ["host overridden prompt"];
			const bridgeResult: BeforeAgentStartEventResult = {
				message: bridgeBundleMessage,
				systemPrompt: bridgePrompt,
			};
			const hostResult: BeforeAgentStartEventResult = {
				message: hostDigestMessage,
				systemPrompt: hostPrompt,
			};

			const result = mergeBeforeAgentStartResults(bridgeResult, hostResult, pi);

			expect(pi.calls).toHaveLength(1);
			expect(pi.calls[0].message).toBe(hostDigestMessage);
			expect(pi.calls[0].options).toEqual({ deliverAs: "nextTurn" });

			expect(result).toEqual({
				message: bridgeBundleMessage,
				systemPrompt: bridgePrompt,
			});
		});

		it("gracefully operates when pi is omitted or undefined", () => {
			const bridgeResult: BeforeAgentStartEventResult = { message: bridgeBundleMessage };
			const hostResult: BeforeAgentStartEventResult = { message: hostDigestMessage };

			const result = mergeBeforeAgentStartResults(bridgeResult, hostResult, undefined);

			expect(result).toEqual({ message: bridgeBundleMessage });
		});
	});
});
