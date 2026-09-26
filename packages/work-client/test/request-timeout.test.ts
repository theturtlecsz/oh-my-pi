import { afterEach, expect, test, vi } from "bun:test";
import * as os from "node:os";
import { WORK_REQUEST_LOAD_CAP, WORK_REQUEST_TIMEOUT_MS, WorkClient, workRequestLoadScale } from "../src/index";

const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";

afterEach(() => {
	vi.restoreAllMocks();
});

/** The abort window the client arms for one loopback request, with time frozen out. */
async function capturedAbortWindow(load: number, cores: number): Promise<number> {
	vi.spyOn(os, "loadavg").mockReturnValue([load, load, load]);
	vi.spyOn(os, "availableParallelism").mockReturnValue(cores);
	const windows: number[] = [];
	vi.spyOn(AbortSignal, "timeout").mockImplementation((milliseconds?: number) => {
		windows.push(milliseconds ?? 0);
		return new AbortController().signal;
	});
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		WORKSPACE_ID,
		() => "token",
		async () => Response.json({ workspace_id: WORKSPACE_ID, work_items: [] }),
	);
	await client.tree();
	expect(windows).toHaveLength(1);
	return windows[0]!;
}

test("a loaded host stretches the request abort window instead of cancelling mid-command", async () => {
	// 160 runnable tasks over 10 cores: 1 + 160/10 = 17, clamped to the 8x ceiling.
	expect(await capturedAbortWindow(160, 10)).toBe(WORK_REQUEST_TIMEOUT_MS * WORK_REQUEST_LOAD_CAP);
});

test("an idle host keeps the fixed floor so a healthy request is not held open", async () => {
	expect(await capturedAbortWindow(0, 10)).toBe(WORK_REQUEST_TIMEOUT_MS);
});

test("load scaling is floored at one, grows with the queue, and never exceeds the ceiling", () => {
	expect(workRequestLoadScale(0, 16)).toBe(1);
	expect(workRequestLoadScale(-4, 16)).toBe(1);
	expect(workRequestLoadScale(8, 16)).toBe(1.5);
	expect(workRequestLoadScale(16, 16)).toBe(2);
	expect(workRequestLoadScale(10_000, 16)).toBe(WORK_REQUEST_LOAD_CAP);
});
