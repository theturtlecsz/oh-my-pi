import { expect, test } from "bun:test";
import {
	type BudgetQuote,
	payloadHash,
	type ResearchCampaign,
	type ResearchComponent,
	type ResearchDeliverableBinding,
	type ResearchObservation,
	type ResearchTrial,
	type ResearchView,
	WORK_CONTRACT_SHA256,
	WorkClient,
	WorkError,
} from "../src/index";

const ENV = {
	api_version: "work.omp.dev/v1" as const,
	workspace_id: "00000000-0000-0000-0000-000000000001",
	operation_id: "00000000-0000-0000-0000-0000000000a1",
	request_id: "00000000-0000-0000-0000-0000000000b1",
	correlation_id: "00000000-0000-0000-0000-0000000000c1",
};

const RECEIPT = {
	operation_id: "o",
	request_id: "r",
	state: "replayed",
	request_sha256: "0".repeat(64),
	result_sha256: "1".repeat(64),
	diagnostics: [],
};

test("sends the fixed command surface and decodes replay", async () => {
	let request: Request | undefined;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json({
				receipt: RECEIPT,
				result: { type: "set_work_state", work_id: "w", state: "DONE", row_version: 2 },
			});
		},
	);
	const result = await client.execute({
		...ENV,
		command: { type: "set_work_state", payload: { work_id: "00000000-0000-0000-0000-0000000000d1", state: "DONE" } },
	});
	expect(request?.url).toBe("http://127.0.0.1:54322/v1/commands");
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(request?.headers.get("x-omp-workspace-id")).toBe(ENV.workspace_id);
	// OMP-143: every authenticated call carries the loaded contract digest.
	expect(request?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(WORK_CONTRACT_SHA256).toMatch(/^[0-9a-f]{64}$/);
	expect(result.receipt.state).toBe("replayed");
	if (result.result.type !== "set_work_state") throw new Error("wrong result variant");
	expect(result.result.row_version).toBe(2);
});

test("surfaces service errors redacted", async () => {
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async () =>
			Response.json(
				{
					error: {
						code: "idempotency_conflict",
						request_id: null,
						correlation_id: null,
						diagnostics: ["Bearer abcdef1234567890abcdef1234567890 leaked"],
					},
				},
				{ status: 409 },
			),
	);
	const err = await client.operation("00000000-0000-0000-0000-0000000000e1").catch(e => e);
	expect(err).toBeInstanceOf(WorkError);
	expect((err as WorkError).code).toBe("idempotency_conflict");
	expect((err as WorkError).status).toBe(409);
	expect(String(err)).not.toContain("abcdef1234567890abcdef1234567890");
});

test("contract_mismatch diagnostics survive redaction into the WorkError text", async () => {
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async () =>
			Response.json(
				{
					error: {
						code: "contract_mismatch",
						request_id: null,
						correlation_id: null,
						diagnostics: [
							"host contract digest: missing",
							`service contract digest: ${"a".repeat(64)}`,
							"restart the OMP session",
						],
					},
				},
				{ status: 409 },
			),
	);
	const err = await client.tree().catch(e => e);
	expect(err).toBeInstanceOf(WorkError);
	expect((err as WorkError).code).toBe("contract_mismatch");
	expect((err as WorkError).status).toBe(409);
	expect(String(err)).toContain("host contract digest: missing");
	expect(String(err)).toContain(`service contract digest: ${"a".repeat(64)}`);
	expect(String(err)).toContain("restart the OMP session");
});

test("health probes never require a bearer", async () => {
	let sawAuth: string | null = "unset";
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => null, // no bearer configured
		async (input, init) => {
			sawAuth = new Request(String(input), init).headers.get("authorization");
			return Response.json({ live: true, ready: true, alerts: [] });
		},
	);
	const ready = await client.healthReady();
	expect(ready.ready).toBe(true);
	expect(sawAuth).toBeNull();
});

test("activity encodes query parameters, sends auth, and decodes the projection", async () => {
	let request: Request | undefined;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json({
				workspace_id: ENV.workspace_id,
				total: 12,
				events: [
					{
						kind: "closeout",
						work_id: "00000000-0000-0000-0000-0000000000d1",
						key: "OMP-7",
						title: "Recent move",
						project_id: null,
						occurred_at: "2026-08-19T12:00:00+00:00",
					},
				],
			});
		},
	);
	await client.activity();
	expect(request?.url).toBe(`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/activity`);
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	const view = await client.activity({ projectId: "00000000-0000-0000-0000-0000000000f1", limit: 8 });
	expect(request?.url).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/activity?project_id=00000000-0000-0000-0000-0000000000f1&limit=8`,
	);
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(view.total).toBe(12);
	expect(view.events[0]?.kind).toBe("closeout");
	expect(view.events[0]?.key).toBe("OMP-7");
});

test("payloadHash matches canonical_json semantics (sorted keys, tight separators, no ascii escaping)", () => {
	// canonical.py: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
	expect(payloadHash({ b: 1, a: [2, { d: "ü", c: null }] })).toBe(payloadHash({ a: [2, { c: null, d: "ü" }], b: 1 }));
});

test("tree rejects unauthenticated without calling fetch when the token provider returns null", async () => {
	let fetchCalls = 0;
	const client = new WorkClient(
		"http://127.0.0.1:8787",
		"workspace-test",
		() => null,
		async () => {
			fetchCalls += 1;
			return new Response("{}", { status: 200 });
		},
	);

	await expect(client.tree()).rejects.toMatchObject({
		code: "unauthenticated",
		status: 401,
	});
	expect(fetchCalls).toBe(0);
});

test("tree maps a redacted fetch exception to unavailable", async () => {
	const secret = "fetch-exception-secret-4f2d9c7a";
	let fetchCalls = 0;
	const client = new WorkClient(
		"http://127.0.0.1:8787",
		"workspace-test",
		() => "configured-token",
		async () => {
			fetchCalls += 1;
			throw new Error(`request failed with Authorization: Bearer ${secret}`);
		},
	);

	let rejection: unknown;
	try {
		await client.tree();
	} catch (error) {
		rejection = error;
	}

	expect(fetchCalls).toBe(1);
	expect(rejection).toMatchObject({
		code: "unavailable",
		status: 0,
	});
	const diagnostics = (rejection as { readonly diagnostics: readonly string[] }).diagnostics;
	expect(diagnostics.length).toBeGreaterThan(0);
	expect(diagnostics.join("\n")).not.toContain(secret);
});

test("complete_work and complete_execution_item send CompletionEvidence and omit push_receipt_id", async () => {
	const bodies: Array<Record<string, unknown>> = [];
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (_input, init) => {
			const parsed = JSON.parse(String(init?.body)) as Record<string, unknown>;
			bodies.push(parsed);
			return new Response(
				JSON.stringify({
					receipt: RECEIPT,
					result: {
						type: (parsed.command as { type: string }).type,
						grant: { grant_id: "00000000-0000-0000-0000-000000000001", state: "completed" },
						item: { work_id: "00000000-0000-0000-0000-000000000002", phase: "completed" },
						work_id: "00000000-0000-0000-0000-000000000002",
						state: "DONE",
						row_version: 1,
					},
				}),
				{ status: 200 },
			);
		},
	);

	const evidence = {
		runner: {
			issuer: "work-service/auditor-settle" as const,
			launch_id: "00000000-0000-0000-0000-000000000011",
			tool_call_id: "tc-1",
			task_sha256: "0".repeat(64),
			judge_sha256: null,
		},
		subject: {
			work_id: "00000000-0000-0000-0000-000000000002",
			revision_id: "00000000-0000-0000-0000-000000000003",
			candidate_id: "00000000-0000-0000-0000-000000000004",
			candidate_sha256: "1".repeat(64),
			candidate_commit: "2".repeat(40),
		},
		check: {
			definition: "sealed_audit_manifest" as const,
			version: 1 as const,
			manifest_id: "00000000-0000-0000-0000-000000000012",
			task_sha256: "0".repeat(64),
		},
		result: "PASS" as const,
		artifacts: [
			{
				receipt_id: "00000000-0000-0000-0000-000000000021",
				kind: "verification" as const,
				payload_sha256: "3".repeat(64),
				artifact_sha256: "4".repeat(64),
			},
			{
				receipt_id: "00000000-0000-0000-0000-000000000022",
				kind: "audit" as const,
				payload_sha256: "5".repeat(64),
				artifact_sha256: "6".repeat(64),
			},
			{
				receipt_id: "00000000-0000-0000-0000-000000000023",
				kind: "push" as const,
				payload_sha256: "7".repeat(64),
				artifact_sha256: null,
			},
		],
		delivery: {
			repository: "theturtlecsz/oh-my-pi",
			remote_url: "https://github.com/theturtlecsz/oh-my-pi.git",
			remote_ref: "refs/heads/main",
			candidate_commit: "2".repeat(40),
			remote_commit: "2".repeat(40),
		},
	};

	await client.execute({
		...ENV,
		command: {
			type: "complete_work",
			payload: {
				input: {
					work_id: "00000000-0000-0000-0000-000000000002",
					current_revision_id: "00000000-0000-0000-0000-000000000003",
					candidate: {
						candidate_id: "00000000-0000-0000-0000-000000000004",
						work_id: "00000000-0000-0000-0000-000000000002",
						revision_id: "00000000-0000-0000-0000-000000000003",
						candidate_sha256: "1".repeat(64),
						commit_sha: "2".repeat(40),
						kind: "final",
						allocated_at: "2026-08-15T00:00:00Z",
					},
					receipts: [],
					closeout_requested: true,
				},
				attempt_id: "00000000-0000-0000-0000-000000000010",
				done_authorization_ref: "done:ref",
				evidence,
			},
		},
	});

	await client.execute({
		...ENV,
		command: {
			type: "complete_execution_item",
			payload: {
				grant_id: "00000000-0000-0000-0000-000000000001",
				expected_grant_version: 1,
				work_id: "00000000-0000-0000-0000-000000000002",
				attempt_id: "00000000-0000-0000-0000-000000000010",
				evidence,
				judge_sha256: "8".repeat(64),
			},
		},
	});
	const cmd0 = bodies[0]?.command;
	expect(cmd0 && typeof cmd0 === "object" && "payload" in cmd0).toBe(true);
	if (
		cmd0 &&
		typeof cmd0 === "object" &&
		"payload" in cmd0 &&
		typeof cmd0.payload === "object" &&
		cmd0.payload !== null
	) {
		const payload = cmd0.payload as Record<string, unknown>;
		expect(payload.evidence).toEqual(evidence);
	}

	const cmd1 = bodies[1]?.command;
	expect(cmd1 && typeof cmd1 === "object" && "payload" in cmd1).toBe(true);
	if (
		cmd1 &&
		typeof cmd1 === "object" &&
		"payload" in cmd1 &&
		typeof cmd1.payload === "object" &&
		cmd1.payload !== null
	) {
		const payload = cmd1.payload as Record<string, unknown>;
		expect(payload.evidence).toEqual(evidence);
		expect("push_receipt_id" in payload).toBe(false);
	}
});

test("revision encodes selector URL and decodes exact WorkRevision", async () => {
	let requestedUrl: string | undefined;
	const requests: Request[] = [];
	const dummyRev = {
		revision_id: "00000000-0000-7000-8000-000000000002",
		work_id: "00000000-0000-7000-8000-000000000001",
		revision_number: 1,
		title: "Initial work",
		description: "desc",
		scope: "scope",
		acceptance_criteria: ["AC-1"],
		content_sha256: "0".repeat(64),
		created_by: "owner",
		created_at: "2026-09-12T12:00:00+00:00",
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			requestedUrl = String(input);
			requests.push(new Request(String(input), init));
			return Response.json(dummyRev);
		},
	);

	await client.revision("OMP-1", 1);
	expect(requestedUrl).toBe("http://127.0.0.1:54322/v1/work-items/OMP-1/revisions/1");
	expect(requests[0].headers.get("authorization")).toBe("Bearer token");

	const revUuid = "00000000-0000-7000-8000-000000000002";
	await client.revision("OMP-1", revUuid);
	expect(requestedUrl).toBe(`http://127.0.0.1:54322/v1/work-items/OMP-1/revisions/${revUuid}`);
});

