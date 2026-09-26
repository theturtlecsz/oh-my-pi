/**
 * Live advisor routing builds one {@link AdvisorSupervisionPipeline} per advisor
 * from `advisor.supervisionPath` and `advisor.supervisionCanaryMaxDivergences`.
 * Every session here sets the path. The schema default is not asserted.
 */
import { afterAll, describe, expect, it } from "bun:test";
import { Agent, type AgentTool } from "@oh-my-pi/pi-agent-core";
import { createMockModel } from "@oh-my-pi/pi-ai/providers/mock";
import { getBundledModel } from "@oh-my-pi/pi-catalog/models";
import {
	ADVISOR_SUPERVISION_PATHS,
	type AdvisorSupervisionAuthority,
	type AdvisorSupervisionPath,
} from "@oh-my-pi/pi-coding-agent/advisor/supervision-pipeline";
import { ModelRegistry } from "@oh-my-pi/pi-coding-agent/config/model-registry";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { AgentSession } from "@oh-my-pi/pi-coding-agent/session/agent-session";
import { AuthStorage } from "@oh-my-pi/pi-coding-agent/session/auth-storage";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { TempDir } from "@oh-my-pi/pi-utils";

const ADVISOR_TYPE = "advisor";
const REAL_CONCERN = "Check the retry queue bounds.";
const REAL_CONCERN_VARIANT = "Check the retry queue bounds!";

interface Harness {
	session: AgentSession;
	settings: Settings;
	authStorage: AuthStorage;
}

const tempDir = TempDir.createSync("@pi-advisor-supervision-route-");

afterAll(async () => {
	await tempDir.remove();
});

function authorityFor(path: AdvisorSupervisionPath): AdvisorSupervisionAuthority {
	return path === "legacy" || path === "shadow" ? "legacy" : "structured";
}

function invocationsFor(path: AdvisorSupervisionPath, calls: number): { legacy: number; structured: number } {
	return {
		legacy: path === "structured" ? 0 : calls,
		structured: path === "legacy" ? 0 : calls,
	};
}

async function createHarness(options: {
	path: AdvisorSupervisionPath;
	canaryMaxDivergences?: number;
}): Promise<Harness> {
	const model = getBundledModel("anthropic", "claude-sonnet-4-5")!;
	const mock = createMockModel({
		responses: [{ content: ["EXACT VERDICT"], stopReason: "stop" }],
	});
	const advisorMock = createMockModel({ responses: [] });
	const agent = new Agent({
		getApiKey: () => "test-key",
		initialState: { model, systemPrompt: ["Test"], tools: [] },
		streamFn: mock.stream,
	});
	const settings = Settings.isolated({
		"compaction.enabled": false,
		"retry.enabled": false,
		...(options.canaryMaxDivergences !== undefined
			? { "advisor.supervisionCanaryMaxDivergences": options.canaryMaxDivergences }
			: {}),
	});
	// `isolated` values are overrides, which outrank `set`. The path is applied
	// with `set` so a later `set` is what the rebuilt advisor reads.
	settings.set("advisor.supervisionPath", options.path);
	settings.setModelRole("advisor", "anthropic/claude-sonnet-4-5");
	const authStorage = await AuthStorage.create(":memory:");
	authStorage.setRuntimeApiKey("anthropic", "test-key");
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
}

