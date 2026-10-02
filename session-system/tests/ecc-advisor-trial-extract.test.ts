/**
 * Contracts: ecc-advisor-trial/extract.ts turns one advisor trial into a
 * reviewable artifact, and refuses a run that is not provably complete.
 *
 * - Findings are `advise` calls the primary actually received: a paired,
 *   non-error tool result whose text is not a suppression ack. Suppressed acks,
 *   errored results, and calls with no result are counted separately and never
 *   appear as findings.
 * - A completed run with no advise call is a valid run with zero findings.
 * - A non-zero exit, no completion, or an ambiguous transcript set (0 or >1
 *   matching transcripts) exits 1 and writes nothing under `--out`.
 */
import { afterAll, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { AgentMessage } from "@oh-my-pi/pi-agent-core";
import { ADVISOR_ACK_SUPPRESSED } from "@oh-my-pi/pi-coding-agent/advisor/advise-tool";
import { slugifyAdvisorName } from "@oh-my-pi/pi-coding-agent/advisor/config";
import { advisorTranscriptFilename } from "@oh-my-pi/pi-coding-agent/advisor/transcript-recorder";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import {
	ECC_ADVISOR_NAME,
	ECC_MECHANISM,
	NOTES_NAME,
	TRANSCRIPT_COPY_NAME,
	type AdvisorTrialNotes,
	extractAdvisorRun,
} from "../../scripts/ecc-advisor-trial/extract";

const repoRoot = path.resolve(import.meta.dir, "../..");
const script = path.join(repoRoot, "scripts/ecc-advisor-trial/extract.ts");
const TRANSCRIPT_NAME = advisorTranscriptFilename(slugifyAdvisorName(ECC_ADVISOR_NAME));
const COMMIT = "0123456789abcdef0123456789abcdef01234567";

const tempDirs: string[] = [];
async function tempDir(prefix: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

async function cleanup(): Promise<void> {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true });
}

const USAGE = {
	input: 1,
	output: 1,
	cacheRead: 0,
	cacheWrite: 0,
	totalTokens: 2,
	cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
};

function adviseCall(id: string, note: string, extra: Record<string, unknown> = {}) {
	return { type: "toolCall" as const, id, name: "advise", arguments: { note, ...extra } };
}

function assistant(
	content: unknown[],
	options: { stopReason?: string; provider?: string; model?: string } = {},
): AgentMessage {
	return {
		role: "assistant",
		content,
		api: "anthropic-messages",
		provider: options.provider ?? "anthropic",
		model: options.model ?? "claude-4",
		usage: USAGE,
		stopReason: options.stopReason ?? "stop",
		timestamp: 1,
	} as unknown as AgentMessage;
}

function toolResult(id: string, text: string, isError = false): AgentMessage {
	return {
		role: "toolResult",
		toolCallId: id,
		toolName: "advise",
		content: [{ type: "text", text }],
		isError,
		timestamp: 2,
	} as unknown as AgentMessage;
}

function user(text: string): AgentMessage {
	return { role: "user", content: [{ type: "text", text }], timestamp: 0 } as unknown as AgentMessage;
}

/** Persist a transcript exactly like `AdvisorTranscriptRecorder` (open + appendMessage). */
async function writeTranscript(file: string, entries: AgentMessage[]): Promise<void> {
	await fs.mkdir(path.dirname(file), { recursive: true });
	const manager = await SessionManager.open(file, undefined, undefined, { suppressBreadcrumb: true });
	for (const entry of entries) manager.appendMessage(entry as never);
	await manager.flush();
	await manager.close();
}

/** Build `<sessions>/<bucket>/__advisor.<slug>.jsonl`. */
async function transcriptAt(sessions: string, bucket: string, entries: AgentMessage[]): Promise<string> {
	const file = path.join(sessions, bucket, TRANSCRIPT_NAME);
	await writeTranscript(file, entries);
	return file;
}

async function makeOut(): Promise<string> {
	const out = await tempDir("advisor-trial-out-");
	await fs.mkdir(out, { recursive: true });
	return out;
}

async function runCli(args: string[]): Promise<{ code: number }> {
	const env = { ...process.env };
	delete env.FORCE_COLOR;
	delete env.NO_COLOR;
	const proc = Bun.spawn(["bun", script, ...args], {
		cwd: repoRoot,
		stdin: "ignore",
		stdout: "pipe",
		stderr: "pipe",
		env,
	});
	const code = await proc.exited;
	return { code };
}

async function readNotes(out: string): Promise<AdvisorTrialNotes> {
	return (await Bun.file(path.join(out, NOTES_NAME)).json()) as AdvisorTrialNotes;
}

