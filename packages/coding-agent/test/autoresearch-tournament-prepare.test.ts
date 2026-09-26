import { describe, expect, it } from "bun:test";
import type { Usage } from "@oh-my-pi/pi-ai";
import {
	type HypothesisInput,
	prepareHypotheses,
	redactIdentity,
	type Summarizer,
} from "../src/autoresearch/tournament/prepare";

function makeUsage(overrides: Partial<Usage> = {}): Usage {
	return {
		input: 0,
		output: 0,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 0,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		...overrides,
	};
}

function input(id: string, title: string, writeUp: string): HypothesisInput {
	return { id, title, writeUp };
}

describe("redactIdentity", () => {
	it("replaces every case-insensitive occurrence of the id and title", () => {
		const text = "Hypothesis H-42 titled Cache Warmup. h-42 again, CACHE WARMUP again.";
		const redacted = redactIdentity(text, { id: "H-42", title: "Cache Warmup" });
		expect(redacted).not.toContain("H-42");
		expect(redacted).not.toContain("h-42");
		expect(redacted).not.toContain("Cache Warmup");
		expect(redacted).not.toContain("CACHE WARMUP");
		expect(redacted).toBe("Hypothesis [redacted] titled [redacted]. [redacted] again, [redacted] again.");
	});

	it("treats regex metacharacters in the identity literally", () => {
		const text = "See a.b (v2) and aXb (v2).";
		const redacted = redactIdentity(text, { id: "a.b", title: "(v2)" });
		expect(redacted).toBe("See [redacted] [redacted] and aXb [redacted].");
	});
});

describe("prepareHypotheses", () => {
	it("redacts a write-up that contains its own id and title", async () => {
		const writeUp = "The H-7 Cache Warmup idea: H-7 warms the cache. Cache Warmup is cheap.";
		const { hypotheses } = await prepareHypotheses(
			[input("H-7", "Cache Warmup", writeUp), input("H-8", "Other", "short")],
			{ maxJudgeChars: 10_000 },
		);
		const first = hypotheses[0];
		expect(first.judgeText).not.toContain("H-7");
		expect(first.judgeText).not.toContain("Cache Warmup");
		expect(first.summarized).toBe(false);
		expect(first.writeUp).toBe(writeUp);
	});

	it("summarizes a long write-up, keeps the full write-up, and sums usage", async () => {
		const writeUp = "x".repeat(10_000);
		const stubUsage = makeUsage({ input: 12, output: 3, totalTokens: 15 });
		const summarizer: Summarizer = {
			async summarize(text, maxChars) {
				expect(text).toBe(writeUp);
				expect(maxChars).toBe(2000);
				return { text: "condensed summary", usage: stubUsage };
			},
		};

		const { hypotheses, usage } = await prepareHypotheses(
			[input("H-1", "First", writeUp), input("H-2", "Second", "short")],
			{ maxJudgeChars: 2000, summarizer },
		);

		const first = hypotheses[0];
		expect(first.summarized).toBe(true);
		expect(first.judgeText).toBe("condensed summary");
		expect(first.writeUp).toBe(writeUp);
		expect(usage.input).toBe(12);
		expect(usage.output).toBe(3);
		expect(usage.totalTokens).toBe(15);
	});

	it("redacts the summary text", async () => {
		const writeUp = "y".repeat(5000);
		const summarizer: Summarizer = {
			async summarize() {
				return { text: "H-9 Cache Warmup summary" };
			},
		};
		const { hypotheses } = await prepareHypotheses(
			[input("H-9", "Cache Warmup", writeUp), input("H-10", "Other", "short")],
			{ maxJudgeChars: 2000, summarizer },
		);
		expect(hypotheses[0].judgeText).toBe("[redacted] [redacted] summary");
	});

	it("rejects a long write-up without a summarizer, naming the id", async () => {
		const writeUp = "z".repeat(10_000);
		await expect(
			prepareHypotheses([input("H-3", "Third", writeUp), input("H-4", "Fourth", "short")], {
				maxJudgeChars: 2000,
			}),
		).rejects.toThrow(/H-3/);
	});

	it("rejects when the summarizer returns text that is still too long, naming the id", async () => {
		const writeUp = "w".repeat(10_000);
		const summarizer: Summarizer = {
			async summarize() {
				return { text: "q".repeat(2001) };
			},
		};
		await expect(
			prepareHypotheses([input("H-5", "Fifth", writeUp), input("H-6", "Sixth", "short")], {
				maxJudgeChars: 2000,
				summarizer,
			}),
		).rejects.toThrow(/H-5/);
	});

	it("rejects duplicate ids", async () => {
		await expect(
			prepareHypotheses([input("dup", "A", "one"), input("dup", "B", "two")], { maxJudgeChars: 100 }),
		).rejects.toThrow(/dup/);
	});

	it("rejects empty id, title, or write-up", async () => {
		await expect(
			prepareHypotheses([input("", "A", "one"), input("b", "B", "two")], { maxJudgeChars: 100 }),
		).rejects.toThrow();
		await expect(
			prepareHypotheses([input("a", "", "one"), input("b", "B", "two")], { maxJudgeChars: 100 }),
		).rejects.toThrow();
		await expect(
			prepareHypotheses([input("a", "A", ""), input("b", "B", "two")], { maxJudgeChars: 100 }),
		).rejects.toThrow();
	});

	it("rejects fewer than two inputs", async () => {
		await expect(prepareHypotheses([input("a", "A", "one")], { maxJudgeChars: 100 })).rejects.toThrow();
		await expect(prepareHypotheses([], { maxJudgeChars: 100 })).rejects.toThrow();
	});

	it("never drops a hypothesis and preserves order", async () => {
		const summarizer: Summarizer = {
			async summarize() {
				return { text: "s" };
			},
		};
		const { hypotheses } = await prepareHypotheses(
			[input("a", "A", "short"), input("b", "B", "b".repeat(5000)), input("c", "C", "short")],
			{ maxJudgeChars: 100, summarizer },
		);
		expect(hypotheses.map(h => h.id)).toEqual(["a", "b", "c"]);
		expect(hypotheses[1].summarized).toBe(true);
	});
});
