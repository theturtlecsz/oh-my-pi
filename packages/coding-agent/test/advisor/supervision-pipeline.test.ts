import { describe, expect, it, vi } from "bun:test";
import { logger } from "@oh-my-pi/pi-utils";
import {
	ADVISOR_SUPERVISION_PATHS,
	AdvisorSupervisionPipeline as AdvisorSupervisionPipelineFromBarrel,
} from "../../src/advisor";
import { AdviseTool, type AdvisorCategory, type AdvisorSeverity } from "../../src/advisor/advise-tool";
import { AdvisorEmissionGuard } from "../../src/advisor/emission-guard";
import {
	type AdvisorSupervisionDecision,
	AdvisorSupervisionGate,
	type AdvisorSupervisionInput,
} from "../../src/advisor/supervision-gate";
import { AdvisorSupervisionPipeline } from "../../src/advisor/supervision-pipeline";

class DropSemanticConcern extends AdvisorSupervisionGate {
	override decide(input: AdvisorSupervisionInput): AdvisorSupervisionDecision {
		if (input.category === "semantic-concern") return { deliver: false, reason: "duplicate-rank" };
		return super.decide(input);
	}
}

interface Delivery {
	note: string;
	severity: AdvisorSeverity | undefined;
	category: AdvisorCategory | undefined;
	index: number | undefined;
}

type Op =
	| { type: "update"; inProgress: boolean }
	| { type: "reset" }
	| { type: "advise"; note: string; severity?: AdvisorSeverity; category: AdvisorCategory; index: number };

function toolText(result: { content: readonly { type: string; text?: string }[] }): string {
	const block = result.content[0];
	if (block?.type !== "text" || block.text === undefined) {
		throw new Error(`unexpected tool content ${JSON.stringify(result.content)}`);
	}
	return block.text;
}

function pushDelivery(
	delivered: Delivery[],
	note: string,
	severity?: AdvisorSeverity,
	category?: AdvisorCategory,
	index?: number,
): void {
	delivered.push({ note, severity, category, index });
}

/** The pre-gate pair: AdviseTool rank map, then AdvisorEmissionGuard. */
async function runLegacy(ops: readonly Op[]): Promise<{ delivered: Delivery[]; texts: string[] }> {
	const guard = new AdvisorEmissionGuard();
	const delivered: Delivery[] = [];
	const texts: string[] = [];
	let cursor = -1;
	const tool = new AdviseTool(
		(note, severity, category, index) => {
			if (guard.accept(note)) pushDelivery(delivered, note, severity, category, index);
		},
		{ transcriptIndex: () => cursor },
	);
	for (const op of ops) {
		if (op.type === "update") {
			tool.beginUpdate(op.inProgress);
			guard.beginUpdate();
		} else if (op.type === "reset") {
			tool.resetDeliveredNotes();
			guard.reset();
		} else {
			cursor = op.index;
			const result = await tool.execute("tc", { note: op.note, severity: op.severity, category: op.category });
			texts.push(toolText(result));
		}
	}
	return { delivered, texts };
}

const EQUIVALENCE_OPS: readonly Op[] = [
	{ type: "advise", note: "   ", severity: "concern", category: "possible-false-positive", index: 1 },
	{ type: "advise", note: "Stop.", severity: "blocker", category: "gate-defect", index: 2 },
	{
		type: "advise",
		note: "Move retries into the queue, not the request path.",
		severity: "nit",
		category: "semantic-concern",
		index: 3,
	},
	{
		type: "advise",
		note: "Second concern: wrong env var name.",
		severity: "concern",
		category: "model-procedure-miss",
		index: 4,
	},
	{ type: "update", inProgress: true },
	{
		type: "advise",
		note: "Deferred concrete concern about the queue.",
		severity: "concern",
		category: "semantic-concern",
		index: 5,
	},
	{
		type: "advise",
		note: "Blocker: a destructive command is running.",
		severity: "blocker",
		category: "gate-defect",
		index: 6,
	},
	{ type: "update", inProgress: false },
	{ type: "reset" },
	{ type: "advise", note: "Stop.", severity: "nit", category: "semantic-concern", index: 7 },
	{
		type: "advise",
		note: "Check the retry queue bounds.",
		severity: "nit",
		category: "semantic-concern",
		index: 8,
	},
	{
		type: "advise",
		note: "Check the retry queue bounds.",
		severity: "blocker",
		category: "gate-defect",
		index: 9,
	},
	{
		type: "advise",
		note: "check the retry queue bounds!",
		severity: "nit",
		category: "policy-ambiguity",
		index: 10,
	},
];