test("revisions fetches complete revision list for a work item", async () => {
	let requestedUrl: string | undefined;
	const dummyList = {
		work_id: "00000000-0000-7000-8000-000000000001",
		key: "OMP-1",
		revisions: [
			{
				revision_id: "00000000-0000-7000-8000-000000000002",
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_number: 1,
				title: "Rev 1",
				description: "d1",
				scope: "s1",
				acceptance_criteria: ["AC-1"],
				content_sha256: "0".repeat(64),
				created_by: "owner",
				created_at: "2026-09-12T12:00:00+00:00",
			},
		],
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async input => {
			requestedUrl = String(input);
			return Response.json(dummyList);
		},
	);

	await client.revisions("OMP-1");
	expect(requestedUrl).toBe("http://127.0.0.1:54322/v1/work-items/OMP-1/revisions");
});

test("receipt fetches immutable EvidenceReceipt by ID", async () => {
	const requests: Request[] = [];
	const dummyReceipt = {
		receipt_id: "00000000-0000-7000-8000-000000000010",
		work_id: "00000000-0000-7000-8000-000000000001",
		revision_id: "00000000-0000-7000-8000-000000000002",
		candidate_id: "00000000-0000-7000-8000-000000000003",
		kind: "plan",
		payload: { note: "plan-payload" },
		payload_sha256: "a".repeat(64),
		issuer: "owner",
		issued_at: "2026-09-12T12:00:00+00:00",
		independent: false,
	};
	const responses = [
		{ ...dummyReceipt, issuer: null, independent: null },
		{ ...dummyReceipt, issuer: "owner", independent: false },
	];
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async input => {
			requests.push(new Request(String(input)));
			return Response.json(responses[requests.length - 1]);
		},
	);

	const legacy = await client.receipt("00000000-0000-7000-8000-000000000010");
	const modern = await client.receipt("00000000-0000-7000-8000-000000000010");

	expect(requests[0].url).toBe("http://127.0.0.1:54322/v1/receipts/00000000-0000-7000-8000-000000000010");
	expect(legacy.issuer).toBeNull();
	expect(legacy.independent).toBeNull();
	expect(modern.issuer).toBe("owner");
	expect(modern.independent).toBe(false);
});

test("workItems paginates keyset with cursor and limit", async () => {
	let requestedUrl: string | undefined;
	const dummyPage = {
		workspace_id: ENV.workspace_id,
		items: [
			{
				work_id: "00000000-0000-7000-8000-000000000001",
				workspace_id: ENV.workspace_id,
				alias: {
					work_id: "00000000-0000-7000-8000-000000000001",
					key: "OMP-1",
					primary: true as const,
					origin: "local" as const,
				},
				state: "TODO",
				revision: {
					revision_id: "00000000-0000-7000-8000-000000000002",
					work_id: "00000000-0000-7000-8000-000000000001",
					revision_number: 1,
					title: "Item 1",
					description: "d",
					scope: "s",
					acceptance_criteria: [],
					content_sha256: "0".repeat(64),
					created_by: "owner",
					created_at: "2026-09-12T12:00:00+00:00",
				},
				candidate: null,
				project_id: null,
				archived: false,
			},
		],
		next_cursor: "cursor-token-123",
		limit: 10,
		exhausted: false,
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async input => {
			requestedUrl = String(input);
			return Response.json(dummyPage);
		},
	);

	await client.workItems({ limit: 10 });
	expect(requestedUrl).toBe(`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/work-items?limit=10`);
});

