import { expect, test } from "bun:test";
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