describe("AdvisorSupervisionPipeline", () => {
	it("is star-exported and names the advise tool", () => {
		expect(AdvisorSupervisionPipelineFromBarrel).toBe(AdvisorSupervisionPipeline);
		expect(ADVISOR_SUPERVISION_PATHS).toEqual(["legacy", "shadow", "canary", "structured"]);
		const pipeline = new AdvisorSupervisionPipeline({
			path: "legacy",
			canaryMaxDivergences: 0,
			transcriptIndex: () => 0,
			deliver: () => {},
		});
		expect(pipeline.tool.name).toBe("advise");
	});

	it("runs only the legacy arm, and deliver finishes before execute resolves", async () => {
		const delivered: Delivery[] = [];
		let cursor = 2;
		let resolvedBeforeDeliver = false;
		const pipeline = new AdvisorSupervisionPipeline({
			path: "legacy",
			canaryMaxDivergences: 3,
			transcriptIndex: () => cursor,
			deliver: (note, severity, category, index) => pushDelivery(delivered, note, severity, category, index),
		});

		const pending = pipeline.tool.execute("tc-1", {
			note: "Check the retry queue bounds.",
			severity: "nit",
			category: "semantic-concern",
		});
		void pending.then(() => {
			if (delivered.length === 0) resolvedBeforeDeliver = true;
		});
		const recorded = await pending;
		expect(toolText(recorded)).toBe("Recorded.");
		expect(resolvedBeforeDeliver).toBe(false);
		expect(delivered).toEqual([
			{
				note: "Check the retry queue bounds.",
				severity: "nit",
				category: "semantic-concern",
				index: 2,
			},
		]);

		const stop = await pipeline.tool.execute("tc-2", {
			note: "Stop.",
			severity: "blocker",
			category: "gate-defect",
		});
		expect(toolText(stop)).toBe("Recorded.");
		expect(
			toolText(
				await pipeline.tool.execute("tc-3", {
					note: "   ",
					severity: "concern",
					category: "possible-false-positive",
				}),
			),
		).toBe("Recorded.");
		expect(delivered).toHaveLength(1);

		delivered.length = 0;
		pipeline.beginUpdate(true);
		cursor = 9;
		const deferred = await pipeline.tool.execute("tc-4", {
			note: "Deferred concrete concern about retries.",
			severity: "nit",
			category: "semantic-concern",
		});
		expect(toolText(deferred)).toContain("Deferred");
		expect(delivered).toEqual([]);
		cursor = 40;
		pipeline.beginUpdate(false);
		expect(delivered).toEqual([
			{
				note: "Deferred concrete concern about retries.",
				severity: "nit",
				category: "semantic-concern",
				index: 9,
			},
		]);

		expect(pipeline.report()).toEqual({
			path: "legacy",
			authority: "legacy",
			divergences: 0,
			canaryBudgetExceeded: false,
			invocations: { legacy: 4, structured: 0 },
			classes: {},
		});
	});

	it("runs only the structured arm", async () => {
		const gate = new AdvisorSupervisionGate();
		const delivered: Delivery[] = [];
		const pipeline = new AdvisorSupervisionPipeline({
			path: "structured",
			canaryMaxDivergences: 0,
			transcriptIndex: () => 1,
			structuredGate: gate,
			deliver: (note, severity, category, index) => pushDelivery(delivered, note, severity, category, index),
		});
		const note = "Check the retry queue bounds.";
		await pipeline.tool.execute("tc-1", { note, severity: "nit", category: "semantic-concern" });
		await pipeline.tool.execute("tc-2", { note: "Stop.", severity: "blocker", category: "gate-defect" });
		expect(delivered).toEqual([{ note, severity: "nit", category: "semantic-concern", index: 1 }]);
		expect(pipeline.report().invocations).toEqual({ legacy: 0, structured: 2 });
		expect(pipeline.report().authority).toBe("structured");
		expect(pipeline.report().divergences).toBe(0);
		expect(pipeline.report().classes).toEqual(gate.report());
	});

	it("on shadow, a gate that drops a class still delivers the legacy buffer", async () => {
		const delivered: Delivery[] = [];
		let cursor = -1;
		const pipeline = new AdvisorSupervisionPipeline({
			path: "shadow",
			canaryMaxDivergences: 0,
			transcriptIndex: () => cursor,
			structuredGate: new DropSemanticConcern(),
			deliver: (note, severity, category, index) => pushDelivery(delivered, note, severity, category, index),
		});
		const warnSpy = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			const texts: string[] = [];
			for (const op of EQUIVALENCE_OPS) {
				if (op.type === "update") pipeline.beginUpdate(op.inProgress);
				else if (op.type === "reset") pipeline.reset();
				else {
					cursor = op.index;
					const result = await pipeline.tool.execute("tc", {
						note: op.note,
						severity: op.severity,
						category: op.category,
					});
					texts.push(toolText(result));
				}
			}
			const legacy = await runLegacy(EQUIVALENCE_OPS);
			expect(delivered).toEqual(legacy.delivered);
			expect(texts).toEqual(legacy.texts);
			expect(pipeline.report().divergences).toBeGreaterThan(0);
			expect(pipeline.report().authority).toBe("legacy");
			expect(pipeline.report().canaryBudgetExceeded).toBe(false);
			expect(pipeline.report().invocations).toEqual({ legacy: 10, structured: 10 });
			expect(warnSpy).not.toHaveBeenCalledWith("canary_budget_exceeded");
		} finally {
			warnSpy.mockRestore();
		}
	});

	it("spends a zero canary budget on the first divergent call and stays on legacy", async () => {
		const delivered: Delivery[] = [];
		let cursor = 1;
		const pipeline = new AdvisorSupervisionPipeline({
			path: "canary",
			canaryMaxDivergences: 0,
			transcriptIndex: () => cursor,
			structuredGate: new DropSemanticConcern(),
			deliver: (note, severity, category, index) => pushDelivery(delivered, note, severity, category, index),
		});
		const warnSpy = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			expect(pipeline.report().authority).toBe("structured");
			const first = await pipeline.tool.execute("tc-1", {
				note: "Check the retry queue bounds.",
				severity: "nit",
				category: "semantic-concern",
			});
			expect(toolText(first)).toBe("Recorded.");
			expect(delivered).toEqual([
				{
					note: "Check the retry queue bounds.",
					severity: "nit",
					category: "semantic-concern",
					index: 1,
				},
			]);
			expect(pipeline.report().divergences).toBe(1);
			expect(pipeline.report().canaryBudgetExceeded).toBe(true);
			expect(pipeline.report().authority).toBe("legacy");
			expect(warnSpy).toHaveBeenCalledTimes(1);
			expect(warnSpy).toHaveBeenCalledWith("canary_budget_exceeded");

			cursor = 2;
			const second = await pipeline.tool.execute("tc-2", {
				note: "Second concern: wrong env var name.",
				severity: "concern",
				category: "semantic-concern",
			});
			expect(toolText(second)).toBe("Recorded.");
			expect(delivered).toHaveLength(1);

			pipeline.beginUpdate(false);
			cursor = 3;
			const third = await pipeline.tool.execute("tc-3", {
				note: "Concrete: read race in #handleRetry.",
				severity: "concern",
				category: "semantic-concern",
			});
			expect(toolText(third)).toBe("Recorded.");
			expect(delivered).toEqual([
				{
					note: "Check the retry queue bounds.",
					severity: "nit",
					category: "semantic-concern",
					index: 1,
				},
				{
					note: "Concrete: read race in #handleRetry.",
					severity: "concern",
					category: "semantic-concern",
					index: 3,
				},
			]);

			const divergences = pipeline.report().divergences;
			const invocations = pipeline.report().invocations;
			pipeline.reset();
			expect(pipeline.report().path).toBe("canary");
			expect(pipeline.report().authority).toBe("legacy");
			expect(pipeline.report().canaryBudgetExceeded).toBe(true);
			expect(pipeline.report().divergences).toBe(divergences);
			expect(pipeline.report().invocations).toEqual(invocations);
			expect(warnSpy).toHaveBeenCalledTimes(1);

			cursor = 4;
			const fourth = await pipeline.tool.execute("tc-4", {
				note: "Move retries into the queue, not the request path.",
				severity: "blocker",
				category: "semantic-concern",
			});
			expect(toolText(fourth)).toBe("Recorded.");
			expect(delivered.at(-1)).toEqual({
				note: "Move retries into the queue, not the request path.",
				severity: "blocker",
				category: "semantic-concern",
				index: 4,
			});
			expect(pipeline.report().authority).toBe("legacy");
			expect(warnSpy).toHaveBeenCalledTimes(1);
		} finally {
			warnSpy.mockRestore();
		}
	});

	it("keeps structured authority until divergences exceed the canary max", async () => {
		const delivered: Delivery[] = [];
		let cursor = 1;
		const pipeline = new AdvisorSupervisionPipeline({
			path: "canary",
			canaryMaxDivergences: 1,
			transcriptIndex: () => cursor,
			structuredGate: new DropSemanticConcern(),
			deliver: (note, severity, category, index) => pushDelivery(delivered, note, severity, category, index),
		});
		const warnSpy = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			const first = await pipeline.tool.execute("tc-1", {
				note: "Check the retry queue bounds.",
				severity: "nit",
				category: "semantic-concern",
			});
			expect(toolText(first)).toBe("Duplicate advice ignored.");
			expect(delivered).toEqual([]);
			expect(pipeline.report().divergences).toBe(1);
			expect(pipeline.report().canaryBudgetExceeded).toBe(false);
			expect(pipeline.report().authority).toBe("structured");
			expect(warnSpy).not.toHaveBeenCalled();

			pipeline.beginUpdate(false);
			cursor = 2;
			const second = await pipeline.tool.execute("tc-2", {
				note: "Concrete: read race in #handleRetry.",
				severity: "concern",
				category: "semantic-concern",
			});
			expect(toolText(second)).toBe("Recorded.");
			expect(delivered).toEqual([
				{
					note: "Concrete: read race in #handleRetry.",
					severity: "concern",
					category: "semantic-concern",
					index: 2,
				},
			]);
			expect(pipeline.report().divergences).toBe(2);
			expect(pipeline.report().canaryBudgetExceeded).toBe(true);
			expect(pipeline.report().authority).toBe("legacy");
			expect(warnSpy).toHaveBeenCalledTimes(1);
			expect(warnSpy).toHaveBeenCalledWith("canary_budget_exceeded");
		} finally {
			warnSpy.mockRestore();
		}
	});

	it("counts a divergent flush against the canary budget before beginUpdate returns", async () => {
		const delivered: Delivery[] = [];
		let cursor = 8;
		const pipeline = new AdvisorSupervisionPipeline({
			path: "canary",
			canaryMaxDivergences: 0,
			transcriptIndex: () => cursor,
			structuredGate: new DropSemanticConcern(),
			deliver: (note, severity, category, index) => pushDelivery(delivered, note, severity, category, index),
		});
		const warnSpy = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			pipeline.beginUpdate(true);
			cursor = 8;
			const deferred = await pipeline.tool.execute("tc-1", {
				note: "Deferred concrete concern about retries.",
				severity: "nit",
				category: "semantic-concern",
			});
			expect(toolText(deferred)).toContain("Deferred");
			expect(delivered).toEqual([]);
			expect(pipeline.report().divergences).toBe(0);
			expect(pipeline.report().authority).toBe("structured");
			cursor = 40;
			pipeline.beginUpdate(false);
			expect(delivered).toEqual([
				{
					note: "Deferred concrete concern about retries.",
					severity: "nit",
					category: "semantic-concern",
					index: 8,
				},
			]);
			expect(pipeline.report().canaryBudgetExceeded).toBe(true);
			expect(pipeline.report().authority).toBe("legacy");
			expect(pipeline.report().invocations).toEqual({ legacy: 1, structured: 1 });
			expect(warnSpy).toHaveBeenCalledWith("canary_budget_exceeded");
		} finally {
			warnSpy.mockRestore();
		}
	});

	it("matches legacy deliveries with the default gate, including blank notes and Stop.", async () => {
		const gate = new AdvisorSupervisionGate();
		const delivered: Delivery[] = [];
		let cursor = -1;
		const pipeline = new AdvisorSupervisionPipeline({
			path: "canary",
			canaryMaxDivergences: 0,
			transcriptIndex: () => cursor,
			structuredGate: gate,
			deliver: (note, severity, category, index) => pushDelivery(delivered, note, severity, category, index),
		});
		const warnSpy = vi.spyOn(logger, "warn").mockImplementation(() => {});
		try {
			const texts: string[] = [];
			for (const op of EQUIVALENCE_OPS) {
				if (op.type === "update") pipeline.beginUpdate(op.inProgress);
				else if (op.type === "reset") pipeline.reset();
				else {
					cursor = op.index;
					const result = await pipeline.tool.execute("tc", {
						note: op.note,
						severity: op.severity,
						category: op.category,
					});
					texts.push(toolText(result));
				}
			}
			const legacy = await runLegacy(EQUIVALENCE_OPS);
			expect(delivered).toEqual(legacy.delivered);
			expect(texts).toEqual(legacy.texts);
			expect(legacy.delivered.length).toBeGreaterThan(0);
			expect(pipeline.report().divergences).toBe(0);
			expect(pipeline.report().canaryBudgetExceeded).toBe(false);
			expect(pipeline.report().authority).toBe("structured");
			expect(pipeline.report().classes).toEqual(gate.report());
			expect(warnSpy).not.toHaveBeenCalledWith("canary_budget_exceeded");
		} finally {
			warnSpy.mockRestore();
		}
	});
});