test("events queries domain events with after_sequence and through_sequence", async () => {
	let requestedUrl: string | undefined;
	const dummyEventsPage = {
		items: [
			{
				event_id: "00000000-0000-7000-8000-000000000001",
				sequence: 1,
				workspace_id: ENV.workspace_id,
				aggregate_type: "work_item",
				aggregate_id: "00000000-0000-7000-8000-000000000002",
				aggregate_version: 1,
				actor_id: "00000000-0000-7000-8000-000000000003",
				actor_kind: "owner",
				capability_id: "00000000-0000-7000-8000-000000000004",
				request_id: "00000000-0000-7000-8000-000000000005",
				correlation_id: "00000000-0000-7000-8000-000000000006",
				operation_id: "00000000-0000-7000-8000-000000000007",
				causation_id: "00000000-0000-7000-8000-000000000007",
				event_type: "create_work_batch",
				outcome: "applied",
				payload: { ok: true },
				payload_sha256: "a".repeat(64),
				previous_event_sha256: null,
				event_sha256: "b".repeat(64),
				occurred_at: "2026-09-12T12:00:00+00:00",
			},
		],
		workspace_id: ENV.workspace_id,
		after_sequence: 0,
		through_sequence: 1,
		next_sequence: 1,
		next_cursor: null,
		limit: 100,
		exhausted: true,
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async input => {
			requestedUrl = String(input);
			return Response.json(dummyEventsPage);
		},
	);

	await client.events({ afterSequence: 0, throughSequence: 1, limit: 100 });
	expect(requestedUrl).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/events?after_sequence=0&through_sequence=1&limit=100`,
	);
});

test("repositories fetches workspace repositories", async () => {
	let requestedUrl: string | undefined;
	const dummyRepos = {
		workspace_id: ENV.workspace_id,
		repositories: [
			{
				repository_id: "00000000-0000-7000-8000-000000000010",
				workspace_id: ENV.workspace_id,
				key: "repo-1",
				name: "primary-repo",
				url: "https://github.com/example/repo",
				archived: false,
				provenance: {},
				created_at: "2026-09-12T12:00:00+00:00",
			},
		],
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async input => {
			requestedUrl = String(input);
			return Response.json(dummyRepos);
		},
	);

	await client.repositories({ limit: 50 });
	expect(requestedUrl).toBe(`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/repositories?limit=50`);
});

test("native collection cursors preserve encoded tokens and zero event bounds", async () => {
	const requests: Request[] = [];
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			const req = new Request(String(input), init);
			requests.push(req);
			return Response.json({});
		},
	);

	await client.workItems({ cursor: "a+/=", limit: 7 });
	await client.events({ afterSequence: 0, throughSequence: 0 });
	await client.events({ cursor: "a+/=", limit: 7 });
	await client.repositories({ cursor: "a+/=", limit: 7 });
	await client.repositories();

	expect(requests.length).toBe(5);
	for (const req of requests) {
		expect(req.method).toBe("GET");
	}

	const u0 = new URL(requests[0].url);
	expect(u0.searchParams.get("cursor")).toBe("a+/=");
	expect(u0.searchParams.get("limit")).toBe("7");

	const u1 = new URL(requests[1].url);
	expect(u1.searchParams.get("after_sequence")).toBe("0");
	expect(u1.searchParams.get("through_sequence")).toBe("0");

	const u2 = new URL(requests[2].url);
	expect(u2.searchParams.get("cursor")).toBe("a+/=");
	expect(u2.searchParams.get("limit")).toBe("7");

	const u3 = new URL(requests[3].url);
	expect(u3.searchParams.get("cursor")).toBe("a+/=");
	expect(u3.searchParams.get("limit")).toBe("7");

	const u4 = new URL(requests[4].url);
	expect(u4.search).toBe("");
});

test("providerAccounts fetches workspace provider-account list view with contract headers", async () => {
	let request: Request | undefined;
	const dummyListView = {
		workspace_id: ENV.workspace_id,
		accounts: [
			{
				account_id: "00000000-0000-7000-8000-000000000001",
				workspace_id: ENV.workspace_id,
				provider: "anthropic",
				account_identity: "acct-1",
				entitlement_evidence: "evidence-1",
				evidence_observed_at: "2026-09-17T00:00:00+00:00",
				billing_mode: "metered" as const,
				rate_card_version: "2026-09",
				observed_balance: "100.50",
				balance_provenance: "provider_observed" as const,
				reset_at: null,
				concurrency_limit: 5,
				budget_resource: "included_credit" as const,
			},
		],
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json(dummyListView);
		},
	);

	const view = await client.providerAccounts();
	expect(request?.url).toBe(`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/provider-accounts`);
	expect(request?.method).toBe("GET");
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(request?.headers.get("x-omp-workspace-id")).toBe(ENV.workspace_id);
	expect(request?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(view).toEqual(dummyListView);
});

test("providerAccount encodes accountId URL and returns single ProviderAccount", async () => {
	let request: Request | undefined;
	const accountId = "test/account id 01";
	const dummyAccount = {
		account_id: accountId,
		workspace_id: ENV.workspace_id,
		provider: "openai",
		account_identity: "acct-2",
		entitlement_evidence: "evidence-2",
		evidence_observed_at: "2026-09-17T00:00:00+00:00",
		billing_mode: "subscription" as const,
		rate_card_version: null,
		observed_balance: null,
		balance_provenance: "unknown" as const,
		reset_at: "2026-10-01T00:00:00+00:00",
		concurrency_limit: 2,
		budget_resource: null,
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json(dummyAccount);
		},
	);

	const account = await client.providerAccount(accountId);
	expect(request?.url).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/provider-accounts/test%2Faccount%20id%2001`,
	);
	expect(request?.method).toBe("GET");
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(request?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(account).toEqual(dummyAccount);
});

test("providerAccount surfaces typed service error on not found", async () => {
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async () =>
			Response.json(
				{
					error: {
						code: "invalid_request",
						request_id: null,
						correlation_id: null,
						diagnostics: ["not_found", "provider account not found in workspace"],
					},
				},
				{ status: 400 },
			),
	);

	const err = await client.providerAccount("00000000-0000-7000-8000-000000000099").catch(e => e);
	expect(err).toBeInstanceOf(WorkError);
	expect((err as WorkError).code).toBe("invalid_request");
	expect((err as WorkError).status).toBe(400);
	expect(String(err)).toContain("not_found");
});

test("rateCards fetches workspace rate-card list view with contract headers", async () => {
	let request: Request | undefined;
	const dummyListView = {
		workspace_id: ENV.workspace_id,
		rate_cards: [
			{
				rate_card_id: "00000000-0000-7000-8000-000000000001",
				workspace_id: ENV.workspace_id,
				provider: "anthropic",
				version: "2026-09",
				billing_modes: ["metered" as const],
				effective_from: "2026-09-01T00:00:00+00:00",
				effective_until: null,
				currency: "USD",
				unit_prices: { input: "0.000003", output: "0.000015" },
				evidence_source: "https://example.com/pricing",
				evidence_sha256: "a".repeat(64),
				observed_at: "2026-09-17T00:00:00+00:00",
				qualification: "qualified" as const,
				registered_at: "2026-09-17T00:00:00+00:00",
			},
		],
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json(dummyListView);
		},
	);

	const view = await client.rateCards();
	expect(request?.url).toBe(`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/rate-cards`);
	expect(request?.method).toBe("GET");
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(request?.headers.get("x-omp-workspace-id")).toBe(ENV.workspace_id);
	expect(request?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(view).toEqual(dummyListView);
});

test("rateCard encodes rateCardId URL and returns single RateCard", async () => {
	let request: Request | undefined;
	const rateCardId = "00000000-0000-7000-8000-000000000001";
	const dummyCard = {
		rate_card_id: rateCardId,
		workspace_id: ENV.workspace_id,
		provider: "anthropic",
		version: "2026-09",
		billing_modes: ["metered" as const],
		effective_from: "2026-09-01T00:00:00+00:00",
		effective_until: null,
		currency: "USD",
		unit_prices: { input: "0.000003", output: "0.000015" },
		evidence_source: "https://example.com/pricing",
		evidence_sha256: "a".repeat(64),
		observed_at: "2026-09-17T00:00:00+00:00",
		qualification: "qualified" as const,
		registered_at: "2026-09-17T00:00:00+00:00",
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json(dummyCard);
		},
	);

	const card = await client.rateCard(rateCardId);
	expect(request?.url).toBe(`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/rate-cards/${rateCardId}`);
	expect(request?.method).toBe("GET");
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(request?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(card).toEqual(dummyCard);
});

test("rateCard surfaces typed service error on not found", async () => {
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async () =>
			Response.json(
				{
					error: {
						code: "invalid_request",
						request_id: null,
						correlation_id: null,
						diagnostics: ["not_found", "rate card not found in workspace"],
					},
				},
				{ status: 400 },
			),
	);

	const err = await client.rateCard("00000000-0000-7000-8000-000000000099").catch(e => e);
	expect(err).toBeInstanceOf(WorkError);
	expect((err as WorkError).code).toBe("invalid_request");
	expect((err as WorkError).status).toBe(400);
	expect(String(err)).toContain("not_found");
});

test("budgetScopes fetches workspace budget-scope list view with contract headers", async () => {
	let request: Request | undefined;
	const dummyListView = {
		workspace_id: ENV.workspace_id,
		scopes: [
			{
				scope_id: "00000000-0000-7000-8000-000000000011",
				workspace_id: ENV.workspace_id,
				parent_scope_id: null,
				kind: "session" as const,
				policy_version: "economy-v1",
				work_id: "00000000-0000-7000-8000-000000000012",
				session_id: "session-xyz",
				limits: { included_credit: "10.00" },
				held: { included_credit: "2.50" },
				spent: { included_credit: "1.00" },
				unresolved: {},
			},
		],
		next_cursor: "cur-abc",
		limit: 50,
		exhausted: false,
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json(dummyListView);
		},
	);

	const view = await client.budgetScopes({
		workId: "00000000-0000-7000-8000-000000000012",
		sessionId: "session-xyz",
		kind: "session",
		cursor: "cur-start",
		limit: 50,
	});
	expect(request?.url).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/budget-scopes?work_id=00000000-0000-7000-8000-000000000012&session_id=session-xyz&kind=session&cursor=cur-start&limit=50`,
	);
	expect(request?.method).toBe("GET");
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(request?.headers.get("x-omp-workspace-id")).toBe(ENV.workspace_id);
	expect(request?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(view).toEqual(dummyListView);
});

test("budgetScope encodes scopeId URL and returns single BudgetScope", async () => {
	let request: Request | undefined;
	const scopeId = "test/scope id 01";
	const dummyScope = {
		scope_id: scopeId,
		workspace_id: ENV.workspace_id,
		parent_scope_id: null,
		kind: "account" as const,
		policy_version: "economy-v1",
		work_id: null,
		session_id: null,
		limits: { cash: "100.00" },
		held: {},
		spent: {},
		unresolved: {},
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json(dummyScope);
		},
	);

	const scope = await client.budgetScope(scopeId);
	expect(request?.url).toBe(
		`http://127.0.0.1:54322/v1/workspaces/${ENV.workspace_id}/budget-scopes/test%2Fscope%20id%2001`,
	);
	expect(request?.method).toBe("GET");
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(request?.headers.get("x-omp-contract-sha256")).toBe(WORK_CONTRACT_SHA256);
	expect(scope).toEqual(dummyScope);
});

