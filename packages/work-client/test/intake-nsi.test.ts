import { expect, test } from "bun:test";
import type {
	ActorRef,
	Binding,
	ClaimModality,
	ClaimOrigin,
	ClaimState,
	DraftMissionIntakePayload,
	IntakeCategory,
	IntakeClaim,
	IntakeContract,
	IntakePublicationReceipt,
	IntakeReadinessAssessment,
	IntakeReadinessReceipt,
	IntakeRunState,
	IntakeSnapshot,
	RatificationReceipt,
	TypedValue,
} from "../src/index";
import { $ } from "bun";
import { WorkClient } from "../src/index";

const contractDir = `${import.meta.dir}/../../../python/omp-work/src/omp_work/contracts`;
const names = ["authority", "taxonomy", "claim", "status", "contract"];
const documents: Record<string, { $defs: Record<string, { enum?: string[] }> }> = {};
for (const name of names) {
	documents[name] = await Bun.file(`${contractDir}/nsi/v1/${name}.schema.json`).json();
}

// Reuse the service's offline validator rather than duplicating schema semantics.
const validationScript = `
import json, sys
from omp_work.contracts.nsi.v1.validate import validate_instance
from jsonschema import Draft202012Validator
from pathlib import Path
results = []
for document, definition, value in json.load(sys.stdin):
    try:
        if document == "bounded":
            schema = json.loads((Path(sys.argv[1]) / "v1/schema.json").read_text())["models"][definition]
            Draft202012Validator(schema).validate(value)
        else:
            validate_instance(f"{document}#/$defs/{definition}", value)
        results.append(True)
    except ValueError:
        results.append(False)
json.dump(results, sys.stdout)
`;
async function validateRecords(records: [string, string, unknown][]): Promise<boolean[]> {
	const result =
		await $`uv run --project ${`${import.meta.dir}/../../../python/omp-work`} python -c ${validationScript} ${contractDir} < ${new Response(JSON.stringify(records))}`
			.quiet()
			.nothrow();
	if (result.exitCode !== 0) throw new Error(result.stderr.toString());
	return JSON.parse(result.text());
}

