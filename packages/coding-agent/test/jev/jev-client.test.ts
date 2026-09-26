import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { decide, type JevQuestions, type JevUsageEntry } from "@oh-my-pi/pi-coding-agent/tiny/jev-client";
import { type StubJevServer, startStubJevServer } from "./stub-jev-server";

describe("JevClient", () => {
	let stub: StubJevServer;
	let entries: JevUsageEntry[];

	const questions: JevQuestions = {
		primary_type: {
			type: "choice",
			instructions: "What is the primary classification?",
			options: ["bug", "enhancement", "question"],
		},
		batch_audit: {
			type: "noul",
			instructions: "Is this automated?",
		},
	};

	beforeEach(() => {
		entries = [];
		stub = startStubJevServer();
	});

	afterEach(() => {
		stub.stop();
	});

	function makeDeps(overrides: { enabled?: boolean; apiKey?: string; baseUrl?: string } = {}) {
		return {
			recordUsage: (entry: JevUsageEntry) => {
				entries.push(entry);
			},
			getSetting: (path: string) => {
				if (path === "jev.enabled") return overrides.enabled ?? true;
				if (path === "jev.baseUrl") return overrides.baseUrl ?? stub.baseUrl;
				return undefined;
			},
			getApiKey: () => overrides.apiKey ?? "test-api-key",
		};
	}

	it("returns stub answers and records 1 usage entry on ok", async () => {
		stub.setMode("ok");
		const answers = await decide("test state content", questions, makeDeps());

		expect(answers).toBeDefined();
		expect(answers?.primary_type).toBeDefined();
		expect("probabilities" in answers!.primary_type).toBe(true);
		expect(answers?.batch_audit).toBeDefined();
		expect("probability" in answers!.batch_audit).toBe(true);

		expect(entries).toHaveLength(1);
		expect(entries[0].outcome).toBe("ok");
		expect(entries[0].attempt).toBe(1);
		expect(entries[0].serverId).toBe("stub-jev-srv-1");
		expect(entries[0].stateChars).toBe("test state content".length);
		expect(entries[0].truncated).toBe(false);

		expect(stub.requests).toHaveLength(1);
		expect(stub.requests[0].headers.get("X-Request-Id")).toBeTruthy();
		expect(stub.requests[0].headers.get("Authorization")).toBe("Bearer test-api-key");
		const payload = stub.requests[0].body as { model: string; state: string; questions: unknown };
		expect(payload.model).toBe("jev-latest");
		expect(payload.state).toBe("test state content");
	});

	it("retries once on 500 and records 2 usage entries", async () => {
		stub.setMode("500");
		const answers = await decide("test state content", questions, makeDeps());

		expect(answers).toBeUndefined();
		expect(entries).toHaveLength(2);
		expect(entries[0].attempt).toBe(1);
		expect(entries[0].outcome).toBe("http_error");
		expect(entries[1].attempt).toBe(2);
		expect(entries[1].outcome).toBe("http_error");

		expect(stub.requests).toHaveLength(2);
		const req1Id = stub.requests[0].headers.get("X-Request-Id");
		const req2Id = stub.requests[1].headers.get("X-Request-Id");
		expect(req1Id).toBeTruthy();
		expect(req2Id).toBeTruthy();
		expect(req1Id).not.toBe(req2Id);
	});

	it("times out on delay in <=3.2s with 1 request and no retry", async () => {
		stub.setMode("delay");
		stub.setDelayMs(4000);

		const started = Date.now();
		const answers = await decide("test state content", questions, makeDeps());
		const elapsedMs = Date.now() - started;

		expect(answers).toBeUndefined();
		expect(elapsedMs).toBeLessThanOrEqual(3200);
		expect(stub.requests).toHaveLength(1);

		expect(entries).toHaveLength(1);
		expect(entries[0].attempt).toBe(1);
		expect(entries[0].outcome).toBe("timeout");
	});

	it("returns undefined on malformed response with 1 request and no retry", async () => {
		stub.setMode("malformed");
		const answers = await decide("test state content", questions, makeDeps());

		expect(answers).toBeUndefined();
		expect(stub.requests).toHaveLength(1);
		expect(entries).toHaveLength(1);
		expect(entries[0].attempt).toBe(1);
		expect(entries[0].outcome).toBe("malformed");
	});

	it("returns undefined on off-list response with 1 request and no retry", async () => {
		stub.setMode("off-list");
		const answers = await decide("test state content", questions, makeDeps());

		expect(answers).toBeUndefined();
		expect(stub.requests).toHaveLength(1);
		expect(entries).toHaveLength(1);
		expect(entries[0].attempt).toBe(1);
		expect(entries[0].outcome).toBe("off_list");
	});

	it("truncates 9000-char state with marker", async () => {
		stub.setMode("ok");
		const longState = "A".repeat(9000);
		const answers = await decide(longState, questions, makeDeps());

		expect(answers).toBeDefined();
		expect(stub.requests).toHaveLength(1);

		const payload = stub.requests[0].body as { state: string };
		const marker = "…[truncated 1000 chars]";
		expect(payload.state).toHaveLength(8000 + marker.length);
		expect(payload.state.startsWith("A".repeat(8000))).toBe(true);
		expect(payload.state.endsWith(marker)).toBe(true);

		expect(entries).toHaveLength(1);
		expect(entries[0].stateChars).toBe(9000);
		expect(entries[0].truncated).toBe(true);
	});

	it("sends 0 requests when jev.enabled is false", async () => {
		const answers = await decide("test state content", questions, makeDeps({ enabled: false }));

		expect(answers).toBeUndefined();
		expect(stub.requests).toHaveLength(0);
		expect(entries).toHaveLength(0);
	});

	it("sends 0 requests when api key is missing", async () => {
		const answers = await decide("test state content", questions, {
			recordUsage: (entry: JevUsageEntry) => {
				entries.push(entry);
			},
			getSetting: (path: string) => {
				if (path === "jev.enabled") return true;
				if (path === "jev.baseUrl") return stub.baseUrl;
				return undefined;
			},
			getApiKey: () => undefined,
		});

		expect(answers).toBeUndefined();
		expect(stub.requests).toHaveLength(0);
		expect(entries).toHaveLength(0);
	});
});