test("budgetScope surfaces typed service error on not found", async () => {
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async () =>
			Response.json(
				{
					error: {
						code: "invalid_request",
						request_id: null,
						correlation_id: null,
						diagnostics: ["not_found", "budget scope not found in workspace"],
					},
				},
				{ status: 400 },
			),
	);

	const err = await client.budgetScope("00000000-0000-7000-8000-000000000099").catch(e => e);
	expect(err).toBeInstanceOf(WorkError);
	expect((err as WorkError).code).toBe("invalid_request");
	expect((err as WorkError).status).toBe(400);
	expect(String(err)).toContain("not_found");
});

test("executes budget and provider-account commands with typed results", async () => {
	const reservationId = "00000000-0000-7000-8000-000000000010";
	const accountId = "00000000-0000-7000-8000-000000000020";
	const dummyAccount = {
		account_id: accountId,
		workspace_id: ENV.workspace_id,
		provider: "gemini",
		account_identity: "acct-3",
		entitlement_evidence: "evidence-3",
		evidence_observed_at: "2026-09-17T00:00:00+00:00",
		billing_mode: "subscription" as const,
		rate_card_version: null,
		observed_balance: null,
		balance_provenance: "unknown" as const,
		reset_at: null,
		concurrency_limit: 1,
	};

	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (_input, init) => {
			const body = JSON.parse(String(init?.body));
			if (body.command.type === "reserve_budget") {
				return Response.json({
					receipt: RECEIPT,
					result: {
						type: "reserve_budget",
						scope_id: body.command.payload.scope_id,
						reservation_id: reservationId,
						fence: 1,
						state: "reserved_unsent",
					},
				});
			}
			return Response.json({
				receipt: RECEIPT,
				result: {
					type: "put_provider_account",
					status: "inserted",
					account_id: accountId,
					account: dummyAccount,
				},
			});
		},
	);

	const reserveRes = await client.execute({
		...ENV,
		command: {
			type: "reserve_budget",
			payload: {
				scope_id: "00000000-0000-7000-8000-000000000001",
				account_id: accountId,
				logical_call_id: "00000000-0000-7000-8000-000000000002",
				transport_attempt_id: "00000000-0000-7000-8000-000000000003",
				provider: "gemini",
				model: "gemini-3.8-flash",
				effort: "high",
				resource: "native_quota",
				worst_case_drawdown: "1.0",
				context_limit: 128000,
				output_limit: 8192,
				expires_at: "2026-09-17T01:00:00Z",
			},
		},
	});
	expect(reserveRes.result.type).toBe("reserve_budget");
	if (reserveRes.result.type === "reserve_budget") {
		expect(reserveRes.result.reservation_id).toBe(reservationId);
		expect(reserveRes.result.fence).toBe(1);
		expect(reserveRes.result.state).toBe("reserved_unsent");
	}

	const accountRes = await client.execute({
		...ENV,
		command: {
			type: "put_provider_account",
			payload: {
				account_id: accountId,
				provider: "gemini",
				account_identity: "acct-3",
				entitlement_evidence: "evidence-3",
				evidence_observed_at: "2026-09-17T00:00:00+00:00",
				billing_mode: "subscription",
				balance_provenance: "unknown",
				concurrency_limit: 1,
			},
		},
	});
	expect(accountRes.result.type).toBe("put_provider_account");
	if (accountRes.result.type === "put_provider_account") {
		expect(accountRes.result.status).toBe("inserted");
		expect(accountRes.result.account_id).toBe(accountId);
		expect(accountRes.result.account.provider).toBe("gemini");
	}
});

test("executes register_rate_card and decodes typed RateCardResult", async () => {
	let request: Request | undefined;
	const rateCardId = "00000000-0000-7000-8000-000000000001";
	const dummyCard = {
		rate_card_id: rateCardId,
		workspace_id: ENV.workspace_id,
		provider: "anthropic",
		version: "2026-09",
		billing_modes: ["metered" as const],
		effective_from: "2026-09-01T00:00:00+00:00",
		effective_until: null,
		currency: "USD",
		unit_prices: { input: "0.000003", output: "0.000015" },
		evidence_source: "https://example.com/pricing",
		evidence_sha256: "a".repeat(64),
		observed_at: "2026-09-17T00:00:00+00:00",
		qualification: "qualified" as const,
		registered_at: "2026-09-17T00:00:00+00:00",
	};
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			request = new Request(String(input), init);
			return Response.json({
				receipt: RECEIPT,
				result: {
					type: "register_rate_card",
					status: "inserted",
					rate_card_id: rateCardId,
					rate_card: dummyCard,
				},
			});
		},
	);

	const res = await client.execute({
		...ENV,
		command: {
			type: "register_rate_card",
			payload: {
				rate_card_id: rateCardId,
				provider: "anthropic",
				version: "2026-09",
				billing_modes: ["metered"],
				effective_from: "2026-09-01T00:00:00+00:00",
				currency: "USD",
				unit_prices: { input: "0.000003", output: "0.000015" },
				evidence_source: "https://example.com/pricing",
				evidence_sha256: "a".repeat(64),
				observed_at: "2026-09-17T00:00:00+00:00",
				qualification: "qualified",
			},
		},
	});

	expect(request?.method).toBe("POST");
	expect(request?.url).toBe("http://127.0.0.1:54322/v1/commands");
	expect(res.result.type).toBe("register_rate_card");
	if (res.result.type === "register_rate_card") {
		expect(res.result.status).toBe("inserted");
		expect(res.result.rate_card_id).toBe(rateCardId);
		expect(res.result.rate_card.provider).toBe("anthropic");
		expect(res.result.rate_card.qualification).toBe("qualified");
	}
});

test("executes quote_budget and reserve_budget with quote_id", async () => {
	const quoteId = "00000000-0000-7000-8000-000000000030";
	const accountId = "00000000-0000-7000-8000-000000000020";
	const rateCardId = "00000000-0000-7000-8000-000000000040";
	const workId = "00000000-0000-7000-8000-000000000050";
	const dummyQuote: BudgetQuote = {
		quote_id: quoteId,
		workspace_id: ENV.workspace_id,
		work_id: workId,
		revision_id: null,
		candidate_id: null,
		attempt_id: null,
		grant_id: null,
		role: "implement",
		launch_id: null,
		account_id: accountId,
		account_evidence_observed_at: "2026-09-17T00:00:00+00:00",
		provider: "gemini",
		model: "gemini-3.8-flash",
		effort: "high",
		rate_card_id: rateCardId,
		rate_card_version: "v1",
		currency: "USD",
		usage_ceiling: { input: 1000, output: 200 },
		worst_case_amount: "0.006",
		evidence_sha256: "a".repeat(64),
		quote_sha256: "b".repeat(64),
		quoted_at: "2026-09-17T00:00:00+00:00",
		resource: "native_quota",
		scope_id: "00000000-0000-7000-8000-000000000001",
	};

	let capturedReservePayload: any = null;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (_input, init) => {
			const body = JSON.parse(String(init?.body));
			if (body.command.type === "quote_budget") {
				return Response.json({
					receipt: RECEIPT,
					result: {
						type: "quote_budget",
						quote: dummyQuote,
					},
				});
			}
			if (body.command.type === "reserve_budget") {
				capturedReservePayload = body.command.payload;
				return Response.json({
					receipt: RECEIPT,
					result: {
						type: "reserve_budget",
						scope_id: body.command.payload.scope_id,
						reservation_id: "00000000-0000-7000-8000-000000000010",
						fence: 1,
						state: "reserved_unsent",
					},
				});
			}
			return Response.json({ error: "unexpected" }, { status: 500 });
		},
	);

	const quoteRes = await client.execute({
		...ENV,
		command: {
			type: "quote_budget",
			payload: {
				work_id: workId,
				role: "implement",
				provider: "gemini",
				model: "gemini-3.8-flash",
				effort: "high",
				currency: "USD",
				usage_ceiling: { input: 1000, output: 200 },
			},
		},
	});
	expect(quoteRes.result.type).toBe("quote_budget");
	if (quoteRes.result.type === "quote_budget") {
		expect(quoteRes.result.quote.quote_id).toBe(quoteId);
		expect(quoteRes.result.quote.worst_case_amount).toBe("0.006");
		expect(quoteRes.result.quote.rate_card_id).toBe(rateCardId);
	}

	const reserveRes = await client.execute({
		...ENV,
		command: {
			type: "reserve_budget",
			payload: {
				scope_id: "00000000-0000-7000-8000-000000000001",
				account_id: accountId,
				logical_call_id: "00000000-0000-7000-8000-000000000002",
				transport_attempt_id: "00000000-0000-7000-8000-000000000003",
				provider: "gemini",
				model: "gemini-3.8-flash",
				effort: "high",
				resource: "native_quota",
				worst_case_drawdown: "0.006",
				context_limit: 128000,
				output_limit: 8192,
				expires_at: "2026-09-17T01:00:00Z",
				quote_id: quoteId,
			},
		},
	});
	expect(reserveRes.result.type).toBe("reserve_budget");
	expect(capturedReservePayload.quote_id).toBe(quoteId);
});

