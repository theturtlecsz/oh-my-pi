import { expect, test } from "bun:test";
import {
	type AssessBoundedIntakeCommand,
	type AssessBoundedIntakeResult,
	type BoundedIntakeDraft,
	type Command,
	type CommandResult,
	WORK_CONTRACT_SHA256,
	WorkClient,
	WorkError,
} from "../src/index";

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";

const SAMPLE_DRAFT: BoundedIntakeDraft = {
	archetype: "small_code_change",
	source: {
		text: "Add json export option to stats command.",
		sha256: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		spans: [
			{
				id: "span-1",
				start: 0,
				end: 39,
				exact_text_sha256: "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
			},
		],
	},
	goal: {
		id: "goal-1",
		statement: "Export stats as JSON",
		source_span_ids: ["span-1"],
	},
	constraints: [
		{
			id: "c-1",
			statement: "Preserves default table output",
			key: "output_format",
			value: { kind: "known", value: "table" },
			polarity: "positive",
		},
	],
	acceptance_criteria: [
		{
			id: "ac-1",
			statement: "Emits valid JSON when --json passed",
			observable_outcome: "Exit 0 with JSON on stdout",
			oracle: "automated_test",
		},
	],
};

test("AssessBoundedIntakeCommand and Result integrate into Command and CommandResult unions", () => {
	const command: Command = {
		type: "assess_bounded_intake",
		payload: { draft: SAMPLE_DRAFT },
	};
	expect(command.type).toBe("assess_bounded_intake");

	const result: CommandResult = {
		type: "assess_bounded_intake",
		semantic_sha256: "a".repeat(64),
		rule_bundle_sha256: "b".repeat(64),
		ready_for_ratification: true,
		issue_count: 0,
		questions: [],
	};
	expect(result.type).toBe("assess_bounded_intake");
	expect(result.ready_for_ratification).toBe(true);
});

test("WorkClient.assessBoundedIntake posts command envelope and returns positive assessment", async () => {
	let postedUrl = "";
	let postedHeaders: Headers | undefined;
	let postedBody: unknown;

	const client = new WorkClient(
		"http://127.0.0.1:54322",
		WORKSPACE_ID,
		() => "test-token",
		async (input, init) => {
			postedUrl = String(input);
			postedHeaders = new Headers(init?.headers);
			postedBody = init?.body ? JSON.parse(String(init.body)) : null;
			return Response.json({
				receipt: {
					operation_id: "op-1",
					request_id: "req-1",
					state: "applied",
					request_sha256: "0".repeat(64),
					result_sha256: "1".repeat(64),
					diagnostics: [],
				},
				result: {
					type: "assess_bounded_intake",
					semantic_sha256: "c".repeat(64),
					rule_bundle_sha256: "d".repeat(64),
					ready_for_ratification: true,
					issue_count: 0,
					questions: [],
				} satisfies AssessBoundedIntakeResult,
			});
		},
	);

	const assessment = await client.assessBoundedIntake(SAMPLE_DRAFT);
	expect(postedUrl).toBe("http://127.0.0.1:54322/v1/commands");
	expect(postedHeaders?.get("authorization")).toBe("Bearer test-token");
	expect(postedHeaders?.get("x-omp-workspace-id")).toBe(WORKSPACE_ID);
	expect(postedHeaders?.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);

	const envelope = postedBody as { command?: AssessBoundedIntakeCommand; workspace_id?: string };
	expect(envelope.workspace_id).toBe(WORKSPACE_ID);
	expect(envelope.command?.type).toBe("assess_bounded_intake");
	expect(envelope.command?.payload.draft.goal.statement).toBe("Export stats as JSON");

	expect(assessment.type).toBe("assess_bounded_intake");
	expect(assessment.ready_for_ratification).toBe(true);
	expect(assessment.issue_count).toBe(0);
	expect(assessment.questions).toEqual([]);
});

test("WorkClient.assessBoundedIntake decodes service blocking questions", async () => {
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		WORKSPACE_ID,
		() => "test-token",
		async () => {
			return Response.json({
				receipt: {
					operation_id: "op-2",
					request_id: "req-2",
					state: "applied",
					request_sha256: "0".repeat(64),
					result_sha256: "1".repeat(64),
					diagnostics: [],
				},
				result: {
					type: "assess_bounded_intake",
					semantic_sha256: "e".repeat(64),
					rule_bundle_sha256: "f".repeat(64),
					ready_for_ratification: false,
					issue_count: 2,
					questions: [
						{
							rule_class: "missing_verification_oracle",
							deduplication_key: "oracle-ac-1",
							statement: "Which verification oracle validates the JSON export?",
							priority: 0,
							claim_ids: ["ac-1"],
						},
						{
							rule_class: "contradictory_constraints",
							deduplication_key: "conflict-c-1",
							statement: "Output format cannot be table when JSON is exported",
							priority: 1,
							claim_ids: ["c-1"],
						},
					],
				} satisfies AssessBoundedIntakeResult,
			});
		},
	);

	const assessment = await client.assessBoundedIntake(SAMPLE_DRAFT);
	expect(assessment.ready_for_ratification).toBe(false);
	expect(assessment.issue_count).toBe(2);
	expect(assessment.questions).toHaveLength(2);
	expect(assessment.questions[0]?.rule_class).toBe("missing_verification_oracle");
	expect(assessment.questions[1]?.rule_class).toBe("contradictory_constraints");
});

test("WorkClient.assessBoundedIntake raises WorkError on 4xx/5xx", async () => {
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		WORKSPACE_ID,
		() => "test-token",
		async () => {
			return new Response(
				JSON.stringify({
					error: {
						code: "invalid_request",
						diagnostics: ["draft missing required fields"],
						request_id: "req-err",
					},
				}),
				{ status: 400 },
			);
		},
	);

	expect(client.assessBoundedIntake(SAMPLE_DRAFT)).rejects.toThrow(WorkError);
});
