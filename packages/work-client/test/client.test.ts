import { expect, test } from "bun:test";
import { payloadHash, WORK_CONTRACT_SHA256, WorkClient, WorkError } from "../src/index";

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
