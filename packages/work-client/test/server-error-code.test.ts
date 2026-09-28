import { expect, test } from "bun:test";
import { WorkClient, WorkError } from "../src/index";

const WORKSPACE_ID = "00000000-0000-0000-0000-000000000001";

/** A client whose every response is a plain-text non-JSON body with `status`. */
function textStatusClient(status: number, body: string): WorkClient {
	return new WorkClient(
		"http://127.0.0.1:54322",
		WORKSPACE_ID,
		() => "token",
		async () => new Response(body, { status, headers: { "Content-Type": "text/plain" } }),
	);
}

/** Capture the WorkError surfaced by a command read. */
async function capturedError(client: WorkClient): Promise<WorkError> {
	try {
		await client.tree();
	} catch (error) {
		expect(error).toBeInstanceOf(WorkError);
		return error as WorkError;
	}
	throw new Error("expected the request to reject");
}

test("a 500 with a non-JSON body surfaces unavailable so the host reconciles", async () => {
	const error = await capturedError(textStatusClient(500, "internal error"));
	expect(error.code).toBe("unavailable");
	expect(error.status).toBe(500);
});

test("a 502/503 with no body still surfaces unavailable, not invalid_request", async () => {
	expect((await capturedError(textStatusClient(503, ""))).code).toBe("unavailable");
	expect((await capturedError(textStatusClient(502, "bad gateway"))).code).toBe("unavailable");
});

test("a 400 with a non-JSON body keeps the 4xx invalid_request default", async () => {
	const error = await capturedError(textStatusClient(400, "bad request"));
	expect(error.code).toBe("invalid_request");
	expect(error.status).toBe(400);
});

test("a typed 5xx body keeps its explicit error code", async () => {
	const error = await capturedError(
		new WorkClient(
			"http://127.0.0.1:54322",
			WORKSPACE_ID,
			() => "token",
			async () =>
				Response.json(
					{ error: { code: "forbidden", request_id: null, correlation_id: null, diagnostics: ["nope"] } },
					{ status: 500 },
				),
		),
	);
	expect(error.code).toBe("forbidden");
	expect(error.status).toBe(500);
});
