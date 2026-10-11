import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, it } from "bun:test";
import { buildOutputValidator } from "@oh-my-pi/pi-coding-agent/tools/output-schema-validator";
import { parseFrontmatter } from "@oh-my-pi/pi-utils";
import { createWorkBackend } from "../extensions/workflow/work";

describe("auditor typed output schema", async () => {
	const auditorPath = path.resolve(import.meta.dir, "../agents/auditor.md");
	const auditorText = await Bun.file(auditorPath).text();
	const { frontmatter } = parseFrontmatter(auditorText);

	const schemaPath = path.resolve(import.meta.dir, "../../python/omp-work/src/omp_work/contracts/v1/schema.json");
	const schemaContract = await Bun.file(schemaPath).json();
	const modelsAuditResult = schemaContract.models.AuditResult;

	it("builds a schema validator from auditor.md frontmatter", () => {
		const { validator, error } = buildOutputValidator(frontmatter.output);
		expect(error).toBeUndefined();
		expect(validator).toBeDefined();
		expect(validator?.requiredFields).toEqual(["audit_result"]);
	});

	it("accepts a contract-valid PASS { audit_result }", () => {
		const { validator } = buildOutputValidator(frontmatter.output);
		expect(validator).toBeDefined();

		const validPassPayload = {
			audit_result: {
				verdict: "PASS",
				evidence: {
					references: [
						{
							kind: "command",
							ref: "bun test session-system/tests/auditor-typed-output.test.ts",
							result: "pass",
						},
					],
				},
				criteria: [
					{
						criterion_id: "AC-1",
						status: "met",
						evidence: "verified in tests",
					},
				],
				findings: [],
				out_of_scope: [],
				remaining_questions: [],
			},
		};

		const result = validator?.validate(validPassPayload);
		expect(result?.success).toBe(true);
		expect(result?.issues).toEqual([]);
	});

	it("rejects an audit_result payload without verdict", () => {
		const { validator } = buildOutputValidator(frontmatter.output);
		expect(validator).toBeDefined();

		const withoutVerdictPayload = {
			audit_result: {
				evidence: {
					references: [
						{
							kind: "command",
							ref: "bun test",
						},
					],
				},
				criteria: [
					{
						criterion_id: "AC-1",
						status: "met",
						evidence: "verified",
					},
				],
			},
		};

		const result = validator?.validate(withoutVerdictPayload);
		expect(result?.success).toBe(false);
		expect(result?.issues?.some(issue => issue.keyword === "required" && issue.path.includes("verdict"))).toBe(true);
	});

	it("rejects legacy text report payload { report: 'VERDICT: PASS' }", () => {
		const { validator } = buildOutputValidator(frontmatter.output);
		expect(validator).toBeDefined();

		const reportPayload = {
			report: "VERDICT: PASS",
		};

		const result = validator?.validate(reportPayload);
		expect(result?.success).toBe(false);
	});

	it("ensures sorted required keys and verdict enum equal models.AuditResult (read-only)", () => {
		const outputSchema = frontmatter.output as Record<string, unknown>;
		const defs = outputSchema.$defs as Record<string, Record<string, unknown>>;
		const auditResultDef = defs.AuditResult;

		expect(auditResultDef).toBeDefined();

		const actualRequired = [...(auditResultDef.required as string[])].sort();
		const expectedRequired = [...(modelsAuditResult.required as string[])].sort();
		expect(actualRequired).toEqual(expectedRequired);

		const actualVerdictProperty = (auditResultDef.properties as Record<string, { enum: string[] }>).verdict;
		const expectedVerdictProperty = modelsAuditResult.properties.verdict;

		const actualVerdictEnum = [...actualVerdictProperty.enum].sort();
		const expectedVerdictEnum = [...expectedVerdictProperty.enum].sort();
		expect(actualVerdictEnum).toEqual(expectedVerdictEnum);
		expect(actualVerdictProperty.enum).toEqual(expectedVerdictProperty.enum);
	});
});

describe("auditor settlement close attempt outcome report", () => {
	const directories: string[] = [];
	afterEach(async () => {
		await Promise.all(directories.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
	});

	it("populates outcome.report when command result receipt contains report string", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "auditor-output-"));
		directories.push(tempDir);

		const launchId = "launch-1";
		const attemptId = "attempt-1";
		const expectedReport = "VERDICT: PASS\nAll criteria met";

		const resultReceipt = {
			payload: {
				report: expectedReport,
			},
		};

		const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
			const method = init?.method ?? (input instanceof Request ? input.method : "GET");

			if (method === "GET" && url.includes("/workflow")) {
				return Response.json({
					auditor_launches: [{ launch_id: launchId, attempt_id: attemptId }],
					close_attempts: [],
					receipts: [],
				});
			}

			if (method === "POST" && url.includes("/v1/commands")) {
				return Response.json({
					receipt: { state: "applied" },
					result: {
						status: "applied",
						verdict: "PASS",
						receipt: resultReceipt,
						event: {
							event_id: "ev-1",
							event_type: "auditor_launch_settled",
							reason_code: "settled",
							rendered_text: "settled",
							rendered_sha256: "a".repeat(64),
							requires_delivery: false,
							requires_fresh_authorization: false,
						},
					},
				});
			}

			return new Response("not found", { status: 404 });
		};

		const config = { baseUrl: "http://127.0.0.1:9999", workspaceId: "ws-1", ownerId: "owner-1" };
		const backend = createWorkBackend(config, () => "token", fetchImpl as never, tempDir);

		const outcome = await backend.settleAuditorLaunch("OMP-100", launchId, { payload: "audit raw text" });
		expect(outcome.status).toBe("applied");
		expect(outcome.report).toBe(resultReceipt.payload.report);
	});

	it("leaves outcome.report undefined when settlement is refused without receipt", async () => {
		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "auditor-output-"));
		directories.push(tempDir);

		const launchId = "launch-2";
		const attemptId = "attempt-2";

		const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
			const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
			const method = init?.method ?? (input instanceof Request ? input.method : "GET");

			if (method === "GET" && url.includes("/workflow")) {
				return Response.json({
					auditor_launches: [{ launch_id: launchId, attempt_id: attemptId }],
					close_attempts: [],
					receipts: [],
				});
			}

			if (method === "POST" && url.includes("/v1/commands")) {
				return Response.json({
					receipt: { state: "applied" },
					result: {
						status: "refused",
						event: {
							event_id: "ev-2",
							event_type: "auditor_settle_refused",
							reason_code: "audit_result_invalid",
							rendered_text: "Settlement refused: audit result invalid",
							rendered_sha256: "b".repeat(64),
							requires_delivery: false,
							requires_fresh_authorization: false,
						},
					},
				});
			}

			return new Response("not found", { status: 404 });
		};

		const config = { baseUrl: "http://127.0.0.1:9999", workspaceId: "ws-1", ownerId: "owner-1" };
		const backend = createWorkBackend(config, () => "token", fetchImpl as never, tempDir);

		const outcome = await backend.settleAuditorLaunch("OMP-100", launchId, { payload: "audit raw text" });
		expect(outcome.status).toBe("refused");
		expect(outcome.event).toBeDefined();
		expect(outcome.event.reasonCode).toBe("audit_result_invalid");
		expect(outcome.report).toBeUndefined();
	});
});

