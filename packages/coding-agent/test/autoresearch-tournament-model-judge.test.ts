import { describe, expect, it } from "bun:test";
import type { Api, AssistantMessage, Context, Model, SimpleStreamOptions, Usage } from "@oh-my-pi/pi-ai";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import {
	createModelJudge,
	createModelSummarizer,
	drawTwoLabels,
	NEUTRAL_LABEL_POOL,
	parseJudgeAnswer,
} from "../src/autoresearch/tournament/model-judge";

function makeUsage(overrides: Partial<Usage> = {}): Usage {
	return {
		input: 10,
		output: 5,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 15,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		...overrides,
	};
}

function makeAssistantMessage(text: string, overrides: Partial<AssistantMessage> = {}): AssistantMessage {
	return {
		role: "assistant",
		api: "openai-completions",
		provider: "openai",
		model: "gpt-4o-mini",
		content: [{ type: "text", text }],
		stopReason: "stop",
		timestamp: Date.now(),
		usage: makeUsage(),
		...overrides,
	};
}

const mockModel = getBundledModel("openai", "gpt-4o-mini") as Model<Api>;

describe("NEUTRAL_LABEL_POOL and drawTwoLabels", () => {
	it("contains at least two neutral labels and never literal A or B", () => {
		expect(NEUTRAL_LABEL_POOL.length).toBeGreaterThanOrEqual(10);
		expect(NEUTRAL_LABEL_POOL).not.toContain("A");
		expect(NEUTRAL_LABEL_POOL).not.toContain("B");
		expect(NEUTRAL_LABEL_POOL).not.toContain("tie");
	});

	it("draws two distinct labels from the pool", () => {
		let callCount = 0;
		const mockRng = () => {
			callCount += 1;
			return (callCount * 0.1) % 1;
		};
		const [labelA, labelB] = drawTwoLabels(NEUTRAL_LABEL_POOL, mockRng);
		expect(labelA).not.toBe(labelB);
		expect(NEUTRAL_LABEL_POOL).toContain(labelA);
		expect(NEUTRAL_LABEL_POOL).toContain(labelB);
	});
});

