import * as path from "node:path";
import { describe, expect, it } from "bun:test";
import { buildOutputValidator } from "@oh-my-pi/pi-coding-agent/tools/output-schema-validator";
import { parseFrontmatter } from "@oh-my-pi/pi-utils";

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
