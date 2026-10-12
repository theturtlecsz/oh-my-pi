import { expect, test } from "bun:test";
import { WORK_CONTRACT_SHA256, WORK_REQUEST_ATTEMPTS, WorkClient, WorkError } from "../src/index";
import type {
	IntakeCommand,
	IntakeCommandResult,
	IntakeRecordView,
	RecordIntakeDecisionPayload,
	StartIntakeResult,
} from "../src/index";

const repo = `${import.meta.dir}/../../..`;
const contractDir = `${repo}/python/omp-work/src/omp_work/contracts`;
const pythonProject = `${repo}/python/omp-work`;

const fixtureText = await Bun.file(`${import.meta.dir}/fixtures/intake-records.json`).text();
type CommandEntry = {
	envelope: { command: { type: string; payload: Record<string, unknown> } };
	result: { type: string } & Record<string, unknown>;
};
const fixture = JSON.parse(fixtureText) as {
	commands: Record<string, CommandEntry>;
	view: unknown;
};

const RESULT_MODEL: Record<string, string> = {
	start_intake: "StartIntakeResult",
	register_intake_evidence: "RegisterIntakeEvidenceResult",
	record_intake_claim: "RecordIntakeClaimResult",
	record_intake_decision: "RecordIntakeDecisionResult",
	supersede_intake_claim: "SupersedeIntakeClaimResult",
	record_intake_issue: "RecordIntakeIssueResult",
	link_intake_criterion: "LinkIntakeCriterionResult",
	assess_intake: "AssessIntakeResult",
};

// Offline validation mirrors the service: JSON Schema for the wire shape, then
// the Pydantic models for the constraints the schema cannot express (revision
// advance, assessed_revision equality, datetime coercion). Only stdin carries
// data; the two contract directories are argv.
const validationScript = `
import json, sys
from pathlib import Path
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter
from omp_work.v1 import api_models, models

contract_dir = Path(sys.argv[1])
schema = json.loads((contract_dir / "v1/schema.json").read_text())
api_schema = json.loads((contract_dir / "v1/api-schema.json").read_text())

results = []
for task in json.load(sys.stdin):
    document = schema if task["doc"] == "schema" else api_schema
    try:
        Draft202012Validator(document["models"][task["name"]]).validate(task["value"])
        schema_ok = True
    except Exception:
        schema_ok = False
    try:
        if task["model"] == "envelope":
            models.CommandEnvelope.model_validate(task["value"])
        elif task["model"] == "result":
            TypeAdapter(api_models.CommandResult).validate_python(task["value"])
        elif task["model"] == "view":
            api_models.IntakeRecordView.model_validate(task["value"])
        model_ok = True
    except Exception:
        model_ok = False
    results.append([schema_ok, model_ok])
json.dump(results, sys.stdout)
`;

type Task = { doc: "schema" | "api"; name: string; model: "envelope" | "result" | "view"; value: unknown };

async function validate(tasks: Task[]): Promise<[boolean, boolean][]> {
	const child = Bun.spawn(["uv", "run", "--project", pythonProject, "python", "-c", validationScript, contractDir], {
		stdin: new Blob([JSON.stringify(tasks)]),
		stdout: "pipe",
		stderr: "pipe",
	});
	const stdout = await new Response(child.stdout).text();
	const stderr = await new Response(child.stderr).text();
	const exitCode = await child.exited;
	if (exitCode !== 0) throw new Error(stderr);
	return JSON.parse(stdout);
}

