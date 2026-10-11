import { expect, test } from "bun:test";
import { intakeCommandSha256, intakeResultSha256 } from "../src/intake-records-hash";

const repo = `${import.meta.dir}/../../..`;
const pythonProject = `${repo}/python/omp-work`;

const fixtureText = await Bun.file(`${import.meta.dir}/fixtures/intake-records.json`).text();
type CommandEntry = {
	envelope: Record<string, unknown>;
	result: Record<string, unknown>;
};
const fixture = JSON.parse(fixtureText) as { commands: Record<string, CommandEntry> };

// Boundaries: 1e-4 and digit-only 1e16 stay as JSON.stringify emits them;
// 1e-5 and 1e21 cross into padded exponential form. Negatives keep their sign.
const SAMPLES = [1e-7, 1e-6, 1e-5, 1e-4, 1e16, 1e21];
const NUMBERS = SAMPLES.flatMap(value => [value, -value]);

const SCRIPT = `
import json, sys
from pydantic import ValidationError
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.canonical import command_sha256, sha256

for item in json.load(sys.stdin):
    if "omittedEquivalent" in item:
        # NSI wire schemas reject explicit null even though model defaults dump null.
        # Prove that boundary, then hash the validated omitted-default equivalent.
        try:
            CommandEnvelope.model_validate(item["envelope"])
        except ValidationError as error:
            assert "NSI validation at" in str(error), str(error)
            assert ("None is not of type" in str(error)
                    or "None is not valid under any of the given schemas" in str(error)), str(error)
        else:
            raise AssertionError("explicit null unexpectedly passed NSI wire validation")
        envelope = CommandEnvelope.model_validate(item["omittedEquivalent"])
        raw_payload = item["envelope"]["command"]["payload"]
        dumped_payload = envelope.command.model_dump(mode="json")["payload"]
        if raw_payload["claim"].get("admitted_by", "omitted") is None:
            assert dumped_payload["claim"]["admitted_by"] is None
        for raw_span, dumped_span in zip(raw_payload["claim"]["evidence"], dumped_payload["claim"]["evidence"], strict=True):
            if raw_span.get("quote", "omitted") is None:
                assert dumped_span["quote"] is None
        for raw_link, dumped_link in zip(raw_payload["evidence_links"], dumped_payload["evidence_links"], strict=True):
            if raw_link["span"].get("quote", "omitted") is None:
                assert dumped_link["span"]["quote"] is None
    else:
        envelope = CommandEnvelope.model_validate(item["envelope"])
    result = item["result"]
    print(command_sha256(envelope))
    print(sha256(result))
`;

type OracleRow = { command: string; result: string };

async function oracle(
	cases: { envelope: unknown; result: unknown; omittedEquivalent?: unknown }[],
): Promise<OracleRow[]> {
	const child = Bun.spawn(["uv", "run", "--project", pythonProject, "python", "-c", SCRIPT], {
		stdin: new Blob([JSON.stringify(cases)]),
		stdout: "pipe",
		stderr: "pipe",
	});
	const stdout = await new Response(child.stdout).text();
	const stderr = await new Response(child.stderr).text();
	const exitCode = await child.exited;
	if (exitCode !== 0) throw new Error(stderr);
	const lines = stdout.split("\n").filter(line => line.length > 0);
	if (lines.length !== cases.length * 2) {
		throw new Error(`expected ${cases.length * 2} hash lines, got ${lines.length}\n${stderr}`);
	}
	const rows: OracleRow[] = [];
	for (let i = 0; i < lines.length; i += 2) {
		rows.push({ command: lines[i] ?? "", result: lines[i + 1] ?? "" });
	}
	return rows;
}

function reorderKeys(value: Record<string, unknown>): Record<string, unknown> {
	const reversed: Record<string, unknown> = {};
	for (const key of Object.keys(value).reverse()) {
		const child = value[key];
		reversed[key] =
			child !== null && typeof child === "object" && !Array.isArray(child)
				? reorderKeys(child as Record<string, unknown>)
				: child;
	}
	return reversed;
}