test("executes record_stage_preflight and decodes typed result and workflow stage_preflights", async () => {
	let commandRequest: Request | undefined;
	let workflowRequest: Request | undefined;

	const preflightId = "00000000-0000-7000-8000-000000000010";
	const workId = "00000000-0000-7000-8000-000000000020";
	const transportAttemptId = "00000000-0000-7000-8000-000000000050";
	const taskSha256 = "a".repeat(64);
	const probeSha256 = "b".repeat(64);

	const mockPreflight = {
		preflight_id: preflightId,
		workspace_id: ENV.workspace_id,
		work_id: workId,
		revision_id: null,
		candidate_id: null,
		attempt_id: null,
		grant_id: null,
		session_id: null,
		role: "implement" as const,
		tool_call_id: "call-1",
		task_sha256: taskSha256,
		probe_sha256: probeSha256,
		transport_attempt_id: transportAttemptId,
		ordinal: 1,
		requested_selector: "gemini:gemini-3.8-flash",
		requested_provider: "gemini",
		requested_model: "gemini-3.8-flash",
		requested_api: "google-genai",
		requested_effort: "medium",
		requested_wire_model: "gemini-3.8-flash-preview",
		is_fallback: false,
		outcome: "selected" as const,
		stop_reason: null,
		error: null,
		requests: null,
		usage: {
			input: 100,
			output: 50,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 150,
			orchestration: { input: 100, cacheRead: 0, output: 50 },
			cttl: null,
			server: null,
		},
		provider_request_id: null,
		observed_at: "2026-09-17T01:00:00Z",
	};

	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			const req = new Request(String(input), init);
			if (req.url.endsWith("/v1/commands")) {
				commandRequest = req;
				return Response.json({
					receipt: RECEIPT,
					result: {
						type: "record_stage_preflight",
						status: "applied",
						preflight: mockPreflight,
					},
				});
			}
			if (req.url.includes("/workflow")) {
				workflowRequest = req;
				return Response.json({
					item: {
						work_id: workId,
						key: "W-1",
						workspace_id: ENV.workspace_id,
						state: "IN_PROGRESS",
						effort: "medium",
						title: "Work Item 1",
						labels: [],
						parent_work_id: null,
						created_by: "user-1",
						created_at: "2026-09-17T00:00:00Z",
						updated_at: "2026-09-17T00:00:00Z",
						row_version: 1,
					},
					relations: [],
					receipts: [],
					close_attempts: [],
					audit_manifest: null,
					auditor_launches: [],
					stage_launches: [],
					stage_preflights: [mockPreflight],
					candidate_source_versions: [],
					close_attempt_events: [],
					checkpoint_deliveries: [],
					project: null,
				});
			}
			return new Response(null, { status: 404 });
		},
	);

	const preflightRes = await client.execute({
		...ENV,
		command: {
			type: "record_stage_preflight",
			payload: {
				work_id: workId,
				role: "implement",
				tool_call_id: "call-1",
				task_sha256: taskSha256,
				probe_sha256: probeSha256,
				transport_attempt_id: transportAttemptId,
				ordinal: 1,
				requested_selector: "gemini:gemini-3.8-flash",
				requested_provider: "gemini",
				requested_model: "gemini-3.8-flash",
				requested_api: "google-genai",
				requested_effort: "medium",
				requested_wire_model: "gemini-3.8-flash-preview",
				outcome: "selected",
				usage: {
					input: 100,
					output: 50,
					cacheRead: 0,
					cacheWrite: 0,
					totalTokens: 150,
					orchestration: { input: 100, cacheRead: 0, output: 50 },
					cttl: null,
					server: null,
				},
			},
		},
	});

	expect(commandRequest?.url).toBe("http://127.0.0.1:54322/v1/commands");
	expect(preflightRes.result.type).toBe("record_stage_preflight");
	if (preflightRes.result.type === "record_stage_preflight") {
		expect(preflightRes.result.status).toBe("applied");
		expect(preflightRes.result.preflight?.tool_call_id).toBe("call-1");
		expect(preflightRes.result.preflight?.probe_sha256).toBe(probeSha256);
		expect(preflightRes.result.preflight?.role).toBe("implement");
		expect(preflightRes.result.preflight?.outcome).toBe("selected");
		expect(preflightRes.result.preflight?.usage?.orchestration?.input).toBe(100);
	}

	const wf = await client.workflow("W-1");
	expect(workflowRequest?.url).toBe("http://127.0.0.1:54322/v1/work-items/W-1/workflow");
	expect(wf.stage_preflights).toBeDefined();
	expect(wf.stage_preflights.length).toBe(1);
	expect(wf.stage_preflights[0].preflight_id).toBe(preflightId);
	expect(wf.stage_preflights[0].outcome).toBe("selected");
	expect(wf.stage_preflights[0].tool_call_id).toBe("call-1");
});

test("executes begin_stage_preflight and decodes typed applied and replayed result", async () => {
	const workId = "00000000-0000-0000-0000-000000000021";
	const taskSha256 = "11".repeat(32);
	const probeSha256 = "22".repeat(32);
	const intentId = "00000000-0000-0000-0000-000000000031";
	const transportAttemptId = "00000000-0000-0000-0000-000000000041";
	const logicalSha256 = "33".repeat(32);
	const groupSha256 = "44".repeat(32);

	let commandRequest: Request | undefined;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			commandRequest = new Request(String(input), init);
			return Response.json({
				api_version: "work.omp.dev/v1",
				workspace_id: ENV.workspace_id,
				operation_id: ENV.operation_id,
				request_id: ENV.request_id,
				result: {
					type: "begin_stage_preflight",
					status: "applied",
					intent: {
						intent_id: intentId,
						workspace_id: ENV.workspace_id,
						work_id: workId,
						revision_id: null,
						candidate_id: null,
						attempt_id: null,
						grant_id: null,
						role: "implement",
						tool_call_id: "call-1",
						task_sha256: taskSha256,
						probe_sha256: probeSha256,
						transport_attempt_id: transportAttemptId,
						ordinal: 0,
						requested_selector: "gemini:gemini-3.8-flash",
						requested_provider: "gemini",
						requested_model: "gemini-3.8-flash",
						requested_api: "google-genai",
						requested_effort: "medium",
						requested_wire_model: "gemini-3.8-flash-preview",
						is_fallback: false,
						logical_sha256: logicalSha256,
						group_sha256: groupSha256,
						host_owner_id: ENV.correlation_id,
						status: "begun",
						created_at: "2026-09-17T12:00:00+00:00",
						settled_at: null,
					},
					preflight: null,
				},
				result_sha256: "55".repeat(32),
				diagnostics: [],
			});
		},
	);

	const res = await client.execute({
		...ENV,
		command: {
			type: "begin_stage_preflight",
			payload: {
				work_id: workId,
				role: "implement",
				tool_call_id: "call-1",
				task_sha256: taskSha256,
				probe_sha256: probeSha256,
				ordinal: 0,
				requested_selector: "gemini:gemini-3.8-flash",
				requested_provider: "gemini",
				requested_model: "gemini-3.8-flash",
				requested_api: "google-genai",
				requested_effort: "medium",
				requested_wire_model: "gemini-3.8-flash-preview",
				is_fallback: false,
			},
		},
	});

	expect(commandRequest?.url).toBe("http://127.0.0.1:54322/v1/commands");
	expect(res.result.type).toBe("begin_stage_preflight");
	if (res.result.type === "begin_stage_preflight") {
		expect(res.result.status).toBe("applied");
		expect(res.result.intent.intent_id).toBe(intentId);
		expect(res.result.intent.transport_attempt_id).toBe(transportAttemptId);
		expect(res.result.intent.status).toBe("begun");
		expect(res.result.intent.logical_sha256).toBe(logicalSha256);
		expect(res.result.preflight).toBeNull();
	}
});