// Compile-time contracts: E1078 forbids implicit defaults, so every field the
// service requires must be supplied by a caller literal.
const decisionBase: RecordIntakeDecisionPayload = {
	run_id: "run-1",
	expected_revision: 1,
	decision: {
		id: "decision-1",
		run_id: "run-1",
		revision: 1,
		actor: { id: "owner-1", kind: "owner" },
		question_id: "question-1",
		answer: { type: "string", value: "99.9" },
		reason: "Owner confirmed the target.",
		created_at: "2026-10-10T00:00:00Z",
	},
	claim_ids: ["claim-1"],
	instruction: {
		text: "Require 99.9% availability.",
		provenance: { channel: "test-owner", message_ref: "message-1", received_at: "2026-10-11T00:00:00+00:00" },
	},
	superseded_decision_id: null,
	verified_relay_operation_id: null,
};
const { expected_revision: _expectedRevision, ...withoutRevision } = decisionBase;
// @ts-expect-error expected_revision is required on every follow-on payload.
const missingRevision: RecordIntakeDecisionPayload = withoutRevision;
const { superseded_decision_id: _superseded, ...withoutSuperseded } = decisionBase;
// @ts-expect-error superseded_decision_id is required and nullable, not optional.
const missingSuperseded: RecordIntakeDecisionPayload = withoutSuperseded;
const { verified_relay_operation_id: _relay, ...withoutRelay } = decisionBase;
// @ts-expect-error verified_relay_operation_id is required and nullable, not optional.
const missingRelay: RecordIntakeDecisionPayload = withoutRelay;
void [missingRevision, missingSuperseded, missingRelay];

test("intake fixture exposes eight NSI-2 commands and the revision-0 view", async () => {
	const commands = Object.keys(fixture.commands);
	expect(commands.sort()).toEqual([
		"assess_intake",
		"link_intake_criterion",
		"record_intake_claim",
		"record_intake_decision",
		"record_intake_issue",
		"register_intake_evidence",
		"start_intake",
		"supersede_intake_claim",
	]);
	for (const [kind, entry] of Object.entries(fixture.commands)) {
		expect(entry.envelope.command.type).toBe(kind);
		expect(entry.result.type).toBe(kind);
	}
	expect((fixture.view as { revision: number }).revision).toBe(0);
	expect((fixture.view as { snapshot: { state: string } }).snapshot.state).toBe("collecting");
});

test("instruction provenance keeps its +00:00 offset literal", () => {
	expect(fixtureText).toContain('"received_at": "2026-10-11T00:00:00+00:00"');
});

test("eight envelopes, results, and the view satisfy the service schemas and models", async () => {
	const tasks: Task[] = [];
	for (const [kind, entry] of Object.entries(fixture.commands)) {
		tasks.push({ doc: "schema", name: "CommandEnvelope", model: "envelope", value: entry.envelope });
		tasks.push({ doc: "api", name: RESULT_MODEL[kind], model: "result", value: entry.result });
	}
	tasks.push({ doc: "api", name: "IntakeRecordView", model: "view", value: fixture.view });
	expect(await validate(tasks)).toEqual(tasks.map(() => [true, true]));
}, 30_000);

test("assess binds revision to assessed_revision and follow-ons advance by one", async () => {
	const assess = fixture.commands.assess_intake.result as unknown as { revision: number; assessed_revision: number };
	expect([assess.revision, assess.assessed_revision]).toEqual([2, 2]);
	const start = fixture.commands.start_intake.result as unknown as { expected_revision: number; revision: number };
	expect([start.expected_revision, start.revision]).toEqual([0, 1]);
	const driftedAssess = { type: "assess_intake", run_id: "run-1", revision: 3, assessed_revision: 2 };
	const stuckStart = { type: "start_intake", run_id: "run-1", expected_revision: 1, revision: 2 };
	const zeroFollowOn = { type: "register_intake_evidence", run_id: "run-1", expected_revision: 0, revision: 1 };
	const missingRevision = {
		...fixture.commands.record_intake_decision.envelope,
		command: {
			type: "record_intake_decision",
			payload: { ...fixture.commands.record_intake_decision.envelope.command.payload, expected_revision: undefined },
		},
	};
	const extraVerdict = { ...driftedAssess, verdict: "PASS" };
	const badDatetime = {
		...fixture.commands.record_intake_decision.envelope,
		command: {
			type: "record_intake_decision",
			payload: {
				...fixture.commands.record_intake_decision.envelope.command.payload,
				instruction: {
					text: "Require 99.9% availability.",
					provenance: { channel: "test-owner", message_ref: "message-1", received_at: "not-a-timestamp" },
				},
			},
		},
	};
	const negativeView = { ...(fixture.view as object), revision: -1 };

	expect(
		await validate([
			{ doc: "api", name: "AssessIntakeResult", model: "result", value: driftedAssess },
			{ doc: "api", name: "StartIntakeResult", model: "result", value: stuckStart },
			{ doc: "api", name: "RegisterIntakeEvidenceResult", model: "result", value: zeroFollowOn },
			{ doc: "schema", name: "CommandEnvelope", model: "envelope", value: missingRevision },
			{ doc: "api", name: "AssessIntakeResult", model: "result", value: extraVerdict },
			{ doc: "schema", name: "CommandEnvelope", model: "envelope", value: badDatetime },
			{ doc: "api", name: "IntakeRecordView", model: "view", value: negativeView },
		]),
	).toEqual([
		[true, false],
		[false, false],
		[false, false],
		[false, false],
		[false, false],
		[true, false],
		[false, false],
	]);
}, 30_000);