test("intake result and command hashes match Python sha256 and command_sha256", async () => {
	const fingerprint = JSON.stringify(fixture);
	const cases: {
		command: string;
		kind: string;
		envelope: Record<string, unknown>;
		result: unknown;
		omittedEquivalent?: Record<string, unknown>;
	}[] = [];

	// 1. Fixture entries, reordered results, and numeric result mutations
	for (const [command, entry] of Object.entries(fixture.commands)) {
		cases.push({ command, kind: "fixture", envelope: entry.envelope, result: entry.result });
		cases.push({
			command,
			kind: "reordered",
			envelope: entry.envelope,
			result: reorderKeys(structuredClone(entry.result)),
		});
		for (const number of NUMBERS) {
			const copy = structuredClone(entry.result);
			copy.revision = number;
			cases.push({ command, kind: `numeric:${number}`, envelope: entry.envelope, result: copy });
		}
		// Mutate each excluded id to a valid UUID
		for (const field of ["operation_id", "request_id", "correlation_id"] as const) {
			const copyEnv = structuredClone(entry.envelope);
			copyEnv[field] = crypto.randomUUID();
			cases.push({ command, kind: `mutated-id:${field}`, envelope: copyEnv, result: entry.result });
		}
	}

	// 2. Numeric mutations on copied claim.value.value
	for (const number of NUMBERS) {
		const copyEnv = structuredClone(fixture.commands.record_intake_claim.envelope);
		const pl = (copyEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
		(pl.claim as Record<string, unknown>).value = { type: "number", value: number };
		cases.push({
			command: "record_intake_claim",
			kind: `numeric-claim:${number}`,
			envelope: copyEnv,
			result: fixture.commands.record_intake_claim.result,
		});
	}

	// 3. Numeric mutations on numeric decision.answer.value
	for (const number of NUMBERS) {
		const copyEnv = structuredClone(fixture.commands.record_intake_decision.envelope);
		const pl = (copyEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
		(pl.decision as Record<string, unknown>).answer = { type: "number", value: number };
		cases.push({
			command: "record_intake_decision",
			kind: `numeric-decision:${number}`,
			envelope: copyEnv,
			result: fixture.commands.record_intake_decision.result,
		});
	}

	// 4. Decision defaults: omitted vs explicit null (fixture) and non-null
	const omittedDecEnv = structuredClone(fixture.commands.record_intake_decision.envelope);
	const decPl = (omittedDecEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
	delete decPl.superseded_decision_id;
	delete decPl.verified_relay_operation_id;
	cases.push({
		command: "record_intake_decision",
		kind: "default-decision:omitted",
		envelope: omittedDecEnv,
		result: fixture.commands.record_intake_decision.result,
	});

	const nonNullDecEnv = structuredClone(fixture.commands.record_intake_decision.envelope);
	const nonNullDecPl = (nonNullDecEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
	nonNullDecPl.superseded_decision_id = "decision-old";
	nonNullDecPl.verified_relay_operation_id = "22222222-2222-4222-8222-222222222222";
	cases.push({
		command: "record_intake_decision",
		kind: "default-decision:non-null",
		envelope: nonNullDecEnv,
		result: fixture.commands.record_intake_decision.result,
	});

	// 5. Source spans default: omitted vs explicit [] (fixture) and non-empty
	const omittedSpansEnv = structuredClone(fixture.commands.register_intake_evidence.envelope);
	const omittedSrc = ((omittedSpansEnv.command as Record<string, unknown>).payload as Record<string, unknown>)
		.source as Record<string, unknown>;
	delete omittedSrc.spans;
	cases.push({
		command: "register_intake_evidence",
		kind: "default-source-spans:omitted",
		envelope: omittedSpansEnv,
		result: fixture.commands.register_intake_evidence.result,
	});

	const sampleText = "Owner";
	const sampleSha = new Bun.CryptoHasher("sha256").update(sampleText, "utf8").digest("hex");
	const nonEmptySpansEnv = structuredClone(fixture.commands.register_intake_evidence.envelope);
	const nonEmptySrc = ((nonEmptySpansEnv.command as Record<string, unknown>).payload as Record<string, unknown>)
		.source as Record<string, unknown>;
	nonEmptySrc.text = sampleText;
	nonEmptySrc.sha256 = sampleSha;
	nonEmptySrc.spans = [
		{
			id: "span-1",
			start: 0,
			end: 5,
			exact_text_sha256: sampleSha,
		},
	];
	cases.push({
		command: "register_intake_evidence",
		kind: "default-source-spans:non-empty",
		envelope: nonEmptySpansEnv,
		result: fixture.commands.register_intake_evidence.result,
	});

	// 6. Quote default: omitted vs non-null (fixture)
	const omittedQuoteEnv = structuredClone(fixture.commands.record_intake_claim.envelope);
	const quoteClaimPl = (omittedQuoteEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
	const quoteClaim = quoteClaimPl.claim as Record<string, unknown>;
	const quoteEv = quoteClaim.evidence as Record<string, unknown>[];
	delete quoteEv[0]?.quote;
	const quoteLinks = quoteClaimPl.evidence_links as { span: Record<string, unknown> }[];
	delete quoteLinks[0]?.span.quote;
	cases.push({
		command: "record_intake_claim",
		kind: "default-quote:omitted",
		envelope: omittedQuoteEnv,
		result: fixture.commands.record_intake_claim.result,
	});

	// 7. Admitted_by default: omitted vs non-null (fixture)
	const omittedAdmittedEnv = structuredClone(fixture.commands.record_intake_claim.envelope);
	const admittedClaimPl = (omittedAdmittedEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
	const admittedClaim = admittedClaimPl.claim as Record<string, unknown>;
	admittedClaim.state = "proposed";
	admittedClaim.origin = "model_proposal";
	admittedClaim.asserted_by = { id: "model-1", kind: "model" };
	delete admittedClaim.admitted_by;
	cases.push({
		command: "record_intake_claim",
		kind: "default-admitted-by:omitted",
		envelope: omittedAdmittedEnv,
		result: fixture.commands.record_intake_claim.result,
	});

	// 8. received_at variants (.1, .1234567, .000000, Z, +00:00, +05:30)
	const RECEIVED_AT_SAMPLES = [
		"2026-10-11T00:00:00.1Z",
		"2026-10-11T00:00:00.1234567Z",
		"2026-10-11T00:00:00.000000Z",
		"2026-10-11T00:00:00Z",
		"2026-10-11T00:00:00+00:00",
		"2026-10-11T00:00:00+05:30",
	];
	for (const sample of RECEIVED_AT_SAMPLES) {
		const copyEnv = structuredClone(fixture.commands.record_intake_decision.envelope);
		const pl = (copyEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
		const inst = pl.instruction as Record<string, unknown>;
		const prov = inst.provenance as Record<string, unknown>;
		prov.received_at = sample;
		cases.push({
			command: "record_intake_decision",
			kind: `received-at:${sample}`,
			envelope: copyEnv,
			result: fixture.commands.record_intake_decision.result,
		});
	}

	// 9. changed created_at affects hash
	const changedCreatedEnv = structuredClone(fixture.commands.record_intake_decision.envelope);
	const createdPl = (changedCreatedEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
	(createdPl.decision as Record<string, unknown>).created_at = "2026-10-10T00:00:01Z";
	cases.push({
		command: "record_intake_decision",
		kind: "changed-created-at",
		envelope: changedCreatedEnv,
		result: fixture.commands.record_intake_decision.result,
	});

	// Explicit null quote equals omitted default quote
	const explicitNullQuoteEnv = structuredClone(omittedQuoteEnv);
	const expQuotePl = (explicitNullQuoteEnv.command as Record<string, unknown>).payload as Record<string, unknown>;
	const expQuoteClaim = expQuotePl.claim as Record<string, unknown>;
	(expQuoteClaim.evidence as Record<string, unknown>[])[0]!.quote = null;
	(expQuotePl.evidence_links as { span: Record<string, unknown> }[])[0]!.span.quote = null;
	cases.push({
		command: "record_intake_claim",
		kind: "default-quote:explicit-null",
		envelope: explicitNullQuoteEnv,
		omittedEquivalent: omittedQuoteEnv,
		result: fixture.commands.record_intake_claim.result,
	});

	// Explicit null admitted_by equals omitted default admitted_by
	const explicitNullAdmittedEnv = structuredClone(omittedAdmittedEnv);
	const expAdmittedPl = (explicitNullAdmittedEnv.command as Record<string, unknown>).payload as Record<
		string,
		unknown
	>;
	(expAdmittedPl.claim as Record<string, unknown>).admitted_by = null;
	cases.push({
		command: "record_intake_claim",
		kind: "default-admitted-by:explicit-null",
		envelope: explicitNullAdmittedEnv,
		omittedEquivalent: omittedAdmittedEnv,
		result: fixture.commands.record_intake_claim.result,
	});

	const rows = await oracle(
		cases.map(({ envelope, result, omittedEquivalent }) => ({ envelope, result, omittedEquivalent })),
	);
	expect(rows).toHaveLength(cases.length);

	const fixtureCommandHash = new Map<string, string>();
	const fixtureResultHash = new Map<string, string>();

	for (let i = 0; i < cases.length; i++) {
		const item = cases[i];
		const row = rows[i];
		if (item === undefined || row === undefined) throw new Error(`missing oracle row ${i}`);
		expect(row.command).toMatch(/^[0-9a-f]{64}$/);
		expect(row.result).toMatch(/^[0-9a-f]{64}$/);

		// Assert no mutation on input envelope
		const envelopeBefore = JSON.stringify(item.envelope);
		const actualCommandHash = intakeCommandSha256(item.envelope);
		expect(JSON.stringify(item.envelope)).toBe(envelopeBefore);

		// Compare BOTH hashes for every fixture command and mutation
		expect(actualCommandHash).toBe(row.command);
		expect(intakeResultSha256(item.result)).toBe(row.result);

		if (item.kind === "fixture") {
			fixtureCommandHash.set(item.command, row.command);
			fixtureResultHash.set(item.command, row.result);
		}
		if (item.kind === "reordered") {
			const expectedResult = fixtureResultHash.get(item.command);
			if (expectedResult === undefined) throw new Error(`missing fixture hash for ${item.command}`);
			expect(row.result).toBe(expectedResult);
			expect(actualCommandHash).toBe(fixtureCommandHash.get(item.command) ?? "");
		}
		if (item.kind.startsWith("mutated-id:")) {
			// Mutated excluded id leaves command hash unchanged
			const baseCommandHash = fixtureCommandHash.get(item.command) ?? "";
			expect(actualCommandHash).toBe(baseCommandHash);
			expect(row.command).toBe(baseCommandHash);
		}
	}

	// Decision omitted defaults equal explicit null in fixture
	expect(intakeCommandSha256(omittedDecEnv)).toBe(fixtureCommandHash.get("record_intake_decision") ?? "");

	// Source spans omitted default equals explicit [] in fixture
	expect(intakeCommandSha256(omittedSpansEnv)).toBe(fixtureCommandHash.get("register_intake_evidence") ?? "");

	expect(intakeCommandSha256(explicitNullQuoteEnv)).toBe(intakeCommandSha256(omittedQuoteEnv));
	expect(intakeCommandSha256(omittedQuoteEnv)).not.toBe(fixtureCommandHash.get("record_intake_claim") ?? "");
	expect(intakeCommandSha256(explicitNullAdmittedEnv)).toBe(intakeCommandSha256(omittedAdmittedEnv));
	expect(intakeCommandSha256(omittedAdmittedEnv)).not.toBe(fixtureCommandHash.get("record_intake_claim") ?? "");

	// Changed created_at affects hash
	expect(intakeCommandSha256(changedCreatedEnv)).not.toBe(fixtureCommandHash.get("record_intake_decision") ?? "");

	// Fixture JSON remains unmodified
	expect(JSON.stringify(fixture)).toBe(fingerprint);
	expect(Object.keys(fixture.commands).length).toBeGreaterThan(0);
}, 60_000);
