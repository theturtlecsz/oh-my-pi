import { describe, expect, it } from "bun:test";
import { createSeededRng, TOURNAMENT_LABEL } from "../src/autoresearch/tournament/types";
import { addUsageTotals, createUsageTotals } from "../src/utils/usage-totals";

describe("autoresearch tournament types and RNG", () => {
	it("exposes the fixed tournament label contract", () => {
		expect(TOURNAMENT_LABEL).toBe("search aid, not a validity certificate");
	});

	it("produces identical pseudorandom sequences for identical seeds", () => {
		const seed = 12345;
		const rng1 = createSeededRng(seed);
		const rng2 = createSeededRng(seed);

		const seq1 = Array.from({ length: 20 }, () => rng1());
		const seq2 = Array.from({ length: 20 }, () => rng2());

		expect(seq1).toEqual(seq2);
	});

	it("produces diverging pseudorandom sequences for different seeds", () => {
		const rngA = createSeededRng(42);
		const rngB = createSeededRng(99);

		const seqA = Array.from({ length: 10 }, () => rngA());
		const seqB = Array.from({ length: 10 }, () => rngB());

		expect(seqA).not.toEqual(seqB);
	});

	it("constrains generated numbers to the unit interval [0, 1)", () => {
		const rng = createSeededRng(0xdeadbeef);
		for (let i = 0; i < 1000; i++) {
			const val = rng();
			expect(val).toBeGreaterThanOrEqual(0);
			expect(val).toBeLessThan(1);
		}
	});

	it("handles 32-bit boundary and unsigned seed coercion identically", () => {
		const rngSigned = createSeededRng(-1);
		const rngUnsigned = createSeededRng(0xffffffff);

		const seqSigned = Array.from({ length: 10 }, () => rngSigned());
		const seqUnsigned = Array.from({ length: 10 }, () => rngUnsigned());

		expect(seqSigned).toEqual(seqUnsigned);
	});
});

describe("usage totals utilities", () => {
	it("initializes a zeroed usage structure", () => {
		const totals = createUsageTotals();
		expect(totals).toEqual({
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		});
	});

	it("accumulates partial usage into running totals", () => {
		const totals = createUsageTotals();

		addUsageTotals(totals, {
			input: 100,
			output: 50,
			cost: { input: 0.001, output: 0.002, cacheRead: 0, cacheWrite: 0, total: 0.003 },
		});

		expect(totals.input).toBe(100);
		expect(totals.output).toBe(50);
		expect(totals.totalTokens).toBe(150);
		expect(totals.cost.total).toBeCloseTo(0.003, 6);

		addUsageTotals(totals, {
			input: 50,
			output: 25,
			cacheRead: 10,
			cacheWrite: 5,
			totalTokens: 90,
			cost: { input: 0.0005, output: 0.001, cacheRead: 0, cacheWrite: 0, total: 0.0015 },
		});

		expect(totals.input).toBe(150);
		expect(totals.output).toBe(75);
		expect(totals.cacheRead).toBe(10);
		expect(totals.cacheWrite).toBe(5);
		expect(totals.totalTokens).toBe(240);
		expect(totals.cost.total).toBeCloseTo(0.0045, 6);
	});
});