test("exported intake command and result unions narrow on the wire type", () => {
	const command = fixture.commands.assess_intake.envelope.command as unknown as IntakeCommand;
	const result = fixture.commands.start_intake.result as unknown as IntakeCommandResult;
	const start = result as StartIntakeResult;
	expect(command.type).toBe("assess_intake");
	expect(start.expected_revision).toBe(0);
	const view = fixture.view as unknown as IntakeRecordView;
	expect(view.provenance.revision).toBe(0);
});

const OP_ID = "22222222-2222-4222-8222-222222222222";
const WS_ID = "33333333-3333-4333-8333-333333333333";
const RECEIPT = {
	operation_id: OP_ID,
	request_id: "r",
	state: "applied",
	request_sha256: "a".repeat(64),
	result_sha256: "b".repeat(64),
	diagnostics: [],
};

function recordingClient(reply: unknown): { client: WorkClient; requests: { url: string; init: RequestInit }[] } {
	const requests: { url: string; init: RequestInit }[] = [];
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		WS_ID,
		() => "token",
		async (input, init) => {
			requests.push({ url: String(input), init: init ?? {} });
			return Response.json(reply);
		},
	);
	return { client, requests };
}

const WRITES = [
	["start_intake", "startIntake"],
	["register_intake_evidence", "registerIntakeEvidence"],
	["record_intake_claim", "recordIntakeClaim"],
	["record_intake_decision", "recordIntakeDecision"],
	["supersede_intake_claim", "supersedeIntakeClaim"],
	["record_intake_issue", "recordIntakeIssue"],
	["link_intake_criterion", "linkIntakeCriterion"],
	["assess_intake", "assessIntake"],
] as const;

for (const [type, method] of WRITES) {
	test(`${method} POSTs the fixture payload untouched under the caller operation id`, async () => {
		const entry = fixture.commands[type];
		const { client, requests } = recordingClient({ receipt: RECEIPT, result: entry.result });
		const payload = entry.envelope.command.payload;
		const response = await (
			client[method] as (id: string, p: unknown) => Promise<{ receipt: unknown; result: { type: string } }>
		)(OP_ID, payload);
		expect(requests).toHaveLength(1);
		const [{ url, init }] = requests;
		expect(url).toBe("http://127.0.0.1:54322/v1/commands");
		expect(init.method).toBe("POST");
		const headers = init.headers as Record<string, string>;
		expect(headers.Authorization).toBe("Bearer token");
		expect(headers["X-OMP-Workspace-ID"]).toBe(WS_ID);
		expect(headers["X-OMP-Contract-SHA256"]).toBe(WORK_CONTRACT_SHA256);
		const sent = JSON.parse(String(init.body)) as {
			api_version: string;
			workspace_id: string;
			operation_id: string;
			request_id: string;
			correlation_id: string;
			command: { type: string; payload: Record<string, unknown> };
		};
		expect(sent.api_version).toBe("work.omp.dev/v1");
		expect(sent.workspace_id).toBe(WS_ID);
		expect(sent.operation_id).toBe(OP_ID);
		expect(sent.request_id).toMatch(/^[0-9a-f-]{36}$/);
		expect(sent.correlation_id).toMatch(/^[0-9a-f-]{36}$/);
		expect(sent.command).toEqual({ type, payload });
		expect(sent.command.payload.expected_revision).toBe(payload.expected_revision);
		if (type === "record_intake_decision") {
			const instruction = sent.command.payload.instruction as { provenance: { received_at: string } };
			expect(instruction.provenance.received_at).toBe("2026-10-11T00:00:00+00:00");
		}
		expect(response.receipt).toEqual(RECEIPT);
		expect(response.result.type).toBe(type);
	});
}

