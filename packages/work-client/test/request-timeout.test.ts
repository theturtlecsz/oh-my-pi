import { afterEach, expect, test, vi } from "bun:test";
import * as os from "node:os";
import {
	WORK_REQUEST_ATTEMPTS,
	WORK_REQUEST_LOAD_CAP,
	WORK_REQUEST_TIMEOUT_MS,
	WorkClient,
	WorkError,
	workRequestLoadScale,
} from "../src/index";

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

/** A client whose first `failures` transports reject before any response. */
function failingClient(failures: number): { client: WorkClient; attempts: () => number } {
	let calls = 0;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		WORKSPACE_ID,
		() => "token",
		async () => {
			calls++;
			if (calls <= failures) throw new Error("The socket connection was closed unexpectedly");
			return Response.json({ workspace_id: WORKSPACE_ID, items: [], relations: [], projects: [] });
		},
	);
	return { client, attempts: () => calls };
}

test("a closed pre-response socket is resent instead of failing the command", async () => {
	// A pooled keep-alive connection the service already closed surfaces as an
	// instant transport rejection, not an abort — the request must still reach a
	// response rather than surfacing `unavailable`.
	const { client, attempts } = failingClient(1);
	await expect(client.tree()).resolves.toMatchObject({ workspace_id: WORKSPACE_ID });
	expect(attempts()).toBe(2);
});

test("a transport that never comes back still fails after the bounded attempts", async () => {
	const { client, attempts } = failingClient(WORK_REQUEST_ATTEMPTS);
	await expect(client.tree()).rejects.toBeInstanceOf(WorkError);
	expect(attempts()).toBe(WORK_REQUEST_ATTEMPTS);
});

test("a body read torn after the headers is resent instead of failing the command", async () => {
	// The abort window bounds the whole exchange, so a slow response can tear the
	// body read after a 200 arrived. The stored result replays by operation id —
	// the resent attempt must deliver it rather than surfacing `unavailable`.
	let calls = 0;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		WORKSPACE_ID,
		() => "token",
		async () => {
			calls++;
			if (calls === 1) {
				return {
					ok: true,
					status: 200,
					text: async () => {
						throw new Error("The operation was aborted");
					},
				} as unknown as Response;
			}
			return Response.json({ workspace_id: WORKSPACE_ID, items: [], relations: [], projects: [] });
		},
	);
	await expect(client.tree()).resolves.toMatchObject({ workspace_id: WORKSPACE_ID });
	expect(calls).toBe(2);
});
