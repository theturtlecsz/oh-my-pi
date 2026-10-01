import { describe, expect, it } from "bun:test";
import { AdviseTool, type AdvisorCategory, type AdvisorSeverity } from "../../src/advisor/advise-tool";
import { AdvisorSupervisionGate, type AdvisorSupervisionReport } from "../../src/advisor/supervision-gate";

const CATEGORIES = [
	"gate-defect",
	"model-procedure-miss",
	"semantic-concern",
	"policy-ambiguity",
	"possible-false-positive",
] as const satisfies readonly AdvisorCategory[];

const SEVERITIES: readonly (AdvisorSeverity | undefined)[] = [undefined, "nit", "concern", "blocker"];

const NOTES = [
	"",
	"   ",
	"Stop.",
	"LGTM",
	"Nit: rename the helper for clarity.",
	"Concern: the helper drops the lock early.",
	"Blocker: the write path drops the ack.",
	"Move retries into the queue, not the request path.",
	"Second concern: wrong env var name.",
	"Concrete: read race in #handleRetry.",
	"Same point raised repeatedly.",
	"The rollback path never runs the third write.",
] as const;

type Op =
	| { type: "update"; inProgress: boolean }
	| { type: "reset" }
	| { type: "advise"; note: string; severity?: AdvisorSeverity; category: AdvisorCategory };

function toolText(result: { content: readonly { type: string; text?: string }[] }): string {
	const block = result.content[0];
	if (block?.type !== "text" || block.text === undefined) {
		throw new Error(`unexpected tool content ${JSON.stringify(result.content)}`);
	}
	return block.text;
}

function deliveredSum(report: AdvisorSupervisionReport): number {
	let total = 0;
	for (const stats of Object.values(report)) {
		if (stats) total += stats.delivered;
	}
	return total;
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
	const advise = () => {
		ops.push({
			type: "advise",
			note: NOTES[Math.floor(rng() * NOTES.length)] ?? "",
			severity: SEVERITIES[Math.floor(rng() * SEVERITIES.length)],
			category: CATEGORIES[Math.floor(rng() * CATEGORIES.length)] ?? "semantic-concern",
		});
	};
	const episodes = 3 + Math.floor(rng() * 4);
	for (let episode = 0; episode < episodes; episode++) {
		if (rng() < 0.2) ops.push({ type: "reset" });
		ops.push({ type: "update", inProgress: rng() < 0.75 });
		const count = 1 + Math.floor(rng() * 5);
		for (let i = 0; i < count; i++) advise();
	}
	ops.push({ type: "update", inProgress: false });
	return ops;
}

describe("AdvisorSupervisionGate deferred delivery tally", () => {
	it("counts a displaced pending nit only if it is later routed, which it is not", async () => {
		const gate = new AdvisorSupervisionGate({ budgetPerUpdate: 1 });
		const routed: string[] = [];
		const tool = new AdviseTool(note => routed.push(note), { guard: gate, transcriptIndex: () => 1 });
		const nitCategory = "policy-ambiguity";
		const concernCategory = "semantic-concern";

		tool.beginUpdate(true);
		const nit = await tool.execute("tc-nit", {
			note: "Nit: rename the helper for clarity.",
			severity: "nit",
			category: nitCategory,
		});
		const concern = await tool.execute("tc-concern", {
			note: "Concern: the helper drops the lock early.",
			severity: "concern",
			category: concernCategory,
		});
		expect(toolText(nit)).toBe("Queued for the end of the turn. Do not re-raise.");
		expect(toolText(concern)).toBe("Queued for the end of the turn. Do not re-raise.");
		expect(routed).toEqual([]);
		expect(gate.report()[nitCategory]).toEqual({ proposed: 1, delivered: 0, suppressed: {} });
		expect(gate.report()[concernCategory]).toEqual({ proposed: 1, delivered: 0, suppressed: {} });

		tool.beginUpdate(false);
		expect(routed).toEqual(["Concern: the helper drops the lock early."]);
		expect(gate.report()[nitCategory]).toEqual({ proposed: 1, delivered: 0, suppressed: {} });
		expect(gate.report()[concernCategory]).toEqual({ proposed: 1, delivered: 1, suppressed: {} });
	});

	it("counts a deferred nit re-raised as a blocker once, including after the flush", async () => {
		const gate = new AdvisorSupervisionGate({ budgetPerUpdate: 1 });
		const routed: { note: string; severity?: AdvisorSeverity }[] = [];
		const tool = new AdviseTool((note, severity) => routed.push({ note, severity }), {
			guard: gate,
			transcriptIndex: () => 2,
		});
		const note = "The retry queue drops the third attempt.";
		const category = "semantic-concern";

		tool.beginUpdate(true);
		const deferred = await tool.execute("tc-nit", { note, severity: "nit", category });
		const blocker = await tool.execute("tc-blocker", { note, severity: "blocker", category });
		expect(toolText(deferred)).toBe("Queued for the end of the turn. Do not re-raise.");
		expect(toolText(blocker)).toBe("Delivered.");
		expect(routed).toEqual([{ note, severity: "blocker" }]);
		expect(deliveredSum(gate.report())).toBe(1);
		expect(gate.report()[category]).toEqual({ proposed: 2, delivered: 1, suppressed: {} });

		tool.beginUpdate(false);
		expect(routed).toEqual([{ note, severity: "blocker" }]);
		expect(deliveredSum(gate.report())).toBe(1);
		expect(gate.report()[category]).toEqual({ proposed: 2, delivered: 1, suppressed: {} });
	});

	it("keeps the delivered sum equal to routed notes after every seeded op", async () => {
		const seeds = 200;
		for (let seed = 1; seed <= seeds; seed++) {
			const rng = mulberry32(seed);
			const budgetPerUpdate = (seed % 4) + 1;
			const gate = new AdvisorSupervisionGate({ budgetPerUpdate });
			let routed = 0;
			const tool = new AdviseTool(
				() => {
					routed += 1;
				},
				{ guard: gate, transcriptIndex: () => seed },
			);
			const ops = makeOps(rng);
			for (let index = 0; index < ops.length; index++) {
				const op = ops[index]!;
				if (op.type === "update") tool.beginUpdate(op.inProgress);
				else if (op.type === "reset") {
					tool.resetDeliveredNotes();
					routed = 0;
				} else {
					await tool.execute("tc", { note: op.note, severity: op.severity, category: op.category });
				}
				const delivered = deliveredSum(gate.report());
				if (delivered !== routed) {
					throw new Error(
						`seed ${seed} budget ${budgetPerUpdate} after op ${index} (${op.type}): delivered ${delivered}, routed ${routed}`,
					);
				}
			}
		}
	});
});
