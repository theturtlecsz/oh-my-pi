import { describe, expect, test } from "bun:test";
import { payloadHash, type CloseAttempt, type WorkflowView } from "@oh-my-pi/pi-work-client";
import {
	stageContextLines,
	type StageContextInput,
	type StageContextRunner,
} from "../extensions/workflow/stage-context";

function makeMinimalView(overrides: Partial<WorkflowView> = {}): WorkflowView {
	return {
		item: {
			work_id: "work-1",
			workspace_id: "ws-1",
			alias: { key: "OMP-100", primary: true },
			state: "IN_PROGRESS",
			revision: {
				revision_id: "rev-1",
				title: "Implement feature",
				description: "Desc",
				acceptance_criteria: [],
				created_at: "2026-09-27T00:00:00Z",
			},
			candidate: {
				candidate_id: "cand-1",
				candidate_sha256: "cand-sha",
				created_at: "2026-09-27T00:00:00Z",
			},
			project_id: null,
			archived: false,
		},
		relations: [],
		receipts: [],
		close_attempts: [],
		audit_manifest: null,
		auditor_launches: [],
		close_attempt_events: [],
		checkpoint_deliveries: [],
		project: null,
		...overrides,
	};
}

describe("stageContextLines", () => {
	const defaultInput: StageContextInput = {
		key: "OMP-100",
		cwd: "/repo/test",
	};

	test("unset env -> [], fetch and run not called", async () => {
		let workflowCalled = false;
		let runCalled = false;

		const lines = await stageContextLines(defaultInput, {
			workflow: async () => {
				workflowCalled = true;
				return makeMinimalView();
			},
			liveAttempt: () => undefined,
			env: {},
			run: async () => {
				runCalled = true;
				return { exitCode: 0, stdout: "{}", stderr: "" };
			},
		});

		expect(lines).toEqual([]);
		expect(workflowCalled).toBe(false);
		expect(runCalled).toBe(false);
	});

	test("plan view sends the right stage, attempt_id and fetched view on stdin", async () => {
		const view = makeMinimalView({
			close_attempts: [],
			receipts: [],
		});

		let capturedArgv: string[] = [];
		let capturedStdin: string = "";

		const text = "FLEET CONTEXT v1\n\n## exact\n- rev";
		// sha256(canonical_json(text)) from omp_work.v1.canonical — not the raw UTF-8 digest.
		const sha = "8ac1238c4ecfe209b1c2e8c1ade80df0d2e16f60d69411012a259d2c7f49c489";
		const fakeRunner: StageContextRunner = async (argv, stdin) => {
			capturedArgv = argv;
			capturedStdin = stdin;
			return {
				exitCode: 0,
				stdout: JSON.stringify({
					bundle_id: "b-plan",
					bundle_sha256: sha,
					stage: "plan",
					tokens: 42,
					token_budget: 1000,
					exclusions: [],
					text,
				}),
				stderr: "",
			};
		};

		const baseCmd = ["uv", "run", "--project", "/opt/omp-knowledge", "python", "-m", "omp_knowledge.context", "--state-dir", "/tmp/state"];
		const lines = await stageContextLines(defaultInput, {
			workflow: async key => {
				expect(key).toBe("OMP-100");
				return view;
			},
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(baseCmd) },
			run: fakeRunner,
		});

		// `--state-dir` exists only on the compile subparser, so it follows `compile`.
		expect(capturedArgv).toEqual([
			"uv",
			"run",
			"--project",
			"/opt/omp-knowledge",
			"python",
			"-m",
			"omp_knowledge.context",
			"compile",
			"--json",
			"--state-dir",
			"/tmp/state",
		]);

		const parsedStdin = JSON.parse(capturedStdin);
		expect(parsedStdin.stage).toBe("plan");
		expect(parsedStdin.attempt_id).toBe("pre-close-0");
		expect(parsedStdin.cwd).toBe("/repo/test");
		expect(parsedStdin.workflow).toEqual(view);

		expect(lines).toEqual([
			`STAGE CONTEXT plan bundle=b-plan sha256=${sha} tokens=42/1000 excluded=0`,
			"FLEET CONTEXT v1",
			"",
			"## exact",
			"- rev",
		]);
	});

	test("splices compile ahead of flags when the command has no module token", async () => {
		let capturedArgv: string[] = [];
		const fakeRunner: StageContextRunner = async argv => {
			capturedArgv = argv;
			return { exitCode: 2, stdout: "", stderr: "" };
		};

		const baseCmd = ["context-cli", "--state-dir", "/tmp/state", "--token-budget", "4000"];
		await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(baseCmd) },
			run: fakeRunner,
		});

		expect(capturedArgv).toEqual([
			"context-cli",
			"compile",
			"--json",
			"--state-dir",
			"/tmp/state",
			"--token-budget",
			"4000",
		]);
	});

	test("reuses an existing compile subcommand instead of duplicating it", async () => {
		let capturedArgv: string[] = [];
		const fakeRunner: StageContextRunner = async argv => {
			capturedArgv = argv;
			return { exitCode: 2, stdout: "", stderr: "" };
		};

		const baseCmd = ["python", "-m", "omp_knowledge.context", "compile", "--state-dir", "/tmp/state", "--encoding", "cl100k"];
		await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(baseCmd) },
			run: fakeRunner,
		});

		expect(capturedArgv).toEqual([
			"python",
			"-m",
			"omp_knowledge.context",
			"compile",
			"--json",
			"--state-dir",
			"/tmp/state",
			"--encoding",
			"cl100k",
		]);
	});

	test("implement view sends the right stage, attempt_id and fetched view on stdin", async () => {
		const revId = "rev-target";
		const candId = "cand-target";
		const view = makeMinimalView({
			item: {
				work_id: "work-1",
				workspace_id: "ws-1",
				alias: { key: "OMP-100", primary: true },
				state: "IN_PROGRESS",
				revision: {
					revision_id: revId,
					title: "Implement feature",
					description: "Desc",
					acceptance_criteria: [],
					created_at: "2026-09-27T00:00:00Z",
				},
				candidate: {
					candidate_id: candId,
					candidate_sha256: "cand-sha",
					created_at: "2026-09-27T00:00:00Z",
				},
				project_id: null,
				archived: false,
			},
			receipts: [
				{
					receipt_id: "rec-1",
					work_id: "work-1",
					revision_id: revId,
					candidate_id: candId,
					kind: "plan",
					payload: { body: "Plan body" },
					payload_sha256: "pay-sha",
					issuer: "test",
					issued_at: "2026-09-27T00:00:00Z",
					independent: false,
				},
			],
			close_attempts: [
				{
					attempt_id: "att-old-1",
					work_id: "work-1",
					revision_id: revId,
					candidate_id: "cand-old",
					requested_at: "2026-09-26T00:00:00Z",
					state: "remediation_required",
					events: [],
				},
				{
					attempt_id: "att-old-2",
					work_id: "work-1",
					revision_id: revId,
					candidate_id: "cand-old",
					requested_at: "2026-09-26T12:00:00Z",
					state: "blocked",
					events: [],
				},
			],
		});

		let capturedStdin: string = "";
		const fakeRunner: StageContextRunner = async (_argv, stdin) => {
			capturedStdin = stdin;
			const text = "FLEET CONTEXT v1\n\n## structural\n- file.ts:1";
			const sha = payloadHash(text);
			return {
				exitCode: 0,
				stdout: JSON.stringify({
					bundle_id: "b-impl",
					bundle_sha256: sha,
					stage: "implement",
					tokens: 88,
					token_budget: 2000,
					exclusions: [{ ref: "ex-1" }],
					text,
				}),
				stderr: "",
			};
		};

		await stageContextLines(defaultInput, {
			workflow: async () => view,
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx-cmd"]) },
			run: fakeRunner,
		});

		const parsedStdin = JSON.parse(capturedStdin);
		expect(parsedStdin.stage).toBe("implement");
		expect(parsedStdin.attempt_id).toBe("pre-close-2");
		expect(parsedStdin.cwd).toBe("/repo/test");
		expect(parsedStdin.workflow).toEqual(view);
	});

	test("audit view sends the right stage, attempt_id and fetched view on stdin", async () => {
		const liveAtt: CloseAttempt = {
			attempt_id: "att-live-99",
			work_id: "work-1",
			revision_id: "rev-1",
			candidate_id: "cand-1",
			requested_at: "2026-09-27T10:00:00Z",
			state: "audit_ready",
			events: [],
		};

		const view = makeMinimalView({
			close_attempts: [liveAtt],
		});

		let capturedStdin: string = "";
		const fakeRunner: StageContextRunner = async (_argv, stdin) => {
			capturedStdin = stdin;
			const text = "FLEET CONTEXT v1\n\n## identity\nwork_id: work-1";
			const sha = payloadHash(text);
			return {
				exitCode: 0,
				stdout: JSON.stringify({
					bundle_id: "b-audit",
					bundle_sha256: sha,
					stage: "audit",
					tokens: 120,
					token_budget: 3000,
					exclusions: [],
					text,
				}),
				stderr: "",
			};
		};

		await stageContextLines(defaultInput, {
			workflow: async () => view,
			liveAttempt: () => liveAtt,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx-cmd"]) },
			run: fakeRunner,
		});

		const parsedStdin = JSON.parse(capturedStdin);
		expect(parsedStdin.stage).toBe("audit");
		expect(parsedStdin.attempt_id).toBe("att-live-99");
		expect(parsedStdin.cwd).toBe("/repo/test");
		expect(parsedStdin.workflow).toEqual(view);
	});

	test("good output -> header line then text lines verbatim", async () => {
		const text = "FLEET CONTEXT v1\n\n## structural\n- mod: line 1\n- mod: line 2\n\n--- identity ---\nstage: implement";
		const sha = payloadHash(text);

		const fakeRunner: StageContextRunner = async () => ({
			exitCode: 0,
			stdout: JSON.stringify({
				bundle_id: "bundle-456",
				bundle_sha256: sha,
				stage: "implement",
				tokens: 250,
				token_budget: 4000,
				exclusions: [{ ref: "e1" }, { ref: "e2" }],
				text,
			}),
			stderr: "",
		});

		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["compile-context"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual([
			`STAGE CONTEXT implement bundle=bundle-456 sha256=${sha} tokens=250/4000 excluded=2`,
			"FLEET CONTEXT v1",
			"",
			"## structural",
			"- mod: line 1",
			"- mod: line 2",
			"",
			"--- identity ---",
			"stage: implement",
		]);
	});

	test("raw utf-8 digest is a sha mismatch", async () => {
		const text = "FLEET CONTEXT v1\n\n## exact\n- rev";
		const raw = new Bun.CryptoHasher("sha256").update(text, "utf8").digest("hex");

		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx"]) },
			run: async () => ({
				exitCode: 0,
				stdout: JSON.stringify({
					bundle_id: "b-raw",
					bundle_sha256: raw,
					stage: "plan",
					tokens: 10,
					token_budget: 1000,
					exclusions: [],
					text,
				}),
				stderr: "",
			}),
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (sha mismatch)"]);
	});

	test("sha mismatch -> one unavailable line only", async () => {
		const text = "FLEET CONTEXT v1\nline1";
		const wrongSha = "0000000000000000000000000000000000000000000000000000000000000000";

		const fakeRunner: StageContextRunner = async () => ({
			exitCode: 0,
			stdout: JSON.stringify({
				bundle_id: "b-mismatch",
				bundle_sha256: wrongSha,
				stage: "plan",
				tokens: 10,
				token_budget: 1000,
				exclusions: [],
				text,
			}),
			stderr: "",
		});

		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (sha mismatch)"]);
	});

	test("exit 2 -> one unavailable line only", async () => {
		const fakeRunner: StageContextRunner = async () => ({
			exitCode: 2,
			stdout: "",
			stderr: "budget_insufficient: mandatory context needs 500 tokens, budget is 200",
		});

		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (exit 2)"]);
	});

	test("timeout -> one unavailable line only", async () => {
		const fakeRunner: StageContextRunner = async () => {
			throw new Error("timeout");
		};

		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (timeout)"]);
	});

	test("bad JSON in stdout -> one unavailable line only", async () => {
		const fakeRunner: StageContextRunner = async () => ({
			exitCode: 0,
			stdout: "not-json-at-all",
			stderr: "",
		});

		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (bad JSON)"]);
	});

	test("bad JSON in env -> one unavailable line only", async () => {
		let runCalled = false;
		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: "invalid-json[" },
			run: async () => {
				runCalled = true;
				return { exitCode: 0, stdout: "{}", stderr: "" };
			},
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (bad JSON)"]);
		expect(runCalled).toBe(false);
	});

	test("non-array JSON in env -> one unavailable line only", async () => {
		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify({ not: "an array" }) },
			run: async () => ({ exitCode: 0, stdout: "{}", stderr: "" }),
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (bad JSON)"]);
	});

	test("malformed JSON payload missing required fields -> one unavailable line only", async () => {
		const fakeRunner: StageContextRunner = async () => ({
			exitCode: 0,
			stdout: JSON.stringify({ bundle_id: "b1" }), // missing bundle_sha256, text, etc.
			stderr: "",
		});

		const lines = await stageContextLines(defaultInput, {
			workflow: async () => makeMinimalView(),
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (bad JSON)"]);
	});

	test("workflow fetch failure -> one unavailable line only and never throws", async () => {
		let runCalled = false;
		const lines = await stageContextLines(defaultInput, {
			workflow: async () => {
				throw new Error("connection reset by peer");
			},
			liveAttempt: () => undefined,
			env: { OMP_KNOWLEDGE_CONTEXT_CMD: JSON.stringify(["ctx"]) },
			run: async () => {
				runCalled = true;
				return { exitCode: 0, stdout: "{}", stderr: "" };
			},
		});

		expect(lines).toEqual(["STAGE CONTEXT: unavailable (connection reset by peer)"]);
		expect(runCalled).toBe(false);
	});
});
