/**
 * Contracts: OMP-208-s08 retires the stacked legacy dedupe from the default
 * live Advisor path with one-switch rollback retained.
 *
 * The retired path is the AdviseTool severity-rank map followed by the
 * AdvisorEmissionGuard stage inside `SessionAdvisors#routeAdvice` — the same
 * dedupe the structured {@link AdvisorSupervisionGate} now owns. The schema
 * default of `advisor.supervisionPath` therefore flips to `structured`, which
 * three behaviors must pin:
 *
 *  1. A session built with default settings reports `structured` authority and
 *     zero legacy invocations — no second live dispatcher runs.
 *  2. The rollback drill: setting `advisor.supervisionPath=legacy` and rebuilding
 *     the advisor reports `legacy` authority with zero structured invocations.
 *     Over the sentinel corpus, `createAdvisorReplayArm("legacy")` delivers
 *     exactly the proposals a pipeline-free AdviseTool + AdvisorEmissionGuard
 *     pair drives (wired as OMP-208-s03 drives it), so the rollback path is the
 *     same supervisor the legacy live path always was.
 *  3. `qualifyAdvisorSupervision` over the corpus is `qualified` for both the
 *     structured and canary arms, so the default flip stays CI-gated.
 */
import { afterAll, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import { Agent, type AgentTool } from "@oh-my-pi/pi-agent-core";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { removeWithRetries, TempDir } from "@oh-my-pi/pi-utils";
import { AdviseTool, type AdvisorCategory, type AdvisorSeverity } from "../../src/advisor/advise-tool";
import { ADVISOR_WIP_MARKER } from "../../src/advisor/delta-split";
import { AdvisorEmissionGuard } from "../../src/advisor/emission-guard";
import { type AdvisorReplayEvent, loadAdvisorSentinelCorpus } from "../../src/advisor/supervision-history";
import { createAdvisorReplayArm, qualifyAdvisorSupervision } from "../../src/advisor/supervision-qualification";
import { AdvisorTranscriptRecorder } from "../../src/advisor/transcript-recorder";
import { ModelRegistry } from "../../src/config/model-registry";
import { Settings } from "../../src/config/settings";
import {
	type CpkSupervisionProposal,
	type CpkSupervisionRuleClass,
	proposalFromAdvisorNote,
} from "../../src/extensibility/cpk/supervision-proposal";
import { AgentSession } from "../../src/session/agent-session";
import { AuthStorage } from "../../src/session/auth-storage";
import { SessionManager } from "../../src/session/session-manager";
import { cfgAdvisorSupervisionPath } from "../../src/advisor/settings";

type SettingOverrides = Parameters<typeof Settings.isolated>[0];

const ADVISOR_TYPE = "advisor";
const REAL_CONCERN = "Check the retry queue bounds.";

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

/**
 * The OMP-208-s03 sentinel corpus: one session per CPK-6 rule class, each
 * carrying the noise/deferral cases the legacy pair must absorb (whitespace
 * note, `"Stop."`/`"LGTM"`, a punctuation variant, an equal-severity retag, a
 * second distinct note in one final update, a wip nit delivered by the flush,
 * and a wip blocker).
 */
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

/**
 * Replay one session through a pipeline-free AdviseTool + AdvisorEmissionGuard
 * pair, wired exactly as `SessionAdvisors` wires it (tool begin, then guard
 * begin; guard accepts in `onAdvice`) and as OMP-208-s03's corpus drive does.
 * Returns the CPK-6 proposals the pair delivers, in delivery order.
 */
async function drivePipelineFreePair(events: readonly AdvisorReplayEvent[]): Promise<CpkSupervisionProposal[]> {
	const delivered: CpkSupervisionProposal[] = [];
	const guard = new AdvisorEmissionGuard();
	let currentIndex = 0;
	const tool = new AdviseTool(
		(note, severity, category, transcriptIndex) => {
			delivered.push(proposalFromAdvisorNote(note, severity, category, transcriptIndex ?? currentIndex));
		},
		{ guard, transcriptIndex: () => currentIndex },
	);
	for (const [index, event] of events.entries()) {
		currentIndex = index;
		if (event.type === "update") {
			tool.beginUpdate(event.inProgress);
			continue;
		}
		if (event.category === undefined) throw new Error("sentinel corpus advise event is missing a category");
		await tool.execute("id", {
			note: event.note,
			category: event.category,
			...(event.severity ? { severity: event.severity } : {}),
		});
	}
	return delivered;
}

/** Replay one session through `createAdvisorReplayArm("legacy")`, in delivery order. */
async function driveLegacyArm(
	sessionId: string,
	events: readonly AdvisorReplayEvent[],
): Promise<CpkSupervisionProposal[]> {
	const arm = createAdvisorReplayArm("legacy");
	const instance = await arm.start(sessionId);
	const proposals: CpkSupervisionProposal[] = [];
	for (const [index, event] of events.entries()) {
		for (const item of await instance.onEvent(event, index)) proposals.push(item as CpkSupervisionProposal);
	}
	return proposals;
}

interface Harness {
	session: AgentSession;
	settings: Settings;
	authStorage: AuthStorage;
}

const tempDir = TempDir.createSync("@pi-advisor-retirement-");

afterAll(async () => {
	await tempDir.remove();
});

function createHarness(overrides: SettingOverrides = {}): Promise<Harness> {
	const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
	const mock = createMockModel({ responses: [{ content: ["EXACT VERDICT"], stopReason: "stop" }] });
	const advisorMock = createMockModel({ responses: [] });
	const agent = new Agent({
		getApiKey: () => "test-key",
		initialState: { model, systemPrompt: ["Test"], tools: [] },
		streamFn: mock.stream,
	});
	const settings = Settings.isolated({
		"compaction.enabled": false,
		"retry.enabled": false,
		...overrides,
	});
	settings.setModelRole("advisor", "anthropic/claude-sonnet-4-5");
	return AuthStorage.create(":memory:").then(async authStorage => {
		authStorage.keys.setRuntime("anthropic", "test-key");
		const modelRegistry = new ModelRegistry(authStorage, tempDir.join("models.yml"));
		const session = new AgentSession({
			agent,
			sessionManager: SessionManager.inMemory(),
			settings,
			modelRegistry,
			advisorTools: [],
			advisorStreamFn: advisorMock.stream,
		});
		return { session, settings, authStorage };
	});
}

function adviseTool(session: AgentSession): AgentTool<any> {
	const advisor = session.getAdvisorAgent();
	if (!advisor) throw new Error("expected advisor agent");
	const tool = advisor.state.tools?.find(item => item.name === "advise");
	if (!tool) throw new Error("expected advise tool");
	return tool;
}

async function advise(
	session: AgentSession,
	args: { note: string; severity?: "nit" | "concern" | "blocker"; category: string },
): Promise<string> {
	const result = await adviseTool(session).execute(`advise-${args.note}`, args);
	const block = result.content[0];
	if (block?.type !== "text" || block.text === undefined) {
		throw new Error(`unexpected advise result ${JSON.stringify(result.content)}`);
	}
	return block.text;
}

function deliveredNotes(session: AgentSession): string[] {
	const notes: string[] = [];
	for (const message of session.agent.state.messages) {
		if (message.role !== "custom") continue;
		const custom = message as { customType?: string; details?: { notes?: { note?: string }[] } };
		if (custom.customType !== ADVISOR_TYPE) continue;
		for (const note of custom.details?.notes ?? []) {
			if (typeof note.note === "string") notes.push(note.note);
		}
	}
	return notes;
}

async function settlePrimary(session: AgentSession): Promise<void> {
	await session.prompt("answer with exactly one line");
	await session.waitForIdle();
}

describe("advisor supervision retirement (OMP-208-s08)", () => {
	it("defaults the live advisor to structured authority with zero legacy invocations", async () => {
		const harness = await createHarness();
		try {
			const { session } = harness;
			expect(session.setAdvisorEnabled(true)).toBe(true);

			const [entry] = session.getAdvisorSupervisionReport();
			expect(entry?.name).toBe("default");
			expect(entry?.report.path).toBe("structured");
			expect(entry?.report.authority).toBe("structured");
			expect(entry?.report.invocations).toEqual({ legacy: 0, structured: 0 });

			await settlePrimary(session);
			expect(await advise(session, { note: REAL_CONCERN, severity: "concern", category: "semantic-concern" })).toBe(
				"Delivered.",
			);

			const [after] = session.getAdvisorSupervisionReport();
			expect(after?.report).toMatchObject({
				path: "structured",
				authority: "structured",
				invocations: { legacy: 0, structured: 1 },
			});
			expect(deliveredNotes(session)).toEqual([REAL_CONCERN]);
		} finally {
			await harness.session.dispose();
			harness.authStorage.close();
		}
	});

	it("rolls back to legacy authority with one settings switch", async () => {
		const harness = await createHarness();
		try {
			const { session, settings } = harness;
			expect(session.setAdvisorEnabled(true)).toBe(true);
			expect(session.getAdvisorSupervisionReport()[0]?.report.authority).toBe("structured");

			cfgAdvisorSupervisionPath.set(settings, "legacy");
			expect(session.setAdvisorEnabled(false)).toBe(false);
			expect(session.setAdvisorEnabled(true)).toBe(true);

			expect(session.getAdvisorSupervisionReport()[0]?.report).toMatchObject({
				path: "legacy",
				authority: "legacy",
				invocations: { legacy: 0, structured: 0 },
			});

			await advise(session, { note: "Stop.", severity: "blocker", category: "gate-defect" });
			expect(session.getAdvisorSupervisionReport()[0]?.report).toMatchObject({
				path: "legacy",
				authority: "legacy",
				invocations: { legacy: 1, structured: 0 },
			});
		} finally {
			await harness.session.dispose();
			harness.authStorage.close();
		}
	});

	it("matches the pipeline-free legacy pair proposal-for-proposal over the sentinel corpus", async () => {
		const dir = await fs.mkdtemp(path.join(tempDir.path(), "corpus-"));
		try {
			await writeCorpus(dir, CORPUS);
			const sessions = await loadAdvisorSentinelCorpus(dir);
			expect(sessions).toHaveLength(CORPUS.length);

			for (const session of sessions) {
				const armProposals = await driveLegacyArm(session.id, session.events);
				const pairProposals = await drivePipelineFreePair(session.events);
				expect(armProposals).toEqual(pairProposals);
				expect(armProposals.length).toBeGreaterThan(0);
			}
		} finally {
			await removeWithRetries(dir);
		}
	});

	it("qualifies structured and canary supervision over the corpus so the flip stays CI-gated", async () => {
		const dir = await fs.mkdtemp(path.join(tempDir.path(), "qualify-"));
		try {
			await writeCorpus(dir, CORPUS);
			const sessions = await loadAdvisorSentinelCorpus(dir);

			const { structured, canary, canaryBudgetExceeded } = await qualifyAdvisorSupervision(sessions);

			expect(structured.status).toBe("qualified");
			expect(structured.zeroRegression).toBe(true);
			expect(canary.status).toBe("qualified");
			expect(canary.zeroRegression).toBe(true);
			expect(canaryBudgetExceeded).toEqual([]);
		} finally {
			await removeWithRetries(dir);
		}
	});
});
