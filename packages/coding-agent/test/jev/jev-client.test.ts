import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import {
	choice,
	createJevBreaker,
	decide,
	type JevBreaker,
	type JevQuestions,
	type JevUsageEntry,
	resetDefaultJevBreaker,
	yesno,
} from "@oh-my-pi/pi-coding-agent/tiny/jev-client";
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
		resetDefaultJevBreaker();
	});

	afterEach(() => {
		stub.stop();
	});

	function makeDeps(
		overrides: {
			enabled?: boolean;
			apiKey?: string;
			baseUrl?: string;
			breaker?: JevBreaker;
			now?: () => number;
		} = {},
	) {
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
			breaker: overrides.breaker,
			now: overrides.now,
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

	it("opens circuit breaker after 2 consecutive failed calls and resolves in <50ms without new requests", async () => {
		stub.setMode("500");
		const breaker = createJevBreaker();
		const deps = makeDeps({ breaker });

		// Call 1 fails (attempts 1 & 2 hit stub)
		const res1 = await decide("test state", questions, deps);
		expect(res1).toBeUndefined();
		expect(stub.requests).toHaveLength(2);
		expect(entries).toHaveLength(2);

		// Call 2 fails (attempts 1 & 2 hit stub)
		const res2 = await decide("test state", questions, deps);
		expect(res2).toBeUndefined();
		expect(stub.requests).toHaveLength(4);
		expect(entries).toHaveLength(4);

		// Call 3: breaker is open; stub records no new request, call resolves undefined in <50ms
		const start = Date.now();
		const res3 = await decide("test state", questions, deps);
		const elapsedMs = Date.now() - start;

		expect(res3).toBeUndefined();
		expect(stub.requests).toHaveLength(4);
		expect(elapsedMs).toBeLessThan(50);
		expect(entries).toHaveLength(4);
	});

	it("reaches stub again after 60s cooldown with injected now", async () => {
		stub.setMode("500");
		let currentTime = 1_000_000;
		const now = () => currentTime;
		const breaker = createJevBreaker(now);
		const deps = makeDeps({ breaker, now });

		// Two failed calls open breaker
		await decide("test state", questions, deps);
		expect(stub.requests).toHaveLength(2);
		await decide("test state", questions, deps);
		expect(stub.requests).toHaveLength(4);

		// Breaker is open
		expect(breaker.isOpen()).toBe(true);
		const blocked = await decide("test state", questions, deps);
		expect(blocked).toBeUndefined();
		expect(stub.requests).toHaveLength(4);

		// Advance time by 60s
		currentTime += 60_000;
		stub.setMode("ok");

		// Next call goes through and reaches stub again
		const res = await decide("test state", questions, deps);
		expect(res).toBeDefined();
		expect(stub.requests).toHaveLength(5);
		expect(breaker.isOpen()).toBe(false);
	});

	it("keeps circuit breaker closed when a success occurs between failures", async () => {
		const breaker = createJevBreaker();
		const deps = makeDeps({ breaker });

		// Call 1 fails (500)
		stub.setMode("500");
		await decide("test state", questions, deps);
		expect(stub.requests).toHaveLength(2);

		// Call 2 succeeds (ok) -> resets failure count
		stub.setMode("ok");
		const res2 = await decide("test state", questions, deps);
		expect(res2).toBeDefined();
		expect(stub.requests).toHaveLength(3);

		// Call 3 fails (500) -> failure count is 1, not 2
		stub.setMode("500");
		await decide("test state", questions, deps);
		expect(stub.requests).toHaveLength(5);
		expect(breaker.isOpen()).toBe(false);

		// Call 4 succeeds (ok) -> reaches stub, breaker remained closed
		stub.setMode("ok");
		const res4 = await decide("test state", questions, deps);
		expect(res4).toBeDefined();
		expect(stub.requests).toHaveLength(6);
		expect(breaker.isOpen()).toBe(false);
	});

	it("choice wrapper returns stub probabilities for given options", async () => {
		stub.setMode("ok");
		const options = ["bug", "enhancement", "question"];
		const probs = await choice("What is the primary classification?", options, "test state content", makeDeps());

		expect(probs).toBeDefined();
		expect(probs?.bug).toBeCloseTo(0.8);
		expect(probs?.enhancement).toBeCloseTo(0.1);
		expect(probs?.question).toBeCloseTo(0.1);

		expect(stub.requests).toHaveLength(1);
		const payload = stub.requests[0].body as {
			model: string;
			state: string;
			questions: Record<string, { type: string; instructions: string; options: string[] }>;
		};
		expect(payload.model).toBe("jev-latest");
		expect(payload.state).toBe("test state content");
		expect(payload.questions.choice).toBeDefined();
		expect(payload.questions.choice.type).toBe("choice");
		expect(payload.questions.choice.instructions).toBe("What is the primary classification?");
		expect(payload.questions.choice.options).toEqual(options);
	});

	it("yesno wrapper returns stub probability for statement", async () => {
		stub.setMode("ok");
		const prob = await yesno("Is this automated?", "test state content", makeDeps());

		expect(prob).toBeCloseTo(0.85);

		expect(stub.requests).toHaveLength(1);
		const payload = stub.requests[0].body as {
			model: string;
			state: string;
			questions: Record<string, { type: string; instructions: string }>;
		};
		expect(payload.model).toBe("jev-latest");
		expect(payload.state).toBe("test state content");
		expect(payload.questions.statement).toBeDefined();
		expect(payload.questions.statement.type).toBe("noul");
		expect(payload.questions.statement.instructions).toBe("Is this automated?");
	});

	it("does not count disabled or missing api key calls as breaker failures", async () => {
		const breaker = createJevBreaker();
		stub.setMode("500");

		// One real failure
		await decide("state", questions, makeDeps({ breaker }));
		expect(breaker.consecutiveFailures).toBe(1);

		// Disabled call
		const disabledRes = await choice("q", ["a", "b"], "state", makeDeps({ enabled: false, breaker }));
		expect(disabledRes).toBeUndefined();
		expect(breaker.consecutiveFailures).toBe(1);

		// Missing key call
		const noKeyRes = await yesno("s", "state", {
			...makeDeps({ breaker }),
			getApiKey: () => undefined,
		});
		expect(noKeyRes).toBeUndefined();
		expect(breaker.consecutiveFailures).toBe(1);

		// Breaker remains closed
		expect(breaker.isOpen()).toBe(false);
	});
});
