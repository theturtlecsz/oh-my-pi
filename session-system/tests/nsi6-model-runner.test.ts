import { Database } from "bun:sqlite";
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { StreamFn } from "@oh-my-pi/pi-agent-core";
import type { AssistantMessage, Context, Usage } from "@oh-my-pi/pi-ai";
import { AssistantMessageEventStream } from "@oh-my-pi/pi-ai/utils/event-stream";
import { calculateUsageCost, getBundledModel } from "@oh-my-pi/pi-catalog/models";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { AuthStorage, SqliteAuthCredentialStore } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { sha256Hex } from "@oh-my-pi/pi-work-client";
import {
	type IntakeRunCase,
	type IntakeRunRequest,
	parseIntakeRunRequest,
	runIntake,
} from "../extensions/workflow/intake-runner";

const SOURCE = "Pin the stats export to JSON. Keep the human table. An automated test must pass.";
const MODEL = "openai/gpt-4o-mini";

/** Fixture-shaped input carrying structured claims the model must never be shown. */
function evaluatorInput(): IntakeRunCase["input"] {
	return {
		bounded: {
			draft: {
				source: { text: SOURCE, sha256: sha256Hex(SOURCE), spans: [] },
				goal: { id: "LEAKED-FIXTURE-GOAL", statement: "LEAKED-FIXTURE-STATEMENT" },
			},
		},
		nsi2_proof_scenario: { kind: "LEAKED-PROOF-RECIPE" },
	};
}

function caseOf(id: string, arm: "current" | "typed"): IntakeRunCase {
	return { id, arm, input: evaluatorInput() };
}

const GOOD_REPLY = JSON.stringify({
	draft: {
		spans: [
			{ id: "span-export", quote: "Pin the stats export to JSON." },
			{ id: "span-test", quote: "An automated test must pass." },
		],
		goal: { id: "goal-1", statement: "Pin the stats export to JSON", source_span_ids: ["span-export"] },
		constraints: [
			{
				id: "c-json",
				statement: "The export encoding is JSON",
				source_span_ids: ["span-export"],
				key: "output_encoding",
				value: { kind: "known", value: "json" },
				polarity: "positive",
			},
		],
		unknowns: [
			{
				id: "u-flag",
				statement: "Whether the owner wants a flag name",
				source_span_ids: [],
				kind: "authority_or_dependency",
				material: true,
			},
		],
		acceptance_criteria: [
			{
				id: "ac-test",
				statement: "An automated test passes",
				source_span_ids: ["span-test"],
				observable_outcome: "the export test passes",
				oracle: "automated_test",
			},
		],
	},
	findings: [{ kind: "unresolved_authority", statement: "Flag name is not settled", claim_ids: ["u-flag"] }],
});

interface Scripted {
	text?: string;
	usage?: Partial<Usage>;
	stopReason?: "stop" | "error";
	errorMessage?: string;
}

/** Provider stand-in beneath the cap gate: records every request that reaches transport. */
function scriptedTransport(script: Scripted[]): { stream: StreamFn; contexts: Context[] } {
	const contexts: Context[] = [];
	const stream: StreamFn = (model, context) => {
		contexts.push(structuredClone(context));
		const step = script[contexts.length - 1];
		if (!step) throw new Error(`unscripted provider request #${contexts.length}`);
		const out = new AssistantMessageEventStream();
		const usage: Usage = {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
			...step.usage,
		};
		const message: AssistantMessage = {
			role: "assistant",
			content: step.text === undefined ? [] : [{ type: "text", text: step.text }],
			api: model.api,
			provider: model.provider,
			model: model.id,
			usage,
			stopReason: step.stopReason ?? "stop",
			errorMessage: step.errorMessage,
			timestamp: Date.now(),
		};
		queueMicrotask(() => {
			if (message.stopReason === "error") out.push({ type: "error", reason: "error", error: message });
			else out.push({ type: "done", reason: "stop", message });
		});
		return out;
	};
	return { stream, contexts };
}

function priced(usage: Pick<Usage, "input" | "output">): number {
	const model = getBundledModel("openai", "gpt-4o-mini");
	return calculateUsageCost(model.cost, {
		...usage,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 0,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	}).total;
}