const owner = { id: "owner-1", kind: "owner" } as const satisfies ActorRef;
const engine = { id: "engine-1", kind: "rule_engine" } as const satisfies ActorRef;
const binding: Binding = {
	run_id: "intake-1",
	revision: 1,
	contract_hash: "a".repeat(64),
	claim_set_hash: "b".repeat(64),
	rule_bundle_hash: "c".repeat(64),
	schema_hash: "d".repeat(64),
	source_hash: "e".repeat(64),
};
const claim: IntakeClaim = {
	id: "claim-1",
	category: "functional_scope_and_behavior",
	subject: { id: "export", type: "feature" },
	predicate_id: "encoding",
	value: { type: "string", value: "JSON" },
	origin: "owner_statement",
	state: "admitted",
	modality: "required",
	polarity: "positive",
	truth: "true",
	asserted_by: owner,
	admitted_by: owner,
	evidence: [{ artifact_id: "source-1", start_byte: 0, end_byte: 4 }],
	premise_ids: [],
};
const snapshot: IntakeSnapshot = {
	run_id: binding.run_id,
	revision: binding.revision,
	state: "ready_for_ratification",
	artifacts: [
		{
			id: "source-1",
			uri: "urn:intake:source-1",
			content_hash: binding.source_hash,
			revision: "1",
			actor: owner,
			media_type: "text/plain",
		},
	],
	entities: [claim.subject],
	claims: [claim],
	ontology: {
		predicates: [
			{
				id: "encoding",
				title: "Export encoding",
				subject_types: ["feature"],
				object_types: ["string"],
				cardinality: "one",
				exclusivity: true,
				transitivity: false,
				authority: ["owner"],
			},
		],
		dimensions: [
			{
				id: "scope",
				title: "Functional scope",
				category: claim.category,
				applicability: { entity_types: ["feature"], predicate_ids: ["encoding"] },
				required_premises: [claim.id],
				materiality: "critical",
				waiver_policy: "never",
			},
		],
	},
};
const intakeContract: IntakeContract = {
	requirements: [
		{ id: "req-1", category: claim.category, text: "Export JSON", claim_ids: [claim.id], dimension_ids: ["scope"] },
	],
	acceptance_criteria: [
		{
			id: "ac-1",
			requirement_ids: ["req-1"],
			description: "Export parses as JSON",
			verification_method: "automated_test",
			expected_result: { type: "boolean", value: true },
		},
	],
	formalizations: [
		{
			id: "formal-1",
			requirement_ids: ["req-1"],
			language: "predicate",
			expression: "encoding(export, JSON)",
			scope: "export",
			assumptions: [],
			content_hash: binding.contract_hash,
		},
	],
	waivers: [
		{
			id: "waiver-1",
			run_id: binding.run_id,
			revision: 1,
			premise_id: "style",
			dimension_id: "ux",
			materiality: "minor",
			waiver_policy: "owner_with_reason",
			granted_by: owner,
			reason: "CLI output only",
			decision_id: "decision-1",
			created_at: "2026-10-11T00:00:00Z",
		},
	],
};
const assessment: IntakeReadinessAssessment = {
	...binding,
	status: "READY_FOR_RATIFICATION",
	gaps: [],
	conflicts: [],
	derivation_receipt_ids: ["derivation-1"],
	assessed_by: engine,
	created_at: "2026-10-11T00:00:00Z",
};
const ratification: RatificationReceipt = {
	...binding,
	assessment_id: "assessment-1",
	formal_binding_ids: ["formal-binding-1"],
	ratified_by: owner,
	created_at: assessment.created_at,
};
const readiness: IntakeReadinessReceipt = {
	...binding,
	status: "ELIGIBLE_FOR_PUBLICATION",
	ratification_receipt_id: "ratification-1",
	formal_binding_ids: ratification.formal_binding_ids,
	derivation_receipt_ids: assessment.derivation_receipt_ids,
	issued_by: engine,
	created_at: assessment.created_at,
};
const publication: IntakePublicationReceipt = {
	...binding,
	state: "published",
	ratification_receipt_id: "ratification-1",
	readiness_receipt_id: "readiness-1",
	publishedBy: owner,
	created_at: assessment.created_at,
};

// Compile-time contracts: wrong authorities, missing admission, mismatched values fail.
// @ts-expect-error Models cannot publish.
const modelPublisher: IntakePublicationReceipt = { ...publication, publishedBy: { id: "model-1", kind: "model" } };
// @ts-expect-error Readiness is issued by the rule engine.
const ownerReadiness: IntakeReadinessReceipt = { ...readiness, issued_by: owner };
// @ts-expect-error Typed boolean values cannot contain strings.
const wrongValue: TypedValue = { type: "boolean", value: "true" };
const { admitted_by: _admittedBy, ...unadmitted } = claim;
// @ts-expect-error An admitted claim requires admission authority.
const missingAdmission: IntakeClaim = unadmitted;
void [modelPublisher, ownerReadiness, wrongValue, missingAdmission];

test("client NSI vocabulary matches versioned schema enums without missing or extra values", () => {
	const categories: Record<IntakeCategory, true> = {
		functional_scope_and_behavior: true,
		domain_and_data_model: true,
		interaction_and_ux_flow: true,
		non_functional_qualities: true,
		integrations_and_external_dependencies: true,
		edge_cases_and_failure_handling: true,
		constraints_and_tradeoffs: true,
		terminology: true,
		completion_signals: true,
		placeholders_and_ambiguities: true,
		scope_boundaries: true,
	};
	const states: Record<IntakeRunState, true> = {
		collecting: true,
		reasoning: true,
		needs_spec: true,
		conflicted: true,
		ready_for_ratification: true,
		ratified: true,
		published: true,
		stale: true,
		invalid: true,
		superseded: true,
	};
	const origins: Record<ClaimOrigin, true> = {
		owner_statement: true,
		policy_rule: true,
		repository_observation: true,
		tool_receipt: true,
		model_proposal: true,
		rule_derivation: true,
	};
	const claimStates: Record<ClaimState, true> = { proposed: true, admitted: true, rejected: true, superseded: true };
	const modalities: Record<ClaimModality, true> = {
		observed: true,
		desired: true,
		required: true,
		prohibited: true,
		possible: true,
		unknown: true,
	};
	for (const [document, definition, values] of [
		["taxonomy", "IntakeCategory", categories],
		["status", "IntakeRunState", states],
		["claim", "ClaimOrigin", origins],
		["claim", "ClaimState", claimStates],
		["claim", "ClaimModality", modalities],
	] as const) {
		expect(Object.keys(values).sort()).toEqual([...documents[document].$defs[definition].enum!].sort());
	}
});