test("executes admit_stage_preflight and decodes typed applied result", async () => {
	const intentId = "00000000-0000-0000-0000-000000000031";
	const transportAttemptId = "00000000-0000-0000-0000-000000000041";
	const logicalSha256 = "33".repeat(32);

	let commandRequest: Request | undefined;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			commandRequest = new Request(String(input), init);
			return Response.json({
				api_version: "work.omp.dev/v1",
				workspace_id: ENV.workspace_id,
				operation_id: ENV.operation_id,
				request_id: ENV.request_id,
				result: {
					type: "admit_stage_preflight",
					status: "applied",
					intent: {
						intent_id: intentId,
						workspace_id: ENV.workspace_id,
						work_id: "00000000-0000-0000-0000-000000000021",
						revision_id: null,
						candidate_id: null,
						attempt_id: null,
						grant_id: null,
						role: "implement",
						tool_call_id: "call-1",
						task_sha256: "11".repeat(32),
						probe_sha256: "22".repeat(32),
						transport_attempt_id: transportAttemptId,
						ordinal: 0,
						requested_selector: "gemini:gemini-3.8-flash",
						requested_provider: "gemini",
						requested_model: "gemini-3.8-flash",
						requested_api: "google-genai",
						requested_effort: "medium",
						requested_wire_model: "gemini-3.8-flash-preview",
						is_fallback: false,
						logical_sha256: logicalSha256,
						group_sha256: "44".repeat(32),
						host_owner_id: ENV.correlation_id,
						dispatched_at: "2026-09-17T12:01:00+00:00",
						dispatch_operation_id: ENV.operation_id,
						dispatch_owner_id: ENV.correlation_id,
						cancelled_at: null,
						cancelled_by: null,
						cancel_reason: null,
						status: "dispatched",
						created_at: "2026-09-17T12:00:00+00:00",
						settled_at: null,
					},
					preflight: null,
				},
				result_sha256: "55".repeat(32),
				diagnostics: [],
			});
		},
	);

	const res = await client.execute({
		...ENV,
		command: {
			type: "admit_stage_preflight",
			payload: {
				transport_attempt_id: transportAttemptId,
				logical_sha256: logicalSha256,
			},
		},
	});

	expect(commandRequest?.url).toBe("http://127.0.0.1:54322/v1/commands");
	expect(res.result.type).toBe("admit_stage_preflight");
	if (res.result.type === "admit_stage_preflight") {
		expect(res.result.status).toBe("applied");
		expect(res.result.intent.intent_id).toBe(intentId);
		expect(res.result.intent.status).toBe("dispatched");
		expect(res.result.intent.dispatch_owner_id).toBe(ENV.correlation_id);
	}
});

test("executes cancel_stage_preflight and decodes typed applied result", async () => {
	const intentId = "00000000-0000-0000-0000-000000000031";
	const transportAttemptId = "00000000-0000-0000-0000-000000000041";
	const logicalSha256 = "33".repeat(32);

	let commandRequest: Request | undefined;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			commandRequest = new Request(String(input), init);
			return Response.json({
				api_version: "work.omp.dev/v1",
				workspace_id: ENV.workspace_id,
				operation_id: ENV.operation_id,
				request_id: ENV.request_id,
				result: {
					type: "cancel_stage_preflight",
					status: "applied",
					intent: {
						intent_id: intentId,
						workspace_id: ENV.workspace_id,
						work_id: "00000000-0000-0000-0000-000000000021",
						revision_id: null,
						candidate_id: null,
						attempt_id: null,
						grant_id: null,
						role: "implement",
						tool_call_id: "call-1",
						task_sha256: "11".repeat(32),
						probe_sha256: "22".repeat(32),
						transport_attempt_id: transportAttemptId,
						ordinal: 0,
						requested_selector: "gemini:gemini-3.8-flash",
						requested_provider: "gemini",
						requested_model: "gemini-3.8-flash",
						requested_api: "google-genai",
						requested_effort: "medium",
						requested_wire_model: "gemini-3.8-flash-preview",
						is_fallback: false,
						logical_sha256: logicalSha256,
						group_sha256: "44".repeat(32),
						host_owner_id: "00000000-0000-0000-0000-000000000099",
						dispatched_at: null,
						dispatch_operation_id: null,
						dispatch_owner_id: null,
						cancelled_at: "2026-09-17T12:02:00+00:00",
						cancelled_by: ENV.correlation_id,
						cancel_reason: "runner recovery reissue",
						status: "cancelled_undispatched",
						created_at: "2026-09-17T12:00:00+00:00",
						settled_at: null,
					},
					preflight: null,
				},
				result_sha256: "55".repeat(32),
				diagnostics: [],
			});
		},
	);

	const res = await client.execute({
		...ENV,
		command: {
			type: "cancel_stage_preflight",
			payload: {
				transport_attempt_id: transportAttemptId,
				logical_sha256: logicalSha256,
				reason: "runner recovery reissue",
			},
		},
	});

	expect(commandRequest?.url).toBe("http://127.0.0.1:54322/v1/commands");
	expect(res.result.type).toBe("cancel_stage_preflight");
	if (res.result.type === "cancel_stage_preflight") {
		expect(res.result.status).toBe("applied");
		expect(res.result.intent.intent_id).toBe(intentId);
		expect(res.result.intent.status).toBe("cancelled_undispatched");
		expect(res.result.intent.cancelled_by).toBe(ENV.correlation_id);
		expect(res.result.intent.cancel_reason).toBe("runner recovery reissue");
	}
});