async function withHarness(
	options: { path: AdvisorSupervisionPath; canaryMaxDivergences?: number },
	run: (harness: Harness) => Promise<void>,
): Promise<void> {
	const harness = await createHarness(options);
	try {
		await run(harness);
	} finally {
		await harness.session.dispose();
		harness.authStorage.close();
	}
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

describe("advisor supervision routing", () => {
	it("reports the path's authority and leaves unbuilt arms at zero invocations", async () => {
		for (const path of ADVISOR_SUPERVISION_PATHS) {
			await withHarness({ path }, async ({ session }) => {
				expect(session.setAdvisorEnabled(true)).toBe(true);
				expect(await advise(session, { note: "Stop.", severity: "blocker", category: "gate-defect" })).toBe(
					"Recorded.",
				);
				expect(deliveredNotes(session)).toEqual([]);
				const [entry] = session.getAdvisorSupervisionReport();
				expect(entry?.name).toBe("default");
				expect(entry?.report.path).toBe(path);
				expect(entry?.report.authority).toBe(authorityFor(path));
				expect(entry?.report.invocations).toEqual(invocationsFor(path, 1));
				expect(entry?.report.canaryBudgetExceeded).toBe(false);
				if (path === "legacy") expect(entry?.report.classes).toEqual({});
			});
		}
	});

	it("suppresses Stop. and a punctuation-variant duplicate under legacy and structured", async () => {
		for (const path of ["legacy", "structured"] as const) {
			await withHarness({ path }, async ({ session }) => {
				await settlePrimary(session);
				expect(session.setAdvisorEnabled(true)).toBe(true);
				expect(await advise(session, { note: "Stop.", severity: "blocker", category: "gate-defect" })).toBe(
					"Recorded.",
				);
				expect(
					await advise(session, { note: REAL_CONCERN, severity: "concern", category: "semantic-concern" }),
				).toBe("Recorded.");
				expect(
					await advise(session, {
						note: REAL_CONCERN_VARIANT,
						severity: "concern",
						category: "semantic-concern",
					}),
				).toBe("Recorded.");
				expect(deliveredNotes(session)).toEqual([REAL_CONCERN]);
				const [entry] = session.getAdvisorSupervisionReport();
				expect(entry?.report.authority).toBe(authorityFor(path));
				expect(entry?.report.invocations).toEqual(invocationsFor(path, 3));
			});
		}
	});

	it("rebuilds a structured advisor onto the legacy path with a fresh arm count", async () => {
		await withHarness({ path: "structured" }, async ({ session, settings }) => {
			expect(session.setAdvisorEnabled(true)).toBe(true);
			await advise(session, { note: "Stop.", severity: "blocker", category: "gate-defect" });
			expect(session.getAdvisorSupervisionReport()[0]?.report).toMatchObject({
				path: "structured",
				authority: "structured",
				invocations: { legacy: 0, structured: 1 },
			});

			settings.set("advisor.supervisionPath", "legacy");
			expect(session.setAdvisorEnabled(false)).toBe(false);
			expect(session.setAdvisorEnabled(true)).toBe(true);
			expect(session.getAdvisorSupervisionReport()[0]?.report).toMatchObject({
				path: "legacy",
				authority: "legacy",
				invocations: { legacy: 0, structured: 0 },
			});
			await advise(session, { note: "Stop.", severity: "blocker", category: "gate-defect" });
			expect(session.getAdvisorSupervisionReport()[0]?.report.invocations).toEqual({ legacy: 1, structured: 0 });
			expect(session.getAdvisorSupervisionReport()[0]?.report.authority).toBe("legacy");
		});
	});

	it("reads the canary divergence budget when the advisor is built", async () => {
		const divergent = { note: REAL_CONCERN, severity: "concern" as const, category: "not-a-class" };
		await withHarness({ path: "canary", canaryMaxDivergences: 0 }, async ({ session }) => {
			await settlePrimary(session);
			expect(session.setAdvisorEnabled(true)).toBe(true);
			await advise(session, divergent);
			expect(session.getAdvisorSupervisionReport()[0]?.report).toMatchObject({
				path: "canary",
				authority: "legacy",
				divergences: 1,
				canaryBudgetExceeded: true,
				invocations: { legacy: 1, structured: 1 },
			});
			expect(deliveredNotes(session)).toEqual([REAL_CONCERN]);
		});
		await withHarness({ path: "canary", canaryMaxDivergences: 1 }, async ({ session }) => {
			await settlePrimary(session);
			expect(session.setAdvisorEnabled(true)).toBe(true);
			await advise(session, divergent);
			expect(session.getAdvisorSupervisionReport()[0]?.report).toMatchObject({
				path: "canary",
				authority: "structured",
				divergences: 1,
				canaryBudgetExceeded: false,
				invocations: { legacy: 1, structured: 1 },
			});
			expect(deliveredNotes(session)).toEqual([]);
		});
	});
});