describe("parseJudgeAnswer", () => {
	it("maps winner labelA to A and labelB to B", () => {
		const resA = parseJudgeAnswer(JSON.stringify({ winner: "Emerald" }), "Emerald", "Sapphire");
		expect(resA.outcome).toBe("A");

		const resB = parseJudgeAnswer(JSON.stringify({ winner: "Sapphire" }), "Emerald", "Sapphire");
		expect(resB.outcome).toBe("B");
	});

	it("maps exact tie to tie", () => {
		const resLower = parseJudgeAnswer(JSON.stringify({ winner: "tie" }), "Emerald", "Sapphire");
		expect(resLower.outcome).toBe("tie");
	});

	it("throws on non-exact case or untrimmed winner string", () => {
		expect(() => parseJudgeAnswer(JSON.stringify({ winner: "Tie" }), "Emerald", "Sapphire")).toThrow(
			/Unknown judge winner/,
		);
		expect(() => parseJudgeAnswer(JSON.stringify({ winner: "TIE" }), "Emerald", "Sapphire")).toThrow(
			/Unknown judge winner/,
		);
		expect(() => parseJudgeAnswer(JSON.stringify({ winner: "emerald" }), "Emerald", "Sapphire")).toThrow(
			/Unknown judge winner/,
		);
		expect(() => parseJudgeAnswer(JSON.stringify({ winner: " Sapphire " }), "Emerald", "Sapphire")).toThrow(
			/Unknown judge winner/,
		);
	});

	it("re-keys valid probabilities from labels to A and B, including tie", () => {
		const payload = {
			winner: "Emerald",
			probabilities: {
				Emerald: 0.7,
				Sapphire: 0.2,
				tie: 0.1,
			},
		};
		const res = parseJudgeAnswer(JSON.stringify(payload), "Emerald", "Sapphire");
		expect(res.outcome).toBe("A");
		expect(res.probabilities).toEqual({ A: 0.7, B: 0.2, tie: 0.1 });
	});

	it("omits probabilities if values are out of [0, 1] range or not numbers", () => {
		const invalidPayload = {
			winner: "Sapphire",
			probabilities: {
				Emerald: -0.1,
				Sapphire: 1.5,
			},
		};
		const res = parseJudgeAnswer(JSON.stringify(invalidPayload), "Emerald", "Sapphire");
		expect(res.outcome).toBe("B");
		expect(res.probabilities).toBeUndefined();
	});

	it("omits probabilities if keys are not the drawn labels (e.g. fallback A and B)", () => {
		const fallbackPayload = {
			winner: "Emerald",
			probabilities: {
				A: 0.9,
				B: 0.1,
			},
		};
		const res = parseJudgeAnswer(JSON.stringify(fallbackPayload), "Emerald", "Sapphire");
		expect(res.outcome).toBe("A");
		expect(res.probabilities).toBeUndefined();
	});

	it("throws on JSON wrapped in markdown code fence", () => {
		const markdownText = '```json\n{"winner": "Sapphire"}\n```';
		expect(() => parseJudgeAnswer(markdownText, "Emerald", "Sapphire")).toThrow(/Malformed judge response/);
	});

	it("throws on unknown winner label", () => {
		expect(() => parseJudgeAnswer(JSON.stringify({ winner: "Diamond" }), "Emerald", "Sapphire")).toThrow(
			/Unknown judge winner/,
		);
	});

	it("throws on literal A or B when neutral labels were assigned", () => {
		expect(() => parseJudgeAnswer(JSON.stringify({ winner: "A" }), "Emerald", "Sapphire")).toThrow(
			/Unknown judge winner/,
		);
		expect(() => parseJudgeAnswer(JSON.stringify({ winner: "B" }), "Emerald", "Sapphire")).toThrow(
			/Unknown judge winner/,
		);
	});

	it("throws on malformed JSON or empty string", () => {
		expect(() => parseJudgeAnswer("not a json", "Emerald", "Sapphire")).toThrow(/Malformed judge response/);
		expect(() => parseJudgeAnswer("", "Emerald", "Sapphire")).toThrow(/Malformed judge response/);
	});

	it("throws on JSON non-object", () => {
		expect(() => parseJudgeAnswer("123", "Emerald", "Sapphire")).toThrow(/expected a single JSON object/);
		expect(() => parseJudgeAnswer('["Emerald"]', "Emerald", "Sapphire")).toThrow(/expected a single JSON object/);
	});

	it("throws on missing winner field", () => {
		expect(() => parseJudgeAnswer(JSON.stringify({ result: "Emerald" }), "Emerald", "Sapphire")).toThrow(
			/missing or invalid "winner"/,
		);
	});

	it("does not reject stopReason inside valid judge JSON payload", () => {
		const res = parseJudgeAnswer(JSON.stringify({ winner: "Emerald", stopReason: "error" }), "Emerald", "Sapphire");
		expect(res.outcome).toBe("A");
	});

	it("throws when labels are not two distinct strings", () => {
		expect(() => parseJudgeAnswer(JSON.stringify({ winner: "Emerald" }), "Emerald", "Emerald")).toThrow(
			/distinct labels/,
		);
	});
});

