import { expect, test } from "bun:test";
import { intakeResultSha256 } from "../src/intake-records-hash";

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
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.canonical import command_sha256, sha256

for item in json.load(sys.stdin):
    envelope = CommandEnvelope.model_validate(item["envelope"])
    result = item["result"]
    print(command_sha256(envelope))
    print(sha256(result))
`;

type OracleRow = { command: string; result: string };

async function oracle(cases: { envelope: unknown; result: unknown }[]): Promise<OracleRow[]> {
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

test("intake result hashes match Python sha256 of the raw result", async () => {
	const fingerprint = JSON.stringify(fixture);
	const cases: { command: string; kind: string; envelope: Record<string, unknown>; result: unknown }[] = [];
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
	}

	const rows = await oracle(cases.map(({ envelope, result }) => ({ envelope, result })));
	expect(rows).toHaveLength(cases.length);
	const commandHash = new Map<string, string>();
	const fixtureHash = new Map<string, string>();
	for (let i = 0; i < cases.length; i++) {
		const item = cases[i];
		const row = rows[i];
		if (item === undefined || row === undefined) throw new Error(`missing oracle row ${i}`);
		expect(row.command).toMatch(/^[0-9a-f]{64}$/);
		expect(row.result).toMatch(/^[0-9a-f]{64}$/);
		const seen = commandHash.get(item.command);
		if (seen === undefined) commandHash.set(item.command, row.command);
		else expect(row.command).toBe(seen);
		expect(intakeResultSha256(item.result)).toBe(row.result);
		if (item.kind === "fixture") fixtureHash.set(item.command, row.result);
		if (item.kind === "reordered") {
			const expected = fixtureHash.get(item.command);
			if (expected === undefined) throw new Error(`missing fixture hash for ${item.command}`);
			expect(row.result).toBe(expected);
		}
	}
	expect(JSON.stringify(fixture)).toBe(fingerprint);
	expect(Object.keys(fixture.commands).length).toBeGreaterThan(0);
}, 60_000);