describe("NSI-6 native drafting runner", () => {
	let tempDir: string;
	let authStorage: AuthStorage;
	let modelRegistry: ModelRegistry;

	beforeEach(() => {
		tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "nsi6-model-runner-"));
		authStorage = new AuthStorage(new SqliteAuthCredentialStore(new Database(":memory:")));
		authStorage.keys.setRuntime("openai", "test-key");
		modelRegistry = new ModelRegistry(authStorage, path.join(tempDir, "models.yml"));
	});

	afterEach(() => {
		fs.rmSync(tempDir, { recursive: true, force: true });
	});

	function run(cases: IntakeRunCase[], transport: StreamFn, maxUsd = 1) {
		const request: IntakeRunRequest = { model: MODEL, max_usd: maxUsd, cases };
		return runIntake(request, { modelRegistry, agentDir: tempDir, transport });
	}

	test("drafts from source text alone, isolates arms and records measured usage", async () => {
		const usage = { input: 1200, output: 300 };
		const { stream, contexts } = scriptedTransport([
			{ text: GOOD_REPLY, usage },
			{ text: GOOD_REPLY, usage },
		]);
		const report = await run([caseOf("a-current", "current"), caseOf("a-typed", "typed")], stream);

		expect(contexts).toHaveLength(2);
		for (const context of contexts) {
			const wire = JSON.stringify(context);
			expect(wire).toContain(SOURCE);
			expect(wire).not.toContain("LEAKED-");
			expect(context.tools ?? []).toEqual([]);
			expect(context.messages).toHaveLength(1);
		}
		const systems = contexts.map(context => (context.systemPrompt ?? []).join("\n"));
		expect(systems[0]).toContain("Existing plan/draft");
		expect(systems[0]).toContain("ONE JSON object");
		expect(systems[0]).not.toContain("Typed completeness guidance");
		expect(systems[1]).toContain("Typed completeness guidance");

		const [current, typed] = report.cases;
		expect(current!.status).toBe("ok");
		expect(current!.draft!.source.sha256).toBe(sha256Hex(SOURCE));
		const exportSpan = current!.draft!.source.spans.find(span => span.id === "span-export")!;
		expect(SOURCE.slice(exportSpan.start, exportSpan.end)).toBe("Pin the stats export to JSON.");
		expect(exportSpan.exact_text_sha256).toBe(sha256Hex("Pin the stats export to JSON."));
		expect(current!.findings).toEqual([
			{ kind: "unresolved_authority", statement: "Flag name is not settled", claim_ids: ["u-flag"] },
		]);
		expect(current!.completions).toEqual([GOOD_REPLY]);

		expect(current!.usage).toMatchObject({ requests: 1, denied_requests: 0, measured: true });
		expect(current!.usage.tokens).toMatchObject({ input: 1200, output: 300, total: 1500 });
		expect(current!.usage.usd).toBeCloseTo(priced(usage), 12);
		expect(current!.model.resolved).toBe(MODEL);
		expect(current!.hashes.typed_completeness_sha256).toBeNull();
		expect(typed!.hashes.typed_completeness_sha256).not.toBeNull();
		expect(typed!.hashes.system_prompt_sha256).not.toBe(current!.hashes.system_prompt_sha256);
		expect(typed!.hashes.skill_sha256).toBe(current!.hashes.skill_sha256);
		expect(report.spent_usd).toBeCloseTo(2 * priced(usage), 12);
		expect(report.cap_usd).toBe(1);
		expect(report.requests).toBe(2);
	});

	test("denies the next request before transport once the remaining cap cannot cover it", async () => {
		// One large reply settles near the cap; the next worst-case bound no longer fits.
		const big = { input: 1000, output: 16000 };
		const { stream, contexts } = scriptedTransport([{ text: GOOD_REPLY, usage: big }, { text: GOOD_REPLY, usage: big }]);
		const cap = priced(big) * 1.5;
		const report = await run([caseOf("first", "current"), caseOf("second", "typed")], stream, cap);

		expect(contexts).toHaveLength(1);
		const [first, second] = report.cases;
		expect(first!.status).toBe("ok");
		expect(second!.status).toBe("cap_denied");
		expect(second!.draft).toBeNull();
		expect(second!.usage).toMatchObject({ requests: 0, denied_requests: 1, usd: 0 });
		expect(report.denied_requests).toBe(1);
		expect(report.spent_usd).toBeCloseTo(priced(big), 12);
		expect(report.spent_usd).toBeLessThanOrEqual(cap);
	});

	test("a cap below one request's worst case sends nothing", async () => {
		const { stream, contexts } = scriptedTransport([{ text: GOOD_REPLY, usage: { input: 10, output: 10 } }]);
		const report = await run([caseOf("only", "current")], stream, 1e-6);
		expect(contexts).toHaveLength(0);
		expect(report.cases[0]!.status).toBe("cap_denied");
		expect(report.spent_usd).toBe(0);
	});

	test("malformed reply is repaired through the same gate and spend of both calls is kept", async () => {
		const first = { input: 900, output: 40 };
		const second = { input: 1100, output: 320 };
		const { stream, contexts } = scriptedTransport([
			{ text: "Sure! Here is your draft.", usage: first },
			{ text: GOOD_REPLY, usage: second },
		]);
		const report = await run([caseOf("repair", "current")], stream);
		const [result] = report.cases;
		expect(contexts).toHaveLength(2);
		expect(JSON.stringify(contexts[1]!.messages)).toContain("not valid JSON");
		expect(result!.status).toBe("ok");
		expect(result!.completions).toHaveLength(2);
		expect(result!.usage.requests).toBe(2);
		expect(result!.usage.usd).toBeCloseTo(priced(first) + priced(second), 12);
	});

	test("unrecoverable malformed replies end invalid and retain incurred spend", async () => {
		const usage = { input: 900, output: 40 };
		const { stream } = scriptedTransport([
			{ text: '{"draft": {"spans": []}}', usage },
			{ text: "still not json", usage },
		]);
		const report = await run([caseOf("bad", "typed")], stream);
		const [result] = report.cases;
		expect(result!.status).toBe("invalid");
		expect(result!.draft).toBeNull();
		expect(result!.completions).toEqual(['{"draft": {"spans": []}}', "still not json"]);
		expect(report.spent_usd).toBeCloseTo(2 * priced(usage), 12);
	});

	test("a repair the cap cannot cover is denied and the first call's spend stays on the books", async () => {
		const big = { input: 1000, output: 16000 };
		const { stream, contexts } = scriptedTransport([{ text: "nope", usage: big }, { text: GOOD_REPLY, usage: big }]);
		const report = await run([caseOf("capped-repair", "current")], stream, priced(big) * 1.5);
		const [result] = report.cases;
		expect(contexts).toHaveLength(1);
		expect(result!.status).toBe("cap_denied");
		expect(result!.usage).toMatchObject({ requests: 1, denied_requests: 1 });
		expect(report.spent_usd).toBeCloseTo(priced(big), 12);
	});

	test("failed provider call is charged at its bound and is not measured evidence", async () => {
		const { stream } = scriptedTransport([{ stopReason: "error", errorMessage: "upstream 500" }]);
		const report = await run([caseOf("boom", "current")], stream);
		const [result] = report.cases;
		expect(result!.status).toBe("failed");
		expect(result!.usage.measured).toBe(false);
		expect(result!.usage.usd).toBeGreaterThan(0);
		expect(report.spent_usd).toBe(result!.usage.usd);
	});

	test("a valid draft without reported usage is unmeasured, never ok", async () => {
		const { stream } = scriptedTransport([{ text: GOOD_REPLY }]);
		const report = await run([caseOf("silent", "current")], stream);
		const [result] = report.cases;
		expect(result!.draft).not.toBeNull();
		expect(result!.status).toBe("unmeasured");
		expect(result!.usage.measured).toBe(false);
		expect(report.spent_usd).toBeGreaterThan(0);
	});

	test("request validation rejects labels and non-finite or non-positive caps", () => {
		const base = { model: MODEL, max_usd: 1, cases: [caseOf("x", "current")] };
		expect(() => parseIntakeRunRequest(base)).not.toThrow();
		for (const cap of [0, -1, Number.NaN, Number.POSITIVE_INFINITY, "1"]) {
			expect(() => parseIntakeRunRequest({ ...base, max_usd: cap })).toThrow("max_usd");
		}
		const labelled = { ...base, cases: [{ ...caseOf("x", "current"), gold: { readiness: "NEEDS_SPEC" } }] };
		expect(() => parseIntakeRunRequest(labelled)).toThrow("labels are never accepted");
		const leaky = { ...base, cases: [{ ...caseOf("x", "current"), input: { ...evaluatorInput(), classes: ["x"] } }] };
		expect(() => parseIntakeRunRequest(leaky)).toThrow("not an evaluator_input field");
		expect(() => parseIntakeRunRequest({ ...base, cases: [caseOf("x", "current"), caseOf("x", "typed")] })).toThrow(
			"duplicate case id",
		);
	});
});