test("executes reconcile_stage_preflight with completed disposition and decodes typed applied result", async () => {
	const reconciliationId = "00000000-0000-0000-0000-000000000051";
	const transportAttemptId = "00000000-0000-0000-0000-000000000052";
	const accountId = "00000000-0000-0000-0000-000000000053";
	const observationId = "00000000-0000-0000-0000-000000000054";
	const preflightId = "00000000-0000-0000-0000-000000000055";
	const logicalSha256 = "66".repeat(32);
	const evidenceSha256 = "77".repeat(32);

	let commandRequest: Request | undefined;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			commandRequest = new Request(String(input), init);
			return Response.json({
				api_version: "work.omp.dev/v1",
				workspace_id: ENV.workspace_id,
				operation_id: ENV.operation_id,
				request_id: ENV.request_id,
				result: {
					type: "reconcile_stage_preflight",
					status: "applied",
					reconciliation: {
						reconciliation_id: reconciliationId,
						workspace_id: ENV.workspace_id,
						transport_attempt_id: transportAttemptId,
						account_id: accountId,
						observation_id: observationId,
						disposition: "completed",
						observed_at: "2026-09-17T12:05:00+00:00",
						evidence_sha256: evidenceSha256,
						provider_request_id: "req-12345",
						requests: 1,
						usage: {
							input: 120,
							output: 40,
							cacheRead: 0,
							cacheWrite: 0,
							totalTokens: 160,
							orchestration: { input: 120, cacheRead: 0, output: 40 },
							cttl: null,
							server: null,
						},
						stop_reason: "stop",
						error: null,
						reconciled_at: "2026-09-17T12:05:01+00:00",
					},
					intent: {
						intent_id: "00000000-0000-0000-0000-000000000056",
						workspace_id: ENV.workspace_id,
						work_id: "00000000-0000-0000-0000-000000000021",
						revision_id: null,
						candidate_id: null,
						attempt_id: null,
						grant_id: null,
						role: "implement",
						tool_call_id: "call-1",
						task_sha256: "11".repeat(32),
						probe_sha256: "22".repeat(32),
						transport_attempt_id: transportAttemptId,
						ordinal: 0,
						requested_selector: "gemini:gemini-3.8-flash",
						requested_provider: "gemini",
						requested_model: "gemini-3.8-flash",
						requested_api: "google-genai",
						requested_effort: "medium",
						requested_wire_model: "gemini-3.8-flash-preview",
						is_fallback: false,
						logical_sha256: logicalSha256,
						group_sha256: "44".repeat(32),
						host_owner_id: "00000000-0000-0000-0000-000000000099",
						dispatched_at: "2026-09-17T12:00:00+00:00",
						dispatch_operation_id: "00000000-0000-0000-0000-000000000098",
						dispatch_owner_id: "00000000-0000-0000-0000-000000000097",
						cancelled_at: null,
						cancelled_by: null,
						cancel_reason: null,
						status: "settled",
						created_at: "2026-09-17T12:00:00+00:00",
						settled_at: "2026-09-17T12:05:01+00:00",
					},
					preflight: {
						preflight_id: preflightId,
						workspace_id: ENV.workspace_id,
						work_id: "00000000-0000-0000-0000-000000000021",
						revision_id: null,
						candidate_id: null,
						attempt_id: null,
						grant_id: null,
						role: "implement",
						tool_call_id: "call-1",
						task_sha256: "11".repeat(32),
						probe_sha256: "22".repeat(32),
						transport_attempt_id: transportAttemptId,
						ordinal: 0,
						requested_selector: "gemini:gemini-3.8-flash",
						requested_provider: "gemini",
						requested_model: "gemini-3.8-flash",
						requested_api: "google-genai",
						requested_effort: "medium",
						requested_wire_model: "gemini-3.8-flash-preview",
						is_fallback: false,
						outcome: "selected",
						stop_reason: "stop",
						error: null,
						requests: 1,
						usage: {
							input: 120,
							output: 40,
							cacheRead: 0,
							cacheWrite: 0,
							totalTokens: 160,
							orchestration: { input: 120, cacheRead: 0, output: 40 },
							cttl: null,
							server: null,
						},
						provider_request_id: "req-12345",
						observed_at: "2026-09-17T12:05:00+00:00",
					},
					reason: null,
				},
				result_sha256: "88".repeat(32),
				diagnostics: [],
			});
		},
	);

	const res = await client.execute({
		...ENV,
		command: {
			type: "reconcile_stage_preflight",
			payload: {
				transport_attempt_id: transportAttemptId,
				logical_sha256: logicalSha256,
				account_id: accountId,
				observation_id: observationId,
				observed_at: "2026-09-17T12:05:00+00:00",
				evidence_sha256: evidenceSha256,
				disposition: "completed",
				requested_provider: "gemini",
				provider_request_id: "req-12345",
				requests: 1,
				usage: {
					input: 120,
					output: 40,
					cacheRead: 0,
					cacheWrite: 0,
					totalTokens: 160,
					orchestration: { input: 120, cacheRead: 0, output: 40 },
					cttl: null,
					server: null,
				},
				stop_reason: "stop",
			},
		},
	});

	expect(commandRequest?.url).toBe("http://127.0.0.1:54322/v1/commands");
	expect(res.result.type).toBe("reconcile_stage_preflight");
	if (res.result.type === "reconcile_stage_preflight") {
		expect(res.result.status).toBe("applied");
		expect(res.result.reconciliation?.reconciliation_id).toBe(reconciliationId);
		expect(res.result.reconciliation?.disposition).toBe("completed");
		expect(res.result.reconciliation?.provider_request_id).toBe("req-12345");
		expect(res.result.intent?.status).toBe("settled");
		expect(res.result.preflight?.outcome).toBe("selected");
		expect(res.result.preflight?.preflight_id).toBe(preflightId);
	}
});

test("executes reconcile_stage_preflight and decodes typed replayed and refused results", async () => {
	const reconciliationId = "00000000-0000-0000-0000-000000000061";
	const transportAttemptId = "00000000-0000-0000-0000-000000000062";
	const accountId = "00000000-0000-0000-0000-000000000063";
	const observationId = "00000000-0000-0000-0000-000000000064";
	const logicalSha256 = "66".repeat(32);
	const evidenceSha256 = "77".repeat(32);

	let returnStatus: "replayed" | "refused" = "replayed";
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async () => {
			if (returnStatus === "replayed") {
				return Response.json({
					api_version: "work.omp.dev/v1",
					workspace_id: ENV.workspace_id,
					operation_id: ENV.operation_id,
					request_id: ENV.request_id,
					result: {
						type: "reconcile_stage_preflight",
						status: "replayed",
						reconciliation: {
							reconciliation_id: reconciliationId,
							workspace_id: ENV.workspace_id,
							transport_attempt_id: transportAttemptId,
							account_id: accountId,
							observation_id: observationId,
							disposition: "indeterminate",
							observed_at: "2026-09-17T12:05:00+00:00",
							evidence_sha256: evidenceSha256,
							reconciled_at: "2026-09-17T12:05:01+00:00",
						},
						intent: null,
						preflight: null,
						reason: "exact_reconciliation_replayed",
					},
					result_sha256: "88".repeat(32),
					diagnostics: [],
				});
			}
			return Response.json({
				api_version: "work.omp.dev/v1",
				workspace_id: ENV.workspace_id,
				operation_id: ENV.operation_id,
				request_id: ENV.request_id,
				result: {
					type: "reconcile_stage_preflight",
					status: "refused",
					reconciliation: null,
					intent: null,
					preflight: null,
					reason: "intent_already_settled",
				},
				result_sha256: "88".repeat(32),
				diagnostics: [],
			});
		},
	);

	const replayedRes = await client.execute({
		...ENV,
		command: {
			type: "reconcile_stage_preflight",
			payload: {
				transport_attempt_id: transportAttemptId,
				logical_sha256: logicalSha256,
				account_id: accountId,
				observation_id: observationId,
				observed_at: "2026-09-17T12:05:00+00:00",
				evidence_sha256: evidenceSha256,
				disposition: "indeterminate",
			},
		},
	});

	expect(replayedRes.result.type).toBe("reconcile_stage_preflight");
	if (replayedRes.result.type === "reconcile_stage_preflight") {
		expect(replayedRes.result.status).toBe("replayed");
		expect(replayedRes.result.reconciliation?.disposition).toBe("indeterminate");
		expect(replayedRes.result.reason).toBe("exact_reconciliation_replayed");
	}

	returnStatus = "refused";
	const refusedRes = await client.execute({
		...ENV,
		command: {
			type: "reconcile_stage_preflight",
			payload: {
				transport_attempt_id: transportAttemptId,
				logical_sha256: logicalSha256,
				account_id: accountId,
				observation_id: observationId,
				observed_at: "2026-09-17T12:05:00+00:00",
				evidence_sha256: evidenceSha256,
				disposition: "indeterminate",
			},
		},
	});

	expect(refusedRes.result.type).toBe("reconcile_stage_preflight");
	if (refusedRes.result.type === "reconcile_stage_preflight") {
		expect(refusedRes.result.status).toBe("refused");
		expect(refusedRes.result.reconciliation).toBeNull();
		expect(refusedRes.result.reason).toBe("intent_already_settled");
	}
});

