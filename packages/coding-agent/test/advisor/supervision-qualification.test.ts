import { afterAll, beforeAll, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import { removeWithRetries } from "@oh-my-pi/pi-utils";
import type { AdvisorCategory, AdvisorSeverity } from "../../src/advisor/advise-tool";
import { ADVISOR_WIP_MARKER } from "../../src/advisor/delta-split";
import {
	type AdvisorSupervisionDecision,
	AdvisorSupervisionGate,
	type AdvisorSupervisionInput,
} from "../../src/advisor/supervision-gate";
import { type AdvisorReplayEvent, loadAdvisorSentinelCorpus } from "../../src/advisor/supervision-history";
import { createAdvisorReplayArm, qualifyAdvisorSupervision } from "../../src/advisor/supervision-qualification";
import { AdvisorTranscriptRecorder } from "../../src/advisor/transcript-recorder";
import {
	CPK_SUPERVISION_RULE_CLASSES,
	type CpkSupervisionRuleClass,
} from "../../src/extensibility/cpk/supervision-proposal";

const N_WRITE_PATH = "The agent edited the write path without running the required flush check.";
const N_RETRY = "The new retry loop may double-charge because the idempotency key is generated inside the loop.";
const N_PLAN_GATE = "Two rules disagree on whether the plan gate applies before or after the write.";
const N_BLOCKER = "The write path mutates state before the approval gate, violating the policy.";
const N_FALSE_POSITIVE = "This may be a false positive: the tool result looks truncated but is a complete preview.";
const N_GATE = "Rule-class check accepts a proposal whose ruleClass does not match its declared class.";
const N_BUDGET_A = "Bound the retry loop attempt count to avoid an unbounded retry storm.";
const N_BUDGET_B = "Log the failure cause before the retry loop swallows it.";

interface SpecAdvise {
	note: string;
	severity?: AdvisorSeverity;
	category?: AdvisorCategory;
}

interface SpecUpdate {
	inProgress: boolean;
	advises: SpecAdvise[];
}

interface SpecLabel {
	ruleClass: CpkSupervisionRuleClass;
	transcriptIndex: number;
}

interface SessionSpec {
	id: string;
	updates: SpecUpdate[];
	labels: SpecLabel[];
}

const CORPUS: SessionSpec[] = [
	{
		id: "gate-defect",
		updates: [
			{ inProgress: false, advises: [{ note: N_GATE, severity: "concern", category: "gate-defect" }] },
			{ inProgress: false, advises: [{ note: "Stop.", severity: "blocker", category: "gate-defect" }] },
			{ inProgress: false, advises: [{ note: "LGTM", severity: "concern", category: "gate-defect" }] },
		],
		labels: [{ ruleClass: "gate-defect", transcriptIndex: 1 }],
	},
	{
		id: "model-procedure-miss",
		updates: [
			{
				inProgress: false,
				advises: [{ note: N_WRITE_PATH, severity: "concern", category: "model-procedure-miss" }],
			},
			{
				inProgress: false,
				advises: [{ note: N_WRITE_PATH, severity: "concern", category: "model-procedure-miss" }],
			},
			{
				inProgress: false,
				advises: [{ note: N_WRITE_PATH.slice(0, -1), severity: "concern", category: "model-procedure-miss" }],
			},
		],
		labels: [{ ruleClass: "model-procedure-miss", transcriptIndex: 1 }],
	},
	{
		id: "semantic-concern",
		updates: [
			{ inProgress: true, advises: [{ note: N_RETRY, severity: "nit", category: "semantic-concern" }] },
			{ inProgress: false, advises: [] },
			{
				inProgress: false,
				advises: [
					{ note: N_BUDGET_A, severity: "concern", category: "semantic-concern" },
					{ note: N_BUDGET_B, severity: "concern", category: "semantic-concern" },
				],
			},
		],
		labels: [{ ruleClass: "semantic-concern", transcriptIndex: 1 }],
	},
	{
		id: "policy-ambiguity",
		updates: [
			{
				inProgress: true,
				advises: [
					{ note: N_PLAN_GATE, severity: "concern", category: "policy-ambiguity" },
					{ note: N_BLOCKER, severity: "blocker", category: "policy-ambiguity" },
				],
			},
			{ inProgress: false, advises: [] },
		],
		labels: [{ ruleClass: "policy-ambiguity", transcriptIndex: 2 }],
	},
	{
		id: "possible-false-positive",
		updates: [
			{
				inProgress: false,
				advises: [{ note: N_FALSE_POSITIVE, severity: "nit", category: "possible-false-positive" }],
			},
			{ inProgress: false, advises: [{ note: "   ", severity: "nit", category: "possible-false-positive" }] },
		],
		labels: [{ ruleClass: "possible-false-positive", transcriptIndex: 1 }],
	},
];

function userMessage(text: string): AgentMessage {
	return { role: "user", content: [{ type: "text", text }], timestamp: 1 } as unknown as AgentMessage;
}

function assistantMessage(advises: SpecAdvise[]): AgentMessage {
	const content =
		advises.length > 0
			? advises.map((a, i) => ({
					type: "toolCall" as const,
					id: `call_${i}`,
					name: "advise",
					arguments: {
						note: a.note,
						...(a.severity ? { severity: a.severity } : {}),
						...(a.category ? { category: a.category } : {}),
					},
				}))
			: [{ type: "text" as const, text: "No new concerns." }];
	return {
		role: "assistant",
		content,
		api: "anthropic-messages",
		provider: "anthropic",
		model: "sentinel-corpus-model",
		usage: {
			input: 1,
			output: 1,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 2,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		stopReason: advises.length > 0 ? "tool_use" : "stop",
		timestamp: 1,
	} as unknown as AgentMessage;
}

async function writeSession(dir: string, spec: SessionSpec): Promise<void> {
	const recorder = new AdvisorTranscriptRecorder(
		() => `${dir}.jsonl`,
		() => "/sentinel-cwd",
		`${spec.id}.jsonl`,
	);
	for (const [i, update] of spec.updates.entries()) {
		const body = `### Session update\n\nupdate ${i}`;
		recorder.beginTurn();
		recorder.record(userMessage(update.inProgress ? `${body}\n\n---\n\n${ADVISOR_WIP_MARKER}` : body));
		recorder.record(assistantMessage(update.advises));
		recorder.commitTurn();
	}
	await recorder.close();
}

async function writeCorpus(dir: string, specs: SessionSpec[]): Promise<void> {
	await fs.mkdir(dir, { recursive: true });
	for (const spec of specs) {
		await writeSession(dir, spec);
		await Bun.write(path.join(dir, `${spec.id}.labels.json`), JSON.stringify({ expected: spec.labels }));
	}
}

class DropPolicyAmbiguityGate extends AdvisorSupervisionGate {
	override decide(input: AdvisorSupervisionInput): AdvisorSupervisionDecision {
		if (input.category === "policy-ambiguity") {
			return { deliver: false, reason: "duplicate-rank" };
		}
		return super.decide(input);
	}
}

describe("Advisor Supervision Qualification (OMP-208-s07)", () => {
	const targetDir = path.resolve(import.meta.dir, "../fixtures/cpk6-advisor-sentinels");
	async function getCorpusPath(): Promise<string> {
		if (
			await Bun.file(
				path.join("packages/coding-agent/test/fixtures/cpk6-advisor-sentinels", "gate-defect.jsonl"),
			).exists()
		) {
			return "packages/coding-agent/test/fixtures/cpk6-advisor-sentinels";
		}
		return targetDir;
	}

	let createdFixtures = false;

	beforeAll(async () => {
		try {
			await fs.access(targetDir);
		} catch {
			createdFixtures = true;
			await writeCorpus(targetDir, CORPUS);
		}
	});

	afterAll(async () => {
		if (createdFixtures) {
			await removeWithRetries(targetDir);
		}
	});

	it("qualifies structured and canary paths over historical sentinel corpus with zero regressions", async () => {
		const sessions = await loadAdvisorSentinelCorpus(await getCorpusPath());
		expect(sessions).toHaveLength(5);

		const totalEvents = sessions.reduce((sum, s) => sum + s.events.length, 0);

		const { structured, canary, canaryBudgetExceeded } = await qualifyAdvisorSupervision(sessions);

		// Status and zero regression
		expect(structured.status).toBe("qualified");
		expect(canary.status).toBe("qualified");
		expect(structured.zeroRegression).toBe(true);
		expect(canary.zeroRegression).toBe(true);

		// Both arm invocations equal the event count
		expect(structured.invocations.legacy).toBe(totalEvents);
		expect(structured.invocations.candidate).toBe(totalEvents);
		expect(canary.invocations.legacy).toBe(totalEvents);
		expect(canary.invocations.candidate).toBe(totalEvents);

		// Canary budget exceeded list is empty
		expect(canaryBudgetExceeded).toEqual([]);

		// Per-class expectations
		for (const ruleClass of CPK_SUPERVISION_RULE_CLASSES) {
			const sReport = structured.classes[ruleClass];
			const cReport = canary.classes[ruleClass];

			expect(sReport.expected).toBeGreaterThanOrEqual(1);
			expect(cReport.expected).toBeGreaterThanOrEqual(1);

			// Legacy caught == expected
			expect(sReport.legacy.caught).toBe(sReport.expected);
			expect(cReport.legacy.caught).toBe(cReport.expected);

			// Candidate caught == expected
			expect(sReport.candidate.caught).toBe(sReport.expected);
			expect(cReport.candidate.caught).toBe(cReport.expected);

			// Candidate false positives <= legacy false positives
			expect(sReport.candidate.falsePositives).toBeLessThanOrEqual(sReport.legacy.falsePositives);
			expect(cReport.candidate.falsePositives).toBeLessThanOrEqual(cReport.legacy.falsePositives);

			// Invalid counts are 0
			expect(sReport.legacy.invalid).toBe(0);
			expect(sReport.candidate.invalid).toBe(0);
			expect(cReport.legacy.invalid).toBe(0);
			expect(cReport.candidate.invalid).toBe(0);
		}
	});

	it("negative control: dropping policy-ambiguity causes structured regression and canary rollback", async () => {
		const sessions = await loadAdvisorSentinelCorpus(await getCorpusPath());
		const droppingGate = new DropPolicyAmbiguityGate();

		const { structured, canary, canaryBudgetExceeded } = await qualifyAdvisorSupervision(sessions, {
			structuredGate: droppingGate,
		});

		// Structured arm regressed with policy-ambiguity element
		expect(structured.status).toBe("regressed");
		expect(structured.zeroRegression).toBe(false);
		expect(structured.regressions).toContainEqual({
			sessionId: "policy-ambiguity",
			ruleClass: "policy-ambiguity",
			transcriptIndex: 2,
		});

		// Canary pipeline exceeded budget on that session and rolled back to legacy
		expect(canaryBudgetExceeded).toEqual(["policy-ambiguity"]);
		expect(canary.regressions).toEqual([]);
		expect(canary.status).toBe("qualified");
		expect(canary.zeroRegression).toBe(true);
	});

	it("reports insufficient_evidence on an empty session list", async () => {
		const { structured, canary, canaryBudgetExceeded } = await qualifyAdvisorSupervision([]);

		expect(structured.status).toBe("insufficient_evidence");
		expect(canary.status).toBe("insufficient_evidence");
		expect(structured.zeroRegression).toBe(false);
		expect(canary.zeroRegression).toBe(false);
		expect(canaryBudgetExceeded).toEqual([]);
	});

	it("createAdvisorReplayArm collects per-session pipeline reports and keeps deferred indices", async () => {
		const arm = createAdvisorReplayArm("structured");
		expect(arm.name).toBe("structured");

		const instance = await arm.start("session-1");
		const event1: AdvisorReplayEvent = { type: "update", inProgress: true };
		const event2: AdvisorReplayEvent = {
			type: "advise",
			note: N_RETRY,
			severity: "nit",
			category: "semantic-concern",
		};
		const event3: AdvisorReplayEvent = { type: "update", inProgress: false };

		const r1 = await instance.onEvent(event1, 0);
		expect(r1).toEqual([]);

		const r2 = await instance.onEvent(event2, 1);
		expect(r2).toEqual([]); // Deferred during WIP

		const r3 = await instance.onEvent(event3, 2);
		expect(r3).toHaveLength(1);
		// Deferred flush keeps its advise-event index (1), not the flush index (2)
		expect(r3[0]).toMatchObject({
			ruleClass: "semantic-concern",
			transcriptIndex: 1,
			claim: N_RETRY,
		});

		const reports = arm.reports();
		expect(reports).toHaveLength(1);
		expect(reports[0].sessionId).toBe("session-1");
		expect(reports[0].report.authority).toBe("structured");
		expect(reports[0].report.invocations.structured).toBe(1);
	});
});
