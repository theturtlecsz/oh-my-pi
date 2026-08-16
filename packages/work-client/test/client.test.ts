import { expect, test } from "bun:test";
import { WorkClient, WorkError } from "../src/index";

test("sends the fixed command surface and decodes replay", async () => {
	let request: Request | undefined;
	const client = new WorkClient(
		"http://127.0.0.1:54322",
		"00000000-0000-0000-0000-000000000001",
		() => "token",
		async (input, init) => {
			request = new Request(input, init);
			return Response.json({
				receipt: {
					operation_id: "o",
					request_id: "r",
					state: "replayed",
					request_sha256: "0".repeat(64),
					result_sha256: "1".repeat(64),
					diagnostics: [],
				},
				result: {},
			});
		},
	);
	const result = await client.execute({
		api_version: "work.omp.dev/v1",
		workspace_id: "00000000-0000-0000-0000-000000000001",
		operation_id: "o",
		request_id: "r",
		correlation_id: "c",
		command: { type: "create_work_batch", payload: { work_items: ["one"] } },
	});
	expect(request?.url).toBe("http://127.0.0.1:54322/v1/commands");
	expect(request?.headers.get("authorization")).toBe("Bearer token");
	expect(result.receipt.state).toBe("replayed");
});

test("surfaces service errors", async () => {
	const client = new WorkClient(
		"http://x",
		"workspace",
		() => "token",
		async () =>
			Response.json(
				{ error: { code: "idempotency_conflict", request_id: null, correlation_id: null, diagnostics: [] } },
				{ status: 409 },
			),
	);
	await expect(client.operation("operation")).rejects.toBeInstanceOf(WorkError);
});