test("dispatches research commands, decodes results, and calls research view", async () => {
	let lastUrl: string | undefined;
	let lastMethod: string | undefined;

	const campaignId = "00000000-0000-0000-0000-0000000000c1";
	const workId = "00000000-0000-0000-0000-0000000000w1";
	const revisionId = "00000000-0000-0000-0000-0000000000r1";
	const trialId = "00000000-0000-0000-0000-0000000000t1";
	const observationId = "00000000-0000-0000-0000-0000000000o1";
	const candidateId = "00000000-0000-0000-0000-0000000000d1";

	const mockCampaign: ResearchCampaign = {
		campaign_id: campaignId,
		workspace_id: ENV.workspace_id,
		work_id: workId,
		revision_id: revisionId,
		domain: "machine_learning",
		spec: {
			objective: "test objective",
			evaluation_protocol_id: "proto-1",
			evaluation_protocol_sha256: "0".repeat(64),
			resource_policy_ref: "pol-1",
			candidate_mapping_policy: "map-1",
		},
		spec_sha256: "1".repeat(64),
		policy_sha256: "2".repeat(64),
		state: "admitted",
		created_at: "2026-09-17T12:00:00Z",
		admitted_at: "2026-09-17T12:01:00Z",
	};

	const mockTrial: ResearchTrial = {
		trial_id: trialId,
		workspace_id: ENV.workspace_id,
		campaign_id: campaignId,
		work_id: workId,
		decision_id: "00000000-0000-0000-0000-0000000000e1",
		candidate_digest: "3".repeat(64),
		experiment_spec_sha256: "4".repeat(64),
		evaluator_sha256: "5".repeat(64),
		environment_sha256: "6".repeat(64),
		input_manifest_sha256: "7".repeat(64),
		policy_sha256: "2".repeat(64),
		state: "proposed",
		proposed_at: "2026-09-17T12:02:00Z",
		action: "evaluate",
		reason: "test proposal",
	};

	const mockObservation: ResearchObservation = {
		observation_id: observationId,
		workspace_id: ENV.workspace_id,
		campaign_id: campaignId,
		trial_id: trialId,
		issuer_kind: "candidate_authored",
		source_ref: "subtrial/0",
		execution_status: "completed",
		payload: { score: 0.95 },
		payload_sha256: "8".repeat(64),
		observed_at: "2026-09-17T12:03:00Z",
		recorded_at: "2026-09-17T12:03:01Z",
	};

	const mockBinding: ResearchDeliverableBinding = {
		trial_id: trialId,
		workspace_id: ENV.workspace_id,
		campaign_id: campaignId,
		work_id: workId,
		revision_id: revisionId,
		candidate_digest: "3".repeat(64),
		native_candidate_id: candidateId,
		binding_sha256: "9".repeat(64),
		bound_at: "2026-09-17T12:04:00Z",
	};

	let nextResult: unknown;

	const client = new WorkClient(
		"http://127.0.0.1:54322",
		ENV.workspace_id,
		() => "token",
		async (input, init) => {
			lastUrl = String(input);
			lastMethod = init?.method;
			return Response.json(nextResult);
		},
	);

	// 0. register_research_component
	const mockComponent: ResearchComponent = {
		component_sha256: "2".repeat(64),
		workspace_id: ENV.workspace_id,
		kind: "policy",
		descriptor: {
			contract_version: "research-component.v1",
			kind: "policy",
			name: "test-policy",
			version: "1",
			artifact_sha256: "a".repeat(64),
			roles: [],
			capabilities: [],
		},
		registered_at: new Date().toISOString(),
	};
	nextResult = {
		receipt: RECEIPT,
		result: { type: "register_research_component", status: "applied", component: mockComponent },
	};
	const regRes = await client.execute({
		...ENV,
		command: {
			type: "register_research_component",
			payload: {
				component_sha256: mockComponent.component_sha256,
				descriptor: mockComponent.descriptor,
			},
		},
	});
	expect(regRes.result.type).toBe("register_research_component");
	if (regRes.result.type === "register_research_component") {
		expect(regRes.result.status).toBe("applied");
		expect(regRes.result.component.component_sha256).toBe(mockComponent.component_sha256);
	}

	// 1. create_research_campaign
	nextResult = {
		receipt: RECEIPT,
		result: { type: "create_research_campaign", status: "applied", campaign: mockCampaign },
	};
	const createRes = await client.execute({
		...ENV,
		command: {
			type: "create_research_campaign",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				revision_id: revisionId,
				domain: "machine_learning",
				spec: mockCampaign.spec,
				spec_sha256: mockCampaign.spec_sha256,
			},
		},
	});
	expect(createRes.result.type).toBe("create_research_campaign");
	if (createRes.result.type === "create_research_campaign") {
		expect(createRes.result.status).toBe("applied");
		expect(createRes.result.campaign.campaign_id).toBe(campaignId);
	}

	// 2. admit_research_campaign
	nextResult = {
		receipt: RECEIPT,
		result: { type: "admit_research_campaign", status: "applied", campaign: mockCampaign },
	};
	const admitRes = await client.execute({
		...ENV,
		command: {
			type: "admit_research_campaign",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				revision_id: revisionId,
				spec_sha256: mockCampaign.spec_sha256,
				policy_sha256: "2".repeat(64),
				compatibility: {
					contract_version: "research-compatibility.v1",
					workers: [],
					evaluators: ["5".repeat(64)],
					audits: [],
					releases: [],
					environments: ["6".repeat(64)],
				},
				compatibility_sha256: "c".repeat(64),
			},
		},
	});
	expect(admitRes.result.type).toBe("admit_research_campaign");

	// 3. propose_research_trial
	nextResult = { receipt: RECEIPT, result: { type: "propose_research_trial", status: "applied", trial: mockTrial } };
	const trialRes = await client.execute({
		...ENV,
		command: {
			type: "propose_research_trial",
			payload: {
				trial_id: trialId,
				campaign_id: campaignId,
				work_id: workId,
				decision_id: "00000000-0000-0000-0000-0000000000e1",
				action: "evaluate",
				reason: "test proposal",
				candidate_digest: "3".repeat(64),
				experiment_spec_sha256: "4".repeat(64),
				evaluator_sha256: "5".repeat(64),
				environment_sha256: "6".repeat(64),
				input_manifest_sha256: "7".repeat(64),
				policy_sha256: "2".repeat(64),
			},
		},
	});
	expect(trialRes.result.type).toBe("propose_research_trial");

	// 4. record_research_observation
	nextResult = {
		receipt: RECEIPT,
		result: { type: "record_research_observation", status: "applied", observation: mockObservation },
	};
	const obsRes = await client.execute({
		...ENV,
		command: {
			type: "record_research_observation",
			payload: {
				observation_id: observationId,
				campaign_id: campaignId,
				trial_id: trialId,
				issuer_kind: "candidate_authored",
				source_ref: "subtrial/0",
				execution_status: "completed",
				payload: { score: 0.95 },
				payload_sha256: "8".repeat(64),
				observed_at: "2026-09-17T12:03:00Z",
			},
		},
	});
	expect(obsRes.result.type).toBe("record_research_observation");

	// 5. bind_research_deliverable
	nextResult = {
		receipt: RECEIPT,
		result: { type: "bind_research_deliverable", status: "applied", deliverable_binding: mockBinding },
	};
	const bindRes = await client.execute({
		...ENV,
		command: {
			type: "bind_research_deliverable",
			payload: {
				trial_id: trialId,
				campaign_id: campaignId,
				work_id: workId,
				revision_id: revisionId,
				candidate_digest: "3".repeat(64),
				native_candidate_id: candidateId,
				binding_sha256: "9".repeat(64),
			},
		},
	});
	expect(bindRes.result.type).toBe("bind_research_deliverable");

	// 6. cancel_research_campaign
	nextResult = {
		receipt: RECEIPT,
		result: {
			type: "cancel_research_campaign",
			status: "applied",
			campaign: { ...mockCampaign, state: "cancelled" },
		},
	};
	const cancelRes = await client.execute({
		...ENV,
		command: {
			type: "cancel_research_campaign",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				reason: "testing cancellation",
			},
		},
	});
	expect(cancelRes.result.type).toBe("cancel_research_campaign");

	// 7. set_research_campaign_state (blocked variant)
	const mockBlockedCampaign: ResearchCampaign = {
		...mockCampaign,
		state: "blocked",
		blocked_dependency: {
			kind: "budget_scope",
			ref: "scope-1",
			reason: "waiting on allocation",
		},
		blocked_from_state: "running",
	};
	nextResult = {
		receipt: RECEIPT,
		result: {
			type: "set_research_campaign_state",
			status: "applied",
			campaign: mockBlockedCampaign,
		},
	};
	const setRes = await client.execute({
		...ENV,
		command: {
			type: "set_research_campaign_state",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				expected_state: "running",
				target_state: "blocked",
				policy_sha256: "2".repeat(64),
				blocked_dependency: {
					kind: "budget_scope",
					ref: "scope-1",
					reason: "waiting on allocation",
				},
			},
		},
	});
	expect(setRes.result.type).toBe("set_research_campaign_state");
	if (setRes.result.type === "set_research_campaign_state") {
		expect(setRes.result.campaign.state).toBe("blocked");
		expect(setRes.result.campaign.blocked_dependency?.kind).toBe("budget_scope");
	}

	// 8. conclude_research_campaign
	const mockConcludedCampaign: ResearchCampaign = {
		...mockCampaign,
		state: "concluded",
		outcome: "supported",
		outcome_reason: "evidence supported",
		concluded_at: "2026-09-17T12:05:00Z",
	};
	nextResult = {
		receipt: RECEIPT,
		result: {
			type: "conclude_research_campaign",
			status: "applied",
			campaign: mockConcludedCampaign,
		},
	};
	const concludeRes = await client.execute({
		...ENV,
		command: {
			type: "conclude_research_campaign",
			payload: {
				campaign_id: campaignId,
				work_id: workId,
				policy_sha256: "2".repeat(64),
				outcome: "supported",
				reason: "evidence supported",
			},
		},
	});
	expect(concludeRes.result.type).toBe("conclude_research_campaign");
	if (concludeRes.result.type === "conclude_research_campaign") {
		expect(concludeRes.result.campaign.state).toBe("concluded");
		expect(concludeRes.result.campaign.outcome).toBe("supported");
	}

	// 9. client.research(key)
	const mockResearchView: ResearchView = {
		work_id: workId,
		campaigns: [mockCampaign],
		trials: [mockTrial],
		observations: [mockObservation],
		deliverable_bindings: [mockBinding],
	};
	nextResult = mockResearchView;
	const view = await client.research("test-work-key");
	expect(lastUrl).toBe("http://127.0.0.1:54322/v1/work-items/test-work-key/research");
	expect(lastMethod).toBe("GET");
	expect(view.work_id).toBe(workId);
	expect(view.campaigns.length).toBe(1);
	expect(view.trials.length).toBe(1);
	expect(view.observations.length).toBe(1);
	expect(view.deliverable_bindings.length).toBe(1);
});
