import { describe, expect, it } from "bun:test";
import { AdvisorSupervisionGate as AdvisorSupervisionGateFromBarrel } from "../../src/advisor";
import {
	type AdviseParams,
	AdviseTool,
	type AdvisorCategory,
	type AdvisorSeverity,
} from "../../src/advisor/advise-tool";
import { AdvisorEmissionGuard } from "../../src/advisor/emission-guard";
import {
	type AdvisorSupervisionDecision,
	AdvisorSupervisionGate,
	type AdvisorSupervisionReport,
} from "../../src/advisor/supervision-gate";
import { isCpkSupervisionRuleClass } from "../../src/extensibility/cpk/supervision-proposal";

const CATEGORIES = [
	"gate-defect",
	"model-procedure-miss",
	"semantic-concern",
	"policy-ambiguity",
	"possible-false-positive",
] as const satisfies readonly AdvisorCategory[];

const SEVERITIES: readonly (AdvisorSeverity | undefined)[] = [undefined, "nit", "concern", "blocker"];

/** Surface forms the random sequences must keep feeding both pipelines. */
const NOTES = [
	"",
	"   ",
	"Stop.",
	"stop",
	"STOP!",
	"*Stop*",
	"LGTM",
	"lgtm.",
	"Looks good.",
	"Done.",
	"done!",
	"No issue; continue.",
	"Move retries into the queue, not the request path.",
	"move retries into the queue, not the request path",
	"Move retries into the queue, not the request path!",
	"First concern: missing await in #handleRetry.",
	"Second concern: wrong env var name.",
	"Concrete: read race in #handleRetry.",
	"Same point raised repeatedly.",
	"Same   point raised repeatedly.",
	"...",
	"Ok.",
	"Halt.",
] as const;

type Op =
	| { type: "update"; inProgress: boolean }
	| { type: "reset" }
	| { type: "advise"; note: string; severity?: AdvisorSeverity; category: AdvisorCategory; index: number };

interface Delivery {
	note: string;
	severity: AdvisorSeverity | undefined;
	category: AdvisorCategory;
	index: number;
}

function toolText(result: { content: readonly { type: string; text?: string }[] }): string {
	const block = result.content[0];
	if (block?.type !== "text" || block.text === undefined) {
		throw new Error(`unexpected tool content ${JSON.stringify(result.content)}`);
	}
	return block.text;
}

function foldReport(
	seen: readonly { category?: string; decision: AdvisorSupervisionDecision }[],
): AdvisorSupervisionReport {
	const report: AdvisorSupervisionReport = {};
	for (const { category, decision } of seen) {
		const key = isCpkSupervisionRuleClass(category) ? category : "unclassified";
		const stats = report[key] ?? { proposed: 0, delivered: 0, suppressed: {} };
		stats.proposed += 1;
		if (decision.deliver) stats.delivered += 1;
		else if (decision.reason !== "delivered") {
			stats.suppressed[decision.reason] = (stats.suppressed[decision.reason] ?? 0) + 1;
		}
		report[key] = stats;
	}
	return report;
}