test("intake reads the record with and without a revision query", async () => {
	const { client, requests } = recordingClient(fixture.view);
	await client.intake("run/1");
	await client.intake("run/1", 2);
	expect(requests[0].url).toBe(`http://127.0.0.1:54322/v1/workspaces/${WS_ID}/intake/run%2F1`);
	expect(requests[1].url.endsWith("?revision=2")).toBe(true);
	const headers = requests[1].init.headers as Record<string, string>;
	expect(requests[1].init.method).toBe("GET");
	expect(headers.Authorization).toBe("Bearer token");
	expect(headers["X-OMP-Workspace-ID"]).toBe(WS_ID);
	expect(headers["X-OMP-Contract-SHA256"]).toBe(WORK_CONTRACT_SHA256);
});

test("missionIntake still POSTs request_id and payload to the client route", async () => {
	const { client, requests } = recordingClient({});
	await client.missionIntake(OP_ID, { goal: "x" } as never);
	expect(requests[0].url).toBe(`http://127.0.0.1:54322/v1/workspaces/${WS_ID}/client/mission-intake`);
	expect(JSON.parse(String(requests[0].init.body))).toEqual({ request_id: OP_ID, payload: { goal: "x" } });
});

// Lost-outcome reconciliation. Digests come from Python (the service's own
// command_sha256 and sha256(result)), never from the TS hashing under test.
const digestScript = `
import json, sys
from omp_work.v1 import models
from omp_work.v1.canonical import command_sha256, sha256

task = json.load(sys.stdin)
json.dump(
    {
        "request": command_sha256(models.CommandEnvelope.model_validate(task["envelope"])),
        "result": sha256(task["result"]),
    },
    sys.stdout,
)
`;

async function pythonDigests(envelope: unknown, result: unknown): Promise<{ request: string; result: string }> {
	const child = Bun.spawn(["uv", "run", "--project", pythonProject, "python", "-c", digestScript], {
		stdin: new Blob([JSON.stringify({ envelope, result })]),
		stdout: "pipe",
		stderr: "pipe",
	});
	const stdout = await new Response(child.stdout).text();
	const stderr = await new Response(child.stderr).text();
	if ((await child.exited) !== 0) throw new Error(stderr);
	return JSON.parse(stdout);
}

type LostOptions = {
	postStatus?: number;
	getStatus?: number;
	mutate?: (row: Record<string, unknown>, receipt: Record<string, unknown>) => void;
};

function lostClient(options: LostOptions = {}) {
	const posts: { operation_id: string; request_id: string }[] = [];
	let firstBody: { operation_id: string; request_id: string; command: { type: string } } | null = null;
	let gets = 0;
	const entry = fixture.commands.record_intake_decision;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		WS_ID,
		() => "token",
		async (input, init) => {
			if (init?.method === "POST") {
				const body = JSON.parse(String(init.body)) as NonNullable<typeof firstBody>;
				firstBody ??= body;
				posts.push(body);
				if (options.postStatus)
					return Response.json(
						{ error: { code: "invalid_request", diagnostics: [] } },
						{ status: options.postStatus },
					);
				throw new TypeError("socket closed");
			}
			gets++;
			expect(String(input)).toBe(`http://127.0.0.1:54322/v1/operations/${OP_ID}`);
			if (options.getStatus)
				return Response.json(
					{ error: { code: "invalid_request", diagnostics: [] } },
					{ status: options.getStatus },
				);
			const saved = firstBody as NonNullable<typeof firstBody>;
			const digests = await pythonDigests(saved, entry.result);
			const receipt: Record<string, unknown> = {
				operation_id: OP_ID,
				request_id: saved.request_id,
				state: "applied",
				request_sha256: digests.request,
				result_sha256: digests.result,
				diagnostics: [],
			};
			const row: Record<string, unknown> = {
				receipt,
				command_type: "record_intake_decision",
				request_id: saved.request_id,
				correlation_id: "c",
				result: entry.result,
			};
			options.mutate?.(row, receipt);
			return Response.json(row);
		},
	);
	return { client, posts, gets: () => gets, entry };
}

