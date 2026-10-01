import { afterEach, describe, expect, it, spyOn } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { syncAllSessions } from "@oh-my-pi/omp-stats/aggregator";
import { getRecentRequests } from "@oh-my-pi/omp-stats/db";
import { getOverallStats } from "@oh-my-pi/omp-stats/rollup";
import { getSessionsDir } from "@oh-my-pi/pi-utils";
import { installStatsTestIsolation } from "./helpers/temp-agent";

installStatsTestIsolation("@pi-stats-ingest-order-");

/**
 * Filesystem birth times come from a coarse kernel clock: two session files
 * created in the same tick share `birthtimeMs`/`ctimeMs`, and `stat().mtimeMs`
 * only separates the ingest tier. Pin the shared fields with one fixed value
 * so every file in a sync lands in the same tier with the same birth time,
 * which forces the tie-break under test to decide the order.
 */
const FIXED_BORN_MS = 1_700_000_000_000;

let restoreStat: (() => void) | null = null;

afterEach(() => {
	restoreStat?.();
	restoreStat = null;
});

/** Return the real stats for every file, but with one shared birth/ctime. */
function pinEqualBirthTimes(): void {
	const realStat = fs.promises.stat;
	const spy = spyOn(fs.promises, "stat").mockImplementation(async (target: fs.PathLike) => {
		const info = await realStat(target);
		if (typeof target === "string" && target.endsWith(".jsonl")) {
			info.birthtimeMs = FIXED_BORN_MS;
			info.ctimeMs = FIXED_BORN_MS;
		}
		return info;
	});
	restoreStat = () => spy.mockRestore();
}

function buildUserEntry(entryId: string, timestamp: string, content: string) {
	return {
		type: "message",
		id: entryId,
		parentId: null,
		timestamp,
		message: { role: "user", content },
	};
}

function buildAssistantEntry(entryId: string, timestamp: string) {
	return {
		type: "message",
		id: entryId,
		parentId: entryId === "asst01ab" ? "user01ab" : null,
		timestamp,
		message: {
			role: "assistant",
			content: [{ type: "text", text: "ok" }],
			api: "openai-responses",
			provider: "openai",
			model: "gpt-5.4",
			responseId: `resp-${entryId}`,
			usage: {
				input: 100,
				output: 50,
				cacheRead: 0,
				cacheWrite: 0,
				totalTokens: 150,
				cost: { input: 0.001, output: 0.002, cacheRead: 0, cacheWrite: 0, total: 0.003 },
			},
			stopReason: "stop",
			timestamp: Date.parse(timestamp),
			duration: 10,
			ttft: 5,
		},
	};
}

async function writeSessionFile(
	folderSlug: string,
	fileName: string,
	header: { id: string; cwd: string; parentSession?: string },
	entries: unknown[],
): Promise<string> {
	const sessionDir = path.join(getSessionsDir(), folderSlug);
	await fs.promises.mkdir(sessionDir, { recursive: true });
	const sessionFile = path.join(sessionDir, fileName);
	const headerEntry = {
		type: "session",
		version: 3,
		id: header.id,
		timestamp: new Date().toISOString(),
		cwd: header.cwd,
		...(header.parentSession ? { parentSession: header.parentSession } : {}),
	};
	const lines = [headerEntry, ...entries].map(entry => JSON.stringify(entry)).join("\n");
	await Bun.write(sessionFile, `${lines}\n`);
	return sessionFile;
}

/**
 * A parent and a fork whose entries are deep copies (see
 * `SessionManager.fork()`): the fork's JSONL holds the same `(entry_id,
 * timestamp)` pair, so only the first transcript to reach the database may own
 * the copied request.
 */
async function buildForkPair(folderSlug: string, forkCreatedFirst: boolean) {
	const ts = new Date("2026-06-24T10:00:00.000Z").toISOString();
	const userEntry = buildUserEntry("user01ab", ts, "hello");
	const assistantEntry = buildAssistantEntry("asst01ab", ts);
	const parentFile = path.join(getSessionsDir(), folderSlug, "01_parent.jsonl");
	const forkFile = path.join(getSessionsDir(), folderSlug, "02_fork.jsonl");
	const parentHeader = { id: "parent00", cwd: "/tmp/project" };
	// `writeSessionFile` needs the parent path for the fork header; compute it first.
	if (forkCreatedFirst) {
		await writeSessionFile(
			folderSlug,
			"02_fork.jsonl",
			{ ...parentHeader, id: "fork0000", parentSession: parentFile },
			[userEntry, assistantEntry],
		);
		await writeSessionFile(folderSlug, "01_parent.jsonl", parentHeader, [userEntry, assistantEntry]);
	} else {
		await writeSessionFile(folderSlug, "01_parent.jsonl", parentHeader, [userEntry, assistantEntry]);
		await writeSessionFile(
			folderSlug,
			"02_fork.jsonl",
			{ ...parentHeader, id: "fork0000", parentSession: parentFile },
			[userEntry, assistantEntry],
		);
	}
	return { parentFile, forkFile };
}

async function expectParentOwnsRequest(folderSlug: string, forkCreatedFirst: boolean) {
	pinEqualBirthTimes();
	const { parentFile } = await buildForkPair(folderSlug, forkCreatedFirst);

	await syncAllSessions({ workers: 1 });

	const requests = getRecentRequests(10).filter(r => r.entryId === "asst01ab");
	expect(requests).toHaveLength(1);
	expect(requests[0].sessionFile).toBe(parentFile);

	const overall = getOverallStats();
	expect(overall.totalRequests).toBe(1);
	expect(overall.totalInputTokens).toBe(100);
	expect(overall.totalOutputTokens).toBe(50);
}

describe("stats ingest order breaks birth-time ties by path", () => {
	// Session files share the folder's coarse birth time when both are created
	// in one kernel tick. The directory listing order is filesystem-defined and
	// unrelated to creation order, so a fork listed before its parent must not
	// steal the copied entry: path order (`01_parent` < `02_fork`) decides.
	it("keeps the parent as owner when the parent was created first", async () => {
		await expectParentOwnsRequest("--tmp--ingest-parent-first", false);
	});

	it("keeps the parent as owner when the fork was created first", async () => {
		await expectParentOwnsRequest("--tmp--ingest-fork-first", true);
	});
});
