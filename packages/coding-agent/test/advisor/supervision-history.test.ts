/**
 * Contracts: the OMP-208 sentinel corpus and its replay loader.
 *
 * `loadAdvisorReplayEvents` must recover the advisor's supervision conversation
 * from a real {@link AdvisorTranscriptRecorder} file: each run of consecutive
 * user "Session update" messages is one `update` (its `inProgress` recovered
 * from the {@link ADVISOR_WIP_MARKER} the renderer appends to a wip batch's last
 * chunk), and each assistant `advise` tool call is one `advise` event in file
 * order. `loadAdvisorSentinelCorpus` pairs those events with labels validated by
 * `assertCpkSentinelLabels`, so a bad label fails closed before comparison.
 *
 * The corpus itself is written by the real recorder into a temp dir (recorder
 * format by construction) and covers all five CPK-6 rule classes. Driving the
 * legacy pair (AdviseTool + AdvisorEmissionGuard, wired exactly as
 * `SessionAdvisors` does in `beginAdvisorUpdate`/`#routeAdvice`) must still
 * deliver every labelled defect while leaving each session with at least one
 * undelivered advise — the noise/duplication the guard exists to absorb.
 */
import { describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import { removeWithRetries } from "@oh-my-pi/pi-utils";
import { AdviseTool, type AdvisorCategory, type AdvisorSeverity } from "../../src/advisor/advise-tool";
import { ADVISOR_WIP_MARKER } from "../../src/advisor/delta-split";
import { AdvisorEmissionGuard, normalizeAdvisorNote } from "../../src/advisor/emission-guard";
import {
	type AdvisorReplayEvent,
	loadAdvisorReplayEvents,
	loadAdvisorSentinelCorpus,
} from "../../src/advisor/supervision-history";
import { AdvisorTranscriptRecorder } from "../../src/advisor/transcript-recorder";
import {
	assertCpkSentinelLabels,
	CPK_SUPERVISION_RULE_CLASSES,
	CpkSupervisionError,
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

/**
 * One session per CPK-6 rule class; every listed noise/deferral case appears at
 * least once. `transcriptIndex` is the index into the recovered `events` array
 * (the same coordinate space `assertCpkSentinelLabels(expected, events.length)`
 * validates against), so a label points at one `advise` event inside `events`.
 *
 * Event layouts (u = update, a = advise):
 * - gate-defect          : [u,a,u,a,u,a]         (3 updates, 1 advise each)
 * - model-procedure-miss : [u,a,u,a,u,a]         (3 updates, 1 advise each)
 * - semantic-concern     : [u,a,u,u,a,a]         (wip, empty, then two notes)
 * - policy-ambiguity     : [u,a,a,u]             (wip with concern + blocker)
 * - possible-false-positive: [u,a,u,a]           (note, then whitespace note)
 */
const CORPUS: SessionSpec[] = [
	{
		// "Stop." and "LGTM" are content-free self-talk the phrase filter drops.
		id: "gate-defect",
		updates: [
			{ inProgress: false, advises: [{ note: N_GATE, severity: "concern", category: "gate-defect" }] },
			{ inProgress: false, advises: [{ note: "Stop.", severity: "blocker", category: "gate-defect" }] },
			{ inProgress: false, advises: [{ note: "LGTM", severity: "concern", category: "gate-defect" }] },
		],
		labels: [{ ruleClass: "gate-defect", transcriptIndex: 1 }],
	},
	{
		// An equal-severity retag (events 1 and 3) and a punctuation variant
		// (event 5) of the delivered note.
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
		// A nit (event 1) deferred by the wip update and delivered by the next
		// completed update's flush; then two distinct notes in one final update
		// (events 4 and 5), where one wins the per-update budget.
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
		// A blocker (event 2) in a wip update reaches the guard immediately; the
		// concern (event 1) deferred alongside it is then lost to that update's
		// budget when the completion flush replays it.
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
		// A whitespace-only note (event 3) is dropped by the guard's empty-key check.
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

/**
 * Write one session as `<dir>/<id>.jsonl` through the REAL recorder. The
 * recorder writes to `<resolveSessionFile() without .jsonl>/<filename>`; pointing
 * the session file at `<dir>.jsonl` therefore lands the transcript directly at
 * `<dir>/<id>.jsonl`.
 */
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

/** Materialize a corpus (recorder transcripts + labels) into `dir`. */
async function writeCorpus(dir: string, specs: SessionSpec[]): Promise<void> {
	for (const spec of specs) {
		await writeSession(dir, spec);
		await Bun.write(path.join(dir, `${spec.id}.labels.json`), JSON.stringify({ expected: spec.labels }));
	}
}

interface DrivenAdvise {
	note: string;
	severity?: AdvisorSeverity;
	category?: AdvisorCategory;
	/** Index of this advise within the full recovered `events` array (label coordinate). */
	eventIndex: number;
	/** Whether the enclosing update was a wip (in-progress) batch. */
	inProgress: boolean;
	updateSeq: number;
}

interface DriveResult {
	advises: DrivenAdvise[];
	/** Note text of every advise the guard accepted, in delivery order. */
	deliveredNotes: string[];
}

/** Drive the legacy pair exactly as `SessionAdvisors` wires it. */
async function driveLegacyPair(events: AdvisorReplayEvent[]): Promise<DriveResult> {
	const deliveredNotes: string[] = [];
	const guard = new AdvisorEmissionGuard();
	const tool = new AdviseTool(note => {
		if (guard.accept(note)) deliveredNotes.push(note);
	});
	const advises: DrivenAdvise[] = [];
	let updateSeq = -1;
	let inProgress = false;
	for (const [eventIndex, event] of events.entries()) {
		if (event.type === "update") {
			updateSeq++;
			inProgress = event.inProgress;
			tool.beginUpdate(event.inProgress);
			guard.beginUpdate();
			continue;
		}
		advises.push({
			note: event.note,
			severity: event.severity,
			category: event.category,
			eventIndex,
			inProgress,
			updateSeq,
		});
		// Every corpus advise carries the schema-required category, exactly as the
		// agent loop only ever feeds schema-validated args to the tool.
		if (event.category === undefined) throw new Error("sentinel corpus advise event is missing a category");
		await tool.execute("id", {
			note: event.note,
			category: event.category,
			...(event.severity ? { severity: event.severity } : {}),
		});
	}
	return { advises, deliveredNotes };
}

function countOf(values: string[], value: string): number {
	let count = 0;
	for (const v of values) if (v === value) count++;
	return count;
}

async function withTempDir<T>(fn: (dir: string) => Promise<T>): Promise<T> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "advisor-supervision-"));
	try {
		return await fn(dir);
	} finally {
		await removeWithRetries(dir);
	}
}

describe("loadAdvisorReplayEvents", () => {
	it("recovers updates and advise events from a transcript the real recorder wrote", async () => {
		await withTempDir(async dir => {
			const spec = CORPUS.find(s => s.id === "semantic-concern")!;
			await writeSession(dir, spec);

			const events = await loadAdvisorReplayEvents(path.join(dir, `${spec.id}.jsonl`));

			// update(wip, N_RETRY nit) / update(final, no advise) / update(final, two notes)
			const expected: AdvisorReplayEvent[] = [
				{ type: "update", inProgress: true },
				{ type: "advise", note: N_RETRY, severity: "nit", category: "semantic-concern" },
				{ type: "update", inProgress: false },
				{ type: "update", inProgress: false },
				{ type: "advise", note: N_BUDGET_A, severity: "concern", category: "semantic-concern" },
				{ type: "advise", note: N_BUDGET_B, severity: "concern", category: "semantic-concern" },
			];
			expect(events).toEqual(expected);
		});
	});
});

describe("loadAdvisorSentinelCorpus", () => {
	it("loads five recorder-format sessions covering all five CPK-6 rule classes", async () => {
		await withTempDir(async dir => {
			await writeCorpus(dir, CORPUS);

			const sessions = await loadAdvisorSentinelCorpus(dir);

			expect(sessions.map(s => s.id)).toEqual([...CORPUS.map(s => s.id)].sort());
			const labelled = new Set<string>();
			for (const session of sessions) {
				expect(session.events.some(e => e.type === "advise")).toBe(true);
				for (const label of session.expected) labelled.add(label.ruleClass);
			}
			expect([...labelled].sort()).toEqual([...CPK_SUPERVISION_RULE_CLASSES].sort());
		});
	});

	it("rejects a labels file with an unknown rule class as invalid_sentinel", async () => {
		await withTempDir(async dir => {
			const [first] = CORPUS;
			await writeCorpus(dir, [first]);
			await Bun.write(
				path.join(dir, `${first.id}.labels.json`),
				JSON.stringify({ expected: [{ ruleClass: "not-a-canonical-class", transcriptIndex: 0 }] }),
			);

			const err = await loadAdvisorSentinelCorpus(dir).then(
				() => undefined,
				(e: unknown) => e,
			);
			expect(err).toBeInstanceOf(CpkSupervisionError);
			expect((err as CpkSupervisionError).code).toBe("invalid_sentinel");
		});
	});
});

describe("sentinel corpus catching power (legacy pair)", () => {
	it("delivers every labelled defect and leaves each session with an undelivered advise", async () => {
		await withTempDir(async dir => {
			await writeCorpus(dir, CORPUS);
			const sessions = await loadAdvisorSentinelCorpus(dir);
			expect(sessions).toHaveLength(CORPUS.length);

			for (const session of sessions) {
				const { advises, deliveredNotes } = await driveLegacyPair(session.events);

				for (const label of session.expected) {
					const event = session.events[label.transcriptIndex];
					expect(event, `${session.id} label ${label.transcriptIndex} points at an event`).toBeDefined();
					const advised = event.type === "advise" ? event : undefined;
					expect(advised, `${session.id} label ${label.transcriptIndex} is an advise event`).toBeDefined();
					expect(advised?.category).toBe(label.ruleClass);
					expect(
						countOf(deliveredNotes, advised!.note),
						`${session.id} delivers ${label.ruleClass}`,
					).toBeGreaterThan(0);
				}
				expect(deliveredNotes.length, `${session.id} has undelivered noise`).toBeLessThan(advises.length);
			}
		});
	});

	it("covers every noise and deferral case the guard must absorb", async () => {
		await withTempDir(async dir => {
			await writeCorpus(dir, CORPUS);
			const sessions = await loadAdvisorSentinelCorpus(dir);
			const driven = await Promise.all(
				sessions.map(async session => ({
					session,
					...(await driveLegacyPair(session.events)),
				})),
			);

			const delivered = (session: string, note: string): number =>
				countOf(driven.find(d => d.session.id === session)!.deliveredNotes, note);
			const allAdvises = driven.flatMap(d => d.advises.map(a => ({ session: d.session.id, ...a })));

			expect(allAdvises.find(a => a.note.trim() === "" && delivered(a.session, a.note) === 0)).toBeDefined(); // whitespace-only
			expect(allAdvises.find(a => a.note === "Stop." && delivered(a.session, a.note) === 0)).toBeDefined();
			expect(allAdvises.find(a => a.note === "LGTM" && delivered(a.session, a.note) === 0)).toBeDefined();

			// punctuation variant of a delivered note
			const punctVariant = allAdvises.find(
				a =>
					delivered(a.session, a.note) > 0 &&
					allAdvises.some(
						b =>
							b.session === a.session &&
							b.note !== a.note &&
							normalizeAdvisorNote(b.note) === normalizeAdvisorNote(a.note) &&
							delivered(b.session, b.note) === 0,
					),
			);
			expect(punctVariant).toBeDefined();

			// equal-severity retag: same text, same severity, delivered exactly once
			const retag = allAdvises.find(
				a =>
					a.severity !== undefined &&
					delivered(a.session, a.note) === 1 &&
					allAdvises.some(
						b =>
							b.session === a.session &&
							b.eventIndex !== a.eventIndex &&
							b.note === a.note &&
							b.severity === a.severity,
					),
			);
			expect(retag).toBeDefined();

			// two distinct notes in one final update
			const grouped = new Map<string, { inProgress: boolean; notes: Set<string>; delivered: number }>();
			for (const a of allAdvises) {
				const key = `${a.session}#${a.updateSeq}`;
				const group = grouped.get(key) ?? { inProgress: a.inProgress, notes: new Set<string>(), delivered: 0 };
				group.notes.add(a.note);
				group.delivered += delivered(a.session, a.note);
				grouped.set(key, group);
			}
			const twoInOneFinal = [...grouped.values()].find(
				g => !g.inProgress && g.notes.size >= 2 && g.delivered >= 1 && g.delivered < g.notes.size,
			);
			expect(twoInOneFinal).toBeDefined();

			// nit in a wip update, delivered by the completion flush
			expect(
				allAdvises.find(a => a.inProgress && a.severity === "nit" && delivered(a.session, a.note) > 0),
			).toBeDefined();
			// blocker in a wip update
			expect(allAdvises.find(a => a.inProgress && a.severity === "blocker")).toBeDefined();
		});
	});
});

describe("assertCpkSentinelLabels boundary for corpus labels", () => {
	it("accepts every corpus label against its recovered event count and rejects an out-of-range index", async () => {
		await withTempDir(async dir => {
			await writeCorpus(dir, CORPUS);
			for (const session of await loadAdvisorSentinelCorpus(dir)) {
				expect(() => assertCpkSentinelLabels(session.expected, session.events.length)).not.toThrow();
				const label = session.expected[0];
				expect(() => assertCpkSentinelLabels([label], label.transcriptIndex)).toThrow(CpkSupervisionError);
			}
		});
	});
});