test("typed snapshot components and receipt chain satisfy NSI wire schemas", async () => {
	const records: [string, string, unknown][] = [
		["authority", "ActorRef", owner],
		["claim", "IntakeClaim", claim],
		["claim", "EvidenceArtifact", snapshot.artifacts[0]],
		["taxonomy", "EntityRef", snapshot.entities[0]],
		["taxonomy", "PredicateDefinition", snapshot.ontology.predicates[0]],
		["taxonomy", "RequirementDimensionDefinition", snapshot.ontology.dimensions[0]],
		["contract", "IntakeContract", intakeContract],
		["contract", "IntakeReadinessAssessment", assessment],
		["contract", "RatificationReceipt", ratification],
		["contract", "IntakeReadinessReceipt", readiness],
		["contract", "IntakePublicationReceipt", publication],
	];
	for (const value of [
		{ type: "integer", value: 1 },
		{ type: "number", value: 1.5 },
		{ type: "null", value: null },
		{ type: "entity", value: claim.subject },
	] satisfies TypedValue[]) {
		records.push(["claim", "IntakeClaim", { ...claim, value }]);
	}
	const invalid: [string, string, unknown][] = [
		["contract", "IntakePublicationReceipt", modelPublisher],
		["contract", "IntakeReadinessReceipt", ownerReadiness],
		["claim", "IntakeClaim", missingAdmission],
		["claim", "IntakeClaim", { ...claim, value: wrongValue }],
		["contract", "IntakePublicationReceipt", { ...publication, source_hash: "unbound" }],
	];
	expect(await validateRecords([...records, ...invalid])).toEqual([
		...records.map(() => true),
		...invalid.map(() => false),
	]);
}, 30_000);

test("bounded intake remains a schema-valid mission.intake client payload", async () => {
	const payload: DraftMissionIntakePayload = {
		mission_id: "00000000-0000-0000-0000-000000000001",
		intake: {
			archetype: "small_code_change",
			source: { text: "Export JSON", sha256: binding.source_hash, spans: [] },
			goal: { id: claim.id, statement: intakeContract.requirements[0].text },
			constraints: [
				{
					id: "encoding",
					statement: "Export JSON",
					key: "encoding",
					value: { kind: "known", value: "JSON" },
					polarity: "positive",
				},
			],
			unknowns: [{ id: "style", statement: "Choose indentation", kind: "routine_choice", material: false }],
			acceptance_criteria: [
				{
					id: "ac-1",
					statement: "Export parses as JSON",
					observable_outcome: "JSON parses",
					oracle: "automated_test",
				},
			],
			budget: { usd: "1.00", tokens: 1000, wall_clock_seconds: 60, max_subagents: 0 },
		},
		scope: {
			project_id: "00000000-0000-0000-0000-000000000002",
			risk_policy: "default",
			approval_policy: "default",
			effort_policy: "default",
			kind: "engineering.execute",
		},
	};
	expect(await validateRecords([["bounded", "DraftMissionIntakePayload", payload]])).toEqual([true]);
	let requestBody: unknown;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		payload.scope.project_id,
		() => "token",
		async (_input, init) => {
			requestBody = JSON.parse(String(init?.body));
			return Response.json({
				outcome: "applied",
				operation: "mission.intake",
				contract: "client.omp.dev/v1",
				result: {
					type: "draft_mission_intake",
					mission_id: payload.mission_id,
					outcome: "proceeded",
					questions: [],
				},
			});
		},
	);
	const requestId = "00000000-0000-0000-0000-000000000003";
	const result = await client.missionIntake(requestId, payload);
	expect(requestBody).toEqual({ request_id: requestId, payload });
	expect(result.operation).toBe("mission.intake");
}, 30_000);