function mulberry32(seed: number): () => number {
	let state = seed >>> 0;
	return () => {
		state = (state + 0x6d2b79f5) >>> 0;
		let t = Math.imul(state ^ (state >>> 15), 1 | state);
		t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
}

function makeOps(rng: () => number): Op[] {
	const ops: Op[] = [];
	let index = 0;
	const advise = () => {
		index += 1;
		const severity = SEVERITIES[Math.floor(rng() * SEVERITIES.length)];
		ops.push({
			type: "advise",
			note: NOTES[Math.floor(rng() * NOTES.length)] ?? "",
			severity,
			category: CATEGORIES[Math.floor(rng() * CATEGORIES.length)] ?? "semantic-concern",
			index,
		});
	};
	ops.push({ type: "update", inProgress: true });
	const duringWip = 1 + Math.floor(rng() * 4);
	for (let i = 0; i < duringWip; i++) advise();
	ops.push({ type: "update", inProgress: false });
	ops.push({ type: "reset" });
	const rest = 12 + Math.floor(rng() * 20);
	for (let i = 0; i < rest; i++) {
		const roll = rng();
		if (roll < 0.1) ops.push({ type: "reset" });
		else if (roll < 0.28) ops.push({ type: "update", inProgress: rng() < 0.5 });
		else advise();
	}
	ops.push({ type: "update", inProgress: false });
	return ops;
}

async function runLegacy(ops: readonly Op[]): Promise<{ delivered: Delivery[]; texts: string[] }> {
	const guard = new AdvisorEmissionGuard();
	const delivered: Delivery[] = [];
	const texts: string[] = [];
	let cursor = -1;
	const tool = new AdviseTool(
		(note, severity, category, index) => {
			if (category === undefined || index === undefined) throw new Error("legacy callback missing fields");
			if (guard.accept(note)) delivered.push({ note, severity, category, index });
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

async function runGated(ops: readonly Op[]): Promise<{ delivered: Delivery[]; texts: string[] }> {
	const gate = new AdvisorSupervisionGate();
	const delivered: Delivery[] = [];
	const texts: string[] = [];
	let cursor = -1;
	const tool = new AdviseTool(
		(note, severity, category, index) => {
			if (category === undefined || index === undefined) throw new Error("gated callback missing fields");
			delivered.push({ note, severity, category, index });
		},
		{ gate, transcriptIndex: () => cursor },
	);
	for (const op of ops) {
		if (op.type === "update") tool.beginUpdate(op.inProgress);
		else if (op.type === "reset") tool.resetDeliveredNotes();
		else {
			cursor = op.index;
			const result = await tool.execute("tc", { note: op.note, severity: op.severity, category: op.category });
			texts.push(toolText(result));
		}
	}
	return { delivered, texts };
}

describe("AdvisorEmissionGuard.classify", () => {
	it("uses accept's order, and accept is classify === accepted", () => {
		const viaAccept = new AdvisorEmissionGuard();
		const viaClassify = new AdvisorEmissionGuard();
		const notes = [
			"",
			"   ",
			"Stop.",
			"LGTM",
			"Done.",
			"No issue; continue.",
			"Move retries into the queue, not the request path.",
			"move retries into the queue, not the request path",
			"Second concrete concern about env vars.",
		];
		for (const note of notes) {
			const verdict = viaClassify.classify(note);
			expect(viaAccept.accept(note)).toBe(verdict === "accepted");
		}
		// "Stop." after the budget is spent is still noise: filler is checked first.
		expect(viaClassify.classify("Stop.")).toBe("noise");
		viaClassify.beginUpdate();
		// Exact repeat of an accepted note in the next update is duplicate, and
		// does not spend the new budget.
		expect(viaClassify.classify("Move retries into the queue, not the request path.")).toBe("duplicate");
		expect(viaClassify.classify("Second concrete concern about env vars.")).toBe("accepted");
		expect(viaClassify.classify("Third concrete concern about the lock.")).toBe("budget");
	});
});

describe("AdvisorSupervisionGate", () => {
	it("is star-exported from the advisor barrel", () => {
		expect(AdvisorSupervisionGateFromBarrel).toBe(AdvisorSupervisionGate);
	});

	it("drops a whitespace final-update note as noise and a missing category as invalid", async () => {
		const gate = new AdvisorSupervisionGate();
		const delivered: unknown[] = [];
		const tool = new AdviseTool(
			(note, severity, category, index) => delivered.push({ note, severity, category, index }),
			{
				gate,
				transcriptIndex: () => 7,
			},
		);
		tool.beginUpdate(false);
		const blank = await tool.execute("tc-blank", {
			note: "   ",
			severity: "concern",
			category: "possible-false-positive",
		});
		expect(toolText(blank)).toBe("Recorded.");
		expect(blank.details).toEqual({ note: "   ", severity: "concern", category: "possible-false-positive" });
		expect(delivered).toEqual([]);

		const missing = await tool.execute("tc-missing", {
			note: "Concrete advice about retries.",
			severity: "concern",
		} as AdviseParams);
		expect(toolText(missing)).toBe("Recorded.");
		expect(delivered).toEqual([]);

		const direct = new AdvisorSupervisionGate();
		expect(
			direct.decide({ note: "   ", severity: "nit", category: "possible-false-positive", transcriptIndex: 4 }),
		).toEqual({ deliver: false, reason: "noise" });
		expect(() => direct.decide({ note: "Concrete advice about retries.", transcriptIndex: 5 })).not.toThrow();
		expect(direct.decide({ note: "Concrete advice about the queue.", transcriptIndex: 6 })).toEqual({
			deliver: false,
			reason: "invalid",
		});
		expect(direct.report()).toEqual({
			"possible-false-positive": { proposed: 1, delivered: 0, suppressed: { noise: 1 } },
			unclassified: { proposed: 2, delivered: 0, suppressed: { invalid: 2 } },
		});
		expect(gate.report()).toEqual({
			"possible-false-positive": { proposed: 1, delivered: 0, suppressed: { noise: 1 } },
			unclassified: { proposed: 1, delivered: 0, suppressed: { invalid: 1 } },
		});
	});

	it("classifies Stop after a spent budget as noise, a later normalized repeat as duplicate, and the second new note as budget", () => {
		const gate = new AdvisorSupervisionGate();
		const seen: { category?: string; decision: AdvisorSupervisionDecision }[] = [];
		const take = (category: string | undefined, decision: AdvisorSupervisionDecision) => {
			seen.push({ category, decision });
			return decision;
		};

		gate.beginUpdate();
		const delivered = take(
			"semantic-concern",
			gate.decide({
				note: "Move retries into the queue, not the request path.",
				severity: "nit",
				category: "semantic-concern",
				transcriptIndex: 0,
			}),
		);
		expect(delivered).toEqual({
			deliver: true,
			reason: "delivered",
			proposal: {
				schema: "cpk6/v1",
				ruleClass: "semantic-concern",
				severity: "nit",
				transcriptIndex: 0,
				claim: "Move retries into the queue, not the request path.",
			},
		});
		expect(
			take(
				"gate-defect",
				gate.decide({ note: "Stop.", severity: "blocker", category: "gate-defect", transcriptIndex: 1 }),
			),
		).toEqual({ deliver: false, reason: "noise" });
		expect(
			take(
				"model-procedure-miss",
				gate.decide({
					note: "Second concern: wrong env var name.",
					severity: "concern",
					category: "model-procedure-miss",
					transcriptIndex: 2,
				}),
			),
		).toEqual({ deliver: false, reason: "budget" });

		gate.beginUpdate();
		expect(gate.report()).toEqual(foldReport(seen));
		expect(
			take(
				"semantic-concern",
				gate.decide({
					note: "move retries into the queue, not the request path!",
					severity: "blocker",
					category: "semantic-concern",
					transcriptIndex: 3,
				}),
			),
		).toEqual({ deliver: false, reason: "duplicate" });
		// Same whitespace key at the same rank never reaches the emission guard.
		expect(
			take(
				"semantic-concern",
				gate.decide({
					note: "Move retries into the queue, not the request path.",
					severity: "nit",
					category: "semantic-concern",
					transcriptIndex: 4,
				}),
			),
		).toEqual({ deliver: false, reason: "duplicate-rank" });

		const snap = gate.report();
		expect(snap).toEqual(foldReport(seen));
		const semantic = snap["semantic-concern"];
		if (!semantic) throw new Error("missing semantic-concern tally");
		semantic.proposed = 0;
		semantic.suppressed.duplicate = 0;
		expect(gate.report()).toEqual(foldReport(seen));

		gate.reset();
		expect(gate.report()).toEqual({});
		expect(
			gate.decide({
				note: "Move retries into the queue, not the request path.",
				severity: "nit",
				category: "semantic-concern",
				transcriptIndex: 5,
			}).reason,
		).toBe("delivered");
	});

	it("records rank even when a later stage suppresses, and does not let that suppression spend the budget", () => {
		const gate = new AdvisorSupervisionGate();
		expect(
			gate.decide({ note: "Stop.", severity: "nit", category: "semantic-concern", transcriptIndex: 0 }).reason,
		).toBe("noise");
		expect(
			gate.decide({ note: "Stop.", severity: "nit", category: "semantic-concern", transcriptIndex: 1 }).reason,
		).toBe("duplicate-rank");
		expect(
			gate.decide({ note: "Stop.", severity: "blocker", category: "semantic-concern", transcriptIndex: 2 }).reason,
		).toBe("noise");
		expect(
			gate.decide({ note: "LGTM", severity: "nit", category: "policy-ambiguity", transcriptIndex: 3 }).reason,
		).toBe("noise");
		const invalid = gate.decide({
			note: "Concrete advice about retries.",
			category: "not-a-class",
			transcriptIndex: Number.NaN,
		});
		expect(invalid.reason).toBe("invalid");
		expect(invalid.proposal).toBeUndefined();
		// Same rank was recorded on the invalid parse, so a valid retag at nit is duplicate-rank.
		expect(
			gate.decide({
				note: "Concrete advice about retries.",
				severity: "nit",
				category: "semantic-concern",
				transcriptIndex: 5,
			}).reason,
		).toBe("duplicate-rank");
		const delivered = gate.decide({
			note: "Other concrete advice about the lock.",
			severity: "concern",
			category: "gate-defect",
			transcriptIndex: 6,
		});
		expect(delivered.reason).toBe("delivered");
		expect(delivered.proposal?.severity).toBe("concern");
		expect(delivered.proposal?.transcriptIndex).toBe(6);
		expect(gate.report().unclassified).toEqual({ proposed: 1, delivered: 0, suppressed: { invalid: 1 } });
	});

	it("says Duplicate advice ignored only for duplicate-rank and keeps a deferred index", async () => {
		const gate = new AdvisorSupervisionGate();
		const delivered: Delivery[] = [];
		let cursor = 0;
		const tool = new AdviseTool(
			(note, severity, category, index) => {
				if (category === undefined || index === undefined) throw new Error("missing callback fields");
				delivered.push({ note, severity, category, index });
			},
			{ gate, transcriptIndex: () => cursor },
		);

		tool.beginUpdate(false);
		const first = await tool.execute("tc-1", {
			note: "Check the retry queue bounds.",
			severity: "nit",
			category: "semantic-concern",
		});
		const stop = await tool.execute("tc-2", { note: "Stop.", severity: "blocker", category: "gate-defect" });
		const second = await tool.execute("tc-3", {
			note: "Check the env var name next.",
			severity: "nit",
			category: "model-procedure-miss",
		});
		const stopAgain = await tool.execute("tc-4", { note: "Stop.", severity: "blocker", category: "gate-defect" });
		expect(toolText(first)).toBe("Recorded.");
		expect(toolText(stop)).toBe("Recorded.");
		expect(toolText(second)).toBe("Recorded.");
		expect(toolText(stopAgain)).toBe("Duplicate advice ignored.");
		expect(delivered).toEqual([
			{ note: "Check the retry queue bounds.", severity: "nit", category: "semantic-concern", index: 0 },
		]);

		delivered.length = 0;
		tool.resetDeliveredNotes();
		tool.beginUpdate(true);
		cursor = 5;
		const deferred = await tool.execute("tc-5", {
			note: "Deferred concrete concern about retries.",
			severity: "nit",
			category: "semantic-concern",
		});
		expect(toolText(deferred)).toContain("Deferred");
		cursor = 6;
		await tool.execute("tc-6", {
			note: "Deferred concrete concern about retries.",
			severity: "nit",
			category: "policy-ambiguity",
		});
		cursor = 11;
		await tool.execute("tc-7", {
			note: "Deferred concrete concern about retries.",
			severity: "concern",
			category: "gate-defect",
		});
		cursor = 40;
		expect(delivered).toEqual([]);
		tool.beginUpdate(false);
		expect(delivered).toEqual([
			{
				note: "Deferred concrete concern about retries.",
				severity: "concern",
				category: "gate-defect",
				index: 11,
			},
		]);
	});

	it("flushes deferred notes before reopening the budget", async () => {
		const gate = new AdvisorSupervisionGate();
		const notes: string[] = [];
		const tool = new AdviseTool(note => notes.push(note), { gate, transcriptIndex: () => 1 });
		tool.beginUpdate(true);
		await tool.execute("tc-1", {
			note: "Deferred concrete concern about the queue.",
			severity: "concern",
			category: "semantic-concern",
		});
		await tool.execute("tc-2", {
			note: "Blocker: a destructive command is running.",
			severity: "blocker",
			category: "gate-defect",
		});
		expect(notes).toEqual(["Blocker: a destructive command is running."]);
		tool.beginUpdate(false);
		expect(notes).toEqual(["Blocker: a destructive command is running."]);
		expect(gate.report()["semantic-concern"]).toEqual({
			proposed: 1,
			delivered: 0,
			suppressed: { budget: 1 },
		});
		expect(gate.report()["gate-defect"]).toEqual({ proposed: 1, delivered: 1, suppressed: {} });
	});
});

describe("AdviseTool with a supervision gate", () => {
	it("matches the legacy rank map plus emission guard on 500 seeded sequences", async () => {
		const sequenceCount = 500;
		for (let seed = 1; seed <= sequenceCount; seed++) {
			const ops = makeOps(mulberry32(seed));
			const legacy = await runLegacy(ops);
			const gated = await runGated(ops);
			if (JSON.stringify(legacy) !== JSON.stringify(gated)) {
				throw new Error(
					`seed ${seed} diverged\nops=${JSON.stringify(ops)}\nlegacy=${JSON.stringify(legacy)}\ngated=${JSON.stringify(gated)}`,
				);
			}
		}
	});
});
