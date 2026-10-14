import { expect, test } from "bun:test";
import {
	type IntakeCallerRelation,
	type IntakeRecordView,
	type IntakeSupportJudgment,
	type IntakeSupportVerdict,
	WorkClient,
} from "../src/index";

const legacy: IntakeRecordView = {
	workspace_id: "00000000-0000-7000-8000-000000000001",
	mission_id: "00000000-0000-7000-8000-000000000002",
	run_id: "run-1",
	revision: 0,
	snapshot: {
		run_id: "run-1",
		revision: 0,
		state: "collecting",
		artifacts: [],
		entities: [],
		claims: [],
		ontology: { predicates: [], dimensions: [] },
	},
	evidence_links: [],
	criterion_links: [],
	decisions: [],
	issues: [],
	traces: [],
	assessments: [],
	provenance: { run_id: "run-1", revision: 0, source_sha256: "a".repeat(64) },
};

const judgment: IntakeSupportJudgment = {
	claim_id: "claim-1",
	artifact_id: "artifact-1",
	model_id: "model-1",
	source_revision: "revision-1",
	model_version: "model-v1",
	prompt_version: "prompt-v1",
	rule_version: "rule-v1",
	context_sha256: "d".repeat(64),
	start_byte: 2,
	end_byte: 18,
	caller_relation: "supports",
	verdict: "contradicts",
	passage_sha256: "b".repeat(64),
	claim_text_sha256: "c".repeat(64),
	disagreement: true,
};

// @ts-expect-error Pending judge states cannot be caller labels.
"timeout" satisfies IntakeCallerRelation;
const { source_revision: _revision, ...missingBinding } = judgment;
// @ts-expect-error Receipt must bind the source revision.
missingBinding satisfies IntakeSupportJudgment;

const populated: IntakeRecordView = { ...legacy, support_judgments: [judgment] };

// Type checking must reject unsupported verdicts and incomplete receipts.
// @ts-expect-error Verdict vocabulary matches the service schema.
"unknown" satisfies IntakeSupportVerdict;
({
	...judgment,
	// @ts-expect-error Judgment verdict must use the service vocabulary.
	verdict: "unknown",
}) satisfies IntakeSupportJudgment;
({
	...judgment,
	// @ts-expect-error Caller relation remains a three-state label.
	caller_relation: "abstain",
}) satisfies IntakeSupportJudgment;
// @ts-expect-error IDs alone omit required span, verdict, hashes and disagreement.
({ claim_id: "claim-1", artifact_id: "artifact-1", model_id: "model-1" }) satisfies IntakeSupportJudgment;

test("legacy intake views accept omitted support judgments", () => {
	expect(legacy.support_judgments).toBeUndefined();
});

test("populated intake views expose typed support receipt fields", () => {
	const receipt: IntakeSupportJudgment | undefined = populated.support_judgments?.[0];
	expect(receipt).toEqual(judgment);
});

test("WorkClient.intake preserves support receipts and legacy omission from HTTP JSON", async () => {
	for (const wire of [legacy, populated]) {
		const client = new WorkClient(
			"http://127.0.0.1:54322",
			wire.workspace_id,
			() => "test-token",
			async (input, init) => {
				expect(String(input)).toBe(`http://127.0.0.1:54322/v1/workspaces/${wire.workspace_id}/intake/run-1`);
				expect(init?.method).toBe("GET");
				return Response.json(wire);
			},
		);
		const view = await client.intake("run-1");
		expect(view.support_judgments).toEqual(wire.support_judgments);
	}
});

for (const verdict of ["supports", "contradicts", "neither", "abstain", "timeout", "evidence-unavailable"] as const) {
	test(`WorkClient.intake preserves ${verdict} verdict and receipt bindings`, async () => {
		const wire: IntakeRecordView = {
			...legacy,
			support_judgments: [{ ...judgment, verdict, disagreement: verdict !== "supports" }],
		};
		const client = new WorkClient(
			"http://127.0.0.1:54322",
			wire.workspace_id,
			() => "test-token",
			async () => Response.json(wire),
		);
		const receipt = (await client.intake("run-1")).support_judgments?.[0];
		expect(receipt).toEqual(wire.support_judgments?.[0]);
		expect(receipt?.verdict === "supports").toBe(verdict === "supports");
		expect(receipt?.disagreement).toBe(verdict !== "supports");
	});
}