describe("extractAdvisorRun", () => {
	test("separates delivered notes from suppressed, errored, and unmatched calls", async () => {
		const sessions = await tempDir("advisor-trial-sessions-");
		const out = await makeOut();
		await transcriptAt(sessions, "2026-10-02T00-00-00-000Z", [
			user("### Session update"),
			assistant([adviseCall("call-a", "A", { category: "semantic-concern" })]),
			toolResult("call-a", "Delivered."),
			assistant([adviseCall("call-b", "B")]),
			toolResult("call-b", "Queued for the end of the turn. Do not re-raise."),
			assistant([adviseCall("call-c", "C")]),
			toolResult("call-c", ADVISOR_ACK_SUPPRESSED.duplicate),
			assistant([adviseCall("call-d", "D")]),
			toolResult("call-d", "boom", true),
			// No result for this call: dropped as an unmatched dangling call.
			assistant([adviseCall("call-e", "E")]),
			assistant([{ type: "text", text: "done" }]),
		]);

		const outcome = await extractAdvisorRun({
			case: "ecc-db-review",
			commit: COMMIT,
			exitCode: 0,
			sessions,
			out,
		});
		expect(outcome.ok).toBe(true);
		if (!outcome.ok) return;

		const notes = await readNotes(out);
		expect(notes).toMatchObject({
			case: "ecc-db-review",
			commit: COMMIT,
			advisor: ECC_ADVISOR_NAME,
			mechanism: ECC_MECHANISM,
			omp_exit: 0,
			advisor_completed: true,
			model: "anthropic/claude-4",
			suppressed: 1,
			failed: 1,
			unmatched: 1,
		});
		expect(notes.notes).toEqual([
			{ n: 1, tool_call_id: "call-a", note: "A", severity: null, category: "semantic-concern" },
			{ n: 2, tool_call_id: "call-b", note: "B", severity: null, category: null },
		]);
		// The copy is byte-exact and its sha256 matches the recorded digest.
		const source = await Bun.file(path.join(sessions, "2026-10-02T00-00-00-000Z", TRANSCRIPT_NAME)).bytes();
		const copied = await Bun.file(path.join(out, TRANSCRIPT_COPY_NAME)).bytes();
		expect(copied).toEqual(source);
		expect(notes.transcript_sha256).toBe(new Bun.CryptoHasher("sha256").update(source).digest("hex"));
	});

	test("a completed run with no advise call is valid with zero findings", async () => {
		const sessions = await tempDir("advisor-trial-sessions-");
		const out = await makeOut();
		await transcriptAt(sessions, "only", [
			user("### Session update"),
			assistant([{ type: "text", text: "no concerns" }]),
		]);

		const outcome = await extractAdvisorRun({ case: "case", commit: COMMIT, exitCode: 0, sessions, out });
		expect(outcome.ok).toBe(true);
		if (!outcome.ok) return;
		const notes = await readNotes(out);
		expect(notes.notes).toEqual([]);
		expect(notes.suppressed).toBe(0);
		expect(notes.failed).toBe(0);
		expect(notes.unmatched).toBe(0);
	});

	test("rejects exit-code, incomplete, and ambiguous runs without writing to --out", async () => {
		const cases: { name: string; exitCode: number; entries: AgentMessage[] }[] = [
			{
				name: "non-zero exit code",
				exitCode: 1,
				entries: [assistant([adviseCall("call-a", "A")]), toolResult("call-a", "Delivered.")],
			},
			{
				name: "last assistant stopReason error",
				exitCode: 0,
				entries: [
					assistant([adviseCall("call-a", "A")]),
					toolResult("call-a", "Delivered."),
					assistant([{ type: "text", text: "partial" }], { stopReason: "error" }),
				],
			},
			{
				name: "last message is a tool result",
				exitCode: 0,
				entries: [assistant([adviseCall("call-a", "A")]), toolResult("call-a", "Delivered.")],
			},
		];
		for (const scenario of cases) {
			const sessions = await tempDir("advisor-trial-sessions-");
			const out = await makeOut();
			await transcriptAt(sessions, "only", scenario.entries);
			const outcome = await extractAdvisorRun({
				case: "case",
				commit: COMMIT,
				exitCode: scenario.exitCode,
				sessions,
				out,
			});
			expect(outcome.ok, scenario.name).toBe(false);
			expect(await fs.readdir(out), scenario.name).toEqual([]);
		}

		// Zero matching transcripts.
		const emptySessions = await tempDir("advisor-trial-sessions-");
		const emptyOut = await makeOut();
		const none = await extractAdvisorRun({
			case: "case",
			commit: COMMIT,
			exitCode: 0,
			sessions: emptySessions,
			out: emptyOut,
		});
		expect(none.ok).toBe(false);
		expect(await fs.readdir(emptyOut)).toEqual([]);

		// Two matching transcripts is ambiguous.
		const twoSessions = await tempDir("advisor-trial-sessions-");
		const twoOut = await makeOut();
		const completed = [assistant([{ type: "text", text: "ok" }])];
		await transcriptAt(twoSessions, "first", completed);
		await transcriptAt(twoSessions, "second", completed);
		const two = await extractAdvisorRun({
			case: "case",
			commit: COMMIT,
			exitCode: 0,
			sessions: twoSessions,
			out: twoOut,
		});
		expect(two.ok).toBe(false);
		expect(await fs.readdir(twoOut)).toEqual([]);
	}, 30_000);

	test("CLI exits 1 on a refused run and 0 on a completed one", async () => {
		const sessions = await tempDir("advisor-trial-sessions-");
		const out = await makeOut();
		await transcriptAt(sessions, "only", [assistant([{ type: "text", text: "ok" }])]);
		const common = ["--case", "case", "--commit", COMMIT, "--sessions", sessions, "--out", out];

		const refused = await runCli([...common, "--exit-code", "1"]);
		expect(refused.code).toBe(1);
		expect(await fs.readdir(out)).toEqual([]);

		const accepted = await runCli([...common, "--exit-code", "0"]);
		expect(accepted.code).toBe(0);
		expect(await Bun.file(path.join(out, NOTES_NAME)).exists()).toBe(true);
		expect((await readNotes(out)).advisor_completed).toBe(true);
	}, 30_000);
});

afterAll(cleanup);