const flipHex = (hex: unknown): string => `${String(hex).startsWith("0") ? "1" : "0"}${String(hex).slice(1)}`;

test("a lost write reconciles from the stored operation after bounded POSTs and one GET", async () => {
	const { client, posts, gets, entry } = lostClient({
		mutate: (_row, receipt) => {
			delete receipt.diagnostics;
		},
	});
	const payload = entry.envelope.command.payload as unknown as RecordIntakeDecisionPayload;
	const response = await client.recordIntakeDecision(OP_ID, payload);
	expect(response.result).toEqual(entry.result as never);
	expect(response.receipt.diagnostics).toEqual([]);
	expect((response.result as unknown as { type: string }).type).toBe("record_intake_decision");
	expect(posts).toHaveLength(WORK_REQUEST_ATTEMPTS);
	expect(new Set(posts.map(post => post.operation_id))).toEqual(new Set([OP_ID]));
	expect(gets()).toBe(1);
	expect(payload.instruction.provenance.received_at).toBe("2026-10-11T00:00:00+00:00");
}, 30_000);

const REJECTED: [string, NonNullable<LostOptions["mutate"]>][] = [
	[
		"request_sha256 differs by one hex",
		(_row, receipt) => void (receipt.request_sha256 = flipHex(receipt.request_sha256)),
	],
	[
		"result_sha256 differs by one hex",
		(_row, receipt) => void (receipt.result_sha256 = flipHex(receipt.result_sha256)),
	],
	["command_type is wrong", row => void (row.command_type = "assess_intake")],
	["result is null", row => void (row.result = null)],
];
for (const [name, mutate] of REJECTED) {
	test(`a lost write is unavailable when ${name}`, async () => {
		const { client, gets, entry } = lostClient({ mutate });
		const payload = entry.envelope.command.payload as unknown as RecordIntakeDecisionPayload;
		const error = await client.recordIntakeDecision(OP_ID, payload).catch((e: unknown) => e);
		expect(error).toBeInstanceOf(WorkError);
		expect((error as WorkError).code).toBe("unavailable");
		expect(gets()).toBe(1);
	}, 30_000);
}

test("a lost write maps a GET invalid_request to unavailable", async () => {
	const { client, entry } = lostClient({ getStatus: 400 });
	const payload = entry.envelope.command.payload as unknown as RecordIntakeDecisionPayload;
	const error = await client.recordIntakeDecision(OP_ID, payload).catch((e: unknown) => e);
	expect((error as WorkError).code).toBe("unavailable");
}, 30_000);

test("a POST 400 is rethrown without reading the operation", async () => {
	const { client, posts, gets, entry } = lostClient({ postStatus: 400 });
	const payload = entry.envelope.command.payload as unknown as RecordIntakeDecisionPayload;
	const error = await client.recordIntakeDecision(OP_ID, payload).catch((e: unknown) => e);
	expect((error as WorkError).code).toBe("invalid_request");
	expect(posts).toHaveLength(1);
	expect(gets()).toBe(0);
});

test("a second call keeps the operation id and mints a new request_id", async () => {
	const { client, posts, entry } = lostClient({ getStatus: 400 });
	const payload = entry.envelope.command.payload as unknown as RecordIntakeDecisionPayload;
	await client.recordIntakeDecision(OP_ID, payload).catch(() => undefined);
	await client.recordIntakeDecision(OP_ID, payload).catch(() => undefined);
	expect(new Set(posts.map(post => post.operation_id))).toEqual(new Set([OP_ID]));
	expect(posts[WORK_REQUEST_ATTEMPTS].request_id).not.toBe(posts[0].request_id);
}, 30_000);