describe("createModelJudge", () => {
	it("varies neutral labels across calls and never uses literal A or B", async () => {
		const recordedLabels: Array<[string, string]> = [];
		const completeStub = async (_model: Model<Api>, context: Context, options?: SimpleStreamOptions) => {
			expect(options?.disableReasoning).toBe(true);
			const promptContent = (context.messages[0]?.content as string) ?? "";

			// Verify candidate labels in prompt
			const matchA = promptContent.match(/Candidate ([A-Za-z0-9_-]+):/);
			const matchB = promptContent.match(/Candidate ([A-Za-z0-9_-]+):[\s\S]*Candidate ([A-Za-z0-9_-]+):/);
			const labelA = matchA?.[1] ?? "";
			const labelB = matchB?.[2] ?? "";
			recordedLabels.push([labelA, labelB]);

			expect(promptContent).toContain(`{"winner": "${labelA}"|"${labelB}"|"tie"`);
			expect(promptContent).not.toContain("<labelA>");
			expect(promptContent).not.toContain("<labelB>");

			// Vote for labelB
			return makeAssistantMessage(JSON.stringify({ winner: labelB }));
		};

		const judge = createModelJudge({
			id: "test-judge",
			family: "test-family",
			model: mockModel,
			seed: 12345,
			complete: completeStub,
		});

		const verdict1 = await judge.judge("hypothesis A", "hypothesis B", { question: "Which is better?" });
		expect(verdict1.outcome).toBe("B");

		const verdict2 = await judge.judge("hypothesis A", "hypothesis B", { question: "Which is better?" });
		expect(verdict2.outcome).toBe("B");

		expect(recordedLabels.length).toBe(2);
		expect(recordedLabels[0][0]).not.toBe("A");
		expect(recordedLabels[0][1]).not.toBe("B");
		expect(recordedLabels[1][0]).not.toBe("A");
		expect(recordedLabels[1][1]).not.toBe("B");
		// Seeded sequence advances across calls
		expect(recordedLabels[0]).not.toEqual(recordedLabels[1]);
	});

	it("maps tie verdict to tie and returns usage", async () => {
		const usage = makeUsage({ input: 42, output: 8, totalTokens: 50 });
		const completeStub = async () => {
			return makeAssistantMessage(JSON.stringify({ winner: "tie" }), { usage });
		};

		const judge = createModelJudge({
			id: "tie-judge",
			family: "tie-family",
			model: mockModel,
			seed: 99,
			complete: completeStub,
		});

		const verdict = await judge.judge("text1", "text2", { question: "Research Question" });
		expect(verdict.outcome).toBe("tie");
		expect(verdict.usage).toEqual(usage);
	});

	it("rejects when the model replies with an unknown label", async () => {
		const completeStub = async () => {
			return makeAssistantMessage(JSON.stringify({ winner: "UnseenAlienLabel" }));
		};

		const judge = createModelJudge({
			id: "unknown-judge",
			family: "test",
			model: mockModel,
			seed: 42,
			complete: completeStub,
		});

		await expect(judge.judge("a", "b", { question: "q" })).rejects.toThrow(/Unknown judge winner/);
	});

	it("rejects when the model replies with malformed output", async () => {
		const completeStub = async () => {
			return makeAssistantMessage("I cannot decide between these two.");
		};

		const judge = createModelJudge({
			id: "malformed-judge",
			family: "test",
			model: mockModel,
			seed: 42,
			complete: completeStub,
		});

		await expect(judge.judge("a", "b", { question: "q" })).rejects.toThrow(/Malformed judge response/);
	});

	it("rejects when complete returns stopReason error", async () => {
		const completeStub = async () => {
			return makeAssistantMessage("", { stopReason: "error", errorMessage: "Backend overloaded" });
		};

		const judge = createModelJudge({
			id: "error-judge",
			family: "test",
			model: mockModel,
			seed: 42,
			complete: completeStub,
		});

		await expect(judge.judge("a", "b", { question: "q" })).rejects.toThrow(/Backend overloaded/);
	});
});

describe("createModelSummarizer", () => {
	it("returns stub text and usage under maxChars", async () => {
		const stubUsage = makeUsage({ input: 200, output: 50, totalTokens: 250 });
		let recordedPrompt = "";

		const completeStub = async (_model: Model<Api>, context: Context, options?: SimpleStreamOptions) => {
			expect(options?.disableReasoning).toBe(true);
			recordedPrompt = (context.messages[0]?.content as string) ?? "";
			return makeAssistantMessage("Condensed summary text.", { usage: stubUsage });
		};

		const summarizer = createModelSummarizer({
			model: mockModel,
			complete: completeStub,
		});

		const result = await summarizer.summarize("Very long writeup about solar flares", 500);
		expect(result.text).toBe("Condensed summary text.");
		expect(result.usage).toEqual(stubUsage);
		expect(recordedPrompt).toContain("500");
		expect(recordedPrompt).toContain("solar flares");
	});

	it("rejects when summarizer complete returns stopReason error", async () => {
		const completeStub = async () => {
			return makeAssistantMessage("", { stopReason: "error", errorMessage: "Summarization failed" });
		};

		const summarizer = createModelSummarizer({
			model: mockModel,
			complete: completeStub,
		});

		await expect(summarizer.summarize("Long text", 200)).rejects.toThrow(/Summarization failed/);
	});
});
