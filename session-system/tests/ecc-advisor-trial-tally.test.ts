import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import {
	RULE,
	tally,
	validateCase,
	type CaseRow,
	type TallyCase,
	type ValidateCaseInput,
} from "../../scripts/ecc-advisor-trial/tally.ts";

const REPO_ROOT = path.resolve(import.meta.dir, "../..");
const TALLY_SCRIPT = path.join(REPO_ROOT, "scripts/ecc-advisor-trial/tally.ts");
const ADVISOR = "ECC Database Reviewer";
const COMMIT_A = "0123456789abcdef0123456789abcdef01234567";
const COMMIT_B = "fedcba9876543210fedcba9876543210fedcba98";
const COMMIT_C = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

interface NoteSpec {
	n: number;
	text?: string;
}

interface LabelSpec {
	n: number;
	label: string;
	rationale: string;
	matches_known_defect: boolean;
}

interface Spec {
	id: string;
	commit: string;
	kind: "known_bad" | "ordinary";
	scope: string;
	known_defect: string | null;
	notesCase?: string;
	notesCommit?: string;
	labelsCase?: string;
	labelsCommit?: string;
	advisor?: string;
	omp_exit?: number;
	advisor_completed?: boolean;
	notes: NoteSpec[];
	labels: LabelSpec[] | null;
	omitFromSums?: string;
	badHashFor?: string;
	skipRun?: boolean;
}

interface Built {
	spec: Spec;
	row: CaseRow;
	input: ValidateCaseInput;
	prompt: string;
	transcript: string;
	notesText: string;
	sumsText: string;
	labelsText: string | null;
}

const roots: string[] = [];

afterEach(async () => {
	await Promise.all(roots.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

function sha256(text: string): string {
	return new Bun.CryptoHasher("sha256").update(text).digest("hex");
}

function base(over: Partial<Spec> & Pick<Spec, "id" | "commit" | "kind">): Spec {
	return {
		scope: "write path",
		known_defect: over.kind === "ordinary" ? null : "known defect",
		notes: [{ n: 1, text: "finding" }],
		labels: [
			{
				n: 1,
				label: "useful",
				rationale: "src/db.ts:42 empty row",
				matches_known_defect: over.kind === "known_bad",
			},
		],
		...over,
	};
}

function build(spec: Spec): Built {
	const row: CaseRow = {
		id: spec.id,
		commit: spec.commit,
		kind: spec.kind,
		scope: spec.scope,
		known_defect: spec.known_defect,
	};
	const prompt = `prompt for ${spec.id}\n`;
	const transcript = `${JSON.stringify({ advisor: ADVISOR, event: "advise" })}\n`;
	const notesText = `${JSON.stringify({
		case: spec.notesCase ?? spec.id,
		commit: spec.notesCommit ?? spec.commit,
		advisor: spec.advisor ?? ADVISOR,
		omp_exit: spec.omp_exit ?? 0,
		advisor_completed: spec.advisor_completed ?? true,
		notes: spec.notes,
	})}\n`;
	const files: Record<string, string> = {
		"prompt.md": prompt,
		"advisor-transcript.jsonl": transcript,
		"notes.json": notesText,
	};
	const lines: string[] = [];
	for (const name of ["advisor-transcript.jsonl", "notes.json", "prompt.md"]) {
		if (spec.omitFromSums === name) continue;
		const hash = spec.badHashFor === name ? "0".repeat(64) : sha256(files[name]);
		lines.push(`${hash}  ${name}`);
	}
	const sumsText = `${lines.join("\n")}\n`;
	const labelsText =
		spec.labels === null
			? null
			: `${JSON.stringify({
					case: spec.labelsCase ?? spec.id,
					commit: spec.labelsCommit ?? spec.commit,
					labels: spec.labels,
				})}\n`;
	return {
		spec,
		row,
		input: { caseRow: row, files, sha256sums: sumsText, labelsText },
		prompt,
		transcript,
		notesText,
		sumsText,
		labelsText,
	};
}

function toTally(spec: Spec): TallyCase {
	return {
		id: spec.id,
		commit: spec.commit,
		kind: spec.kind,
		notes: spec.notes,
		labels: (spec.labels ?? []).map(label => ({
			n: label.n,
			label: label.label,
			matches_known_defect: label.matches_known_defect,
		})),
	};
}

async function makeRoot(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-advisor-trial-"));
	roots.push(dir);
	return dir;
}

async function writeTrial(root: string, built: Built[]): Promise<void> {
	await fs.writeFile(
		path.join(root, "cases.json"),
		`${JSON.stringify(
			built.map(item => item.row),
			null,
			"\t",
		)}\n`,
	);
	for (const item of built) {
		if (item.spec.skipRun) continue;
		const dir = path.join(root, "runs", item.row.id);
		await fs.mkdir(dir, { recursive: true });
		await fs.writeFile(path.join(dir, "prompt.md"), item.prompt);
		await fs.writeFile(path.join(dir, "advisor-transcript.jsonl"), item.transcript);
		await fs.writeFile(path.join(dir, "notes.json"), item.notesText);
		await fs.writeFile(path.join(dir, "SHA256SUMS"), item.sumsText);
		if (item.labelsText !== null) {
			const labelsDir = path.join(root, "labels");
			await fs.mkdir(labelsDir, { recursive: true });
			await fs.writeFile(path.join(labelsDir, `${item.row.id}.json`), item.labelsText);
		}
	}
}

async function runCli(args: string[]): Promise<{ exitCode: number; stdout: string; stderr: string }> {
	const proc = Bun.spawn(["bun", TALLY_SCRIPT, ...args], {
		cwd: REPO_ROOT,
		stdout: "pipe",
		stderr: "pipe",
		stdin: "ignore",
	});
	const [stdout, stderr, exitCode] = await Promise.all([
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
		proc.exited,
	]);
	return { exitCode, stdout, stderr };
}

function stderrLines(text: string): string[] {
	return text.split("\n").filter(line => line.length > 0);
}

async function expectReported(spec: Spec, fragments: string[]): Promise<void> {
	const built = build(spec);
	const errors = validateCase(built.input);
	expect(errors).toHaveLength(fragments.length);
	for (const fragment of fragments) expect(errors.some(error => error.includes(fragment))).toBe(true);
	const root = await makeRoot();
	await writeTrial(root, [built]);
	const cli = await runCli(["--check", "--root", root]);
	expect(cli.exitCode).toBe(1);
	expect(stderrLines(cli.stderr)).toEqual(errors.map(error => `${spec.id}: ${error}`));
	expect(
		await fs.access(path.join(root, "summary.json")).then(
			() => true,
			() => false,
		),
	).toBe(false);
}

describe("validateCase", () => {
	test("reports case and commit differences", async () => {
		await expectReported(
			base({
				id: "mismatch",
				commit: COMMIT_A,
				kind: "known_bad",
				notesCase: "other",
				labelsCommit: COMMIT_B,
			}),
			["case differs", "commit differs"],
		);
	});

	test("reports a commit that is not 40 hex", async () => {
		await expectReported(base({ id: "short", commit: "abc", kind: "known_bad" }), ["commit is not 40 hex"]);
	});

	test("reports a missing SHA256SUMS entry and a hash that differs", async () => {
		await expectReported(
			base({ id: "sums", commit: COMMIT_A, kind: "known_bad", omitFromSums: "prompt.md", badHashFor: "notes.json" }),
			["SHA256SUMS lacks prompt.md", "SHA256SUMS hash differs for notes.json"],
		);
	});

	test("reports an advisor that is not ECC Database Reviewer", async () => {
		await expectReported(base({ id: "advisor", commit: COMMIT_A, kind: "known_bad", advisor: "other" }), [
			`advisor is not "${ADVISOR}"`,
		]);
	});

	test("reports omp_exit that is not 0", async () => {
		await expectReported(base({ id: "exit", commit: COMMIT_A, kind: "known_bad", omp_exit: 1 }), [
			"omp_exit is not 0",
		]);
	});

	test("reports advisor_completed that is not true", async () => {
		await expectReported(base({ id: "done", commit: COMMIT_A, kind: "known_bad", advisor_completed: false }), [
			"advisor_completed is not true",
		]);
	});

	test("reports a label n set that differs from the note n set", async () => {
		await expectReported(
			base({
				id: "nset",
				commit: COMMIT_A,
				kind: "known_bad",
				notes: [{ n: 1 }, { n: 2 }],
				labels: [
					{ n: 1, label: "false_alarm", rationale: "src/db.ts:11 existing check", matches_known_defect: false },
				],
			}),
			["label n set differs from note n set"],
		);
	});

	test("reports an unknown label", async () => {
		await expectReported(
			base({
				id: "label",
				commit: COMMIT_A,
				kind: "known_bad",
				labels: [{ n: 1, label: "bogus", rationale: "src/db.ts:42 empty row", matches_known_defect: false }],
			}),
			["unknown label 'bogus'"],
		);
	});

	test("reports a rationale with no path:line", async () => {
		await expectReported(
			base({
				id: "rationale",
				commit: COMMIT_A,
				kind: "known_bad",
				labels: [{ n: 1, label: "useful", rationale: "The query looks wrong.", matches_known_defect: true }],
			}),
			["rationale has no path:line"],
		);
	});

	test("reports matches_known_defect on an ordinary case", async () => {
		await expectReported(
			base({
				id: "ordinary",
				commit: COMMIT_A,
				kind: "ordinary",
				labels: [{ n: 1, label: "useful", rationale: "src/db.ts:42 empty row", matches_known_defect: true }],
			}),
			["matches_known_defect on an ordinary case"],
		);
	});
});

describe("cli", () => {
	test("empty root passes --check", async () => {
		const root = await makeRoot();
		const cli = await runCli(["--check", "--root", root]);
		expect(cli.exitCode).toBe(0);
		expect(cli.stderr).toBe("");
		expect(
			await fs.access(path.join(root, "summary.json")).then(
				() => true,
				() => false,
			),
		).toBe(false);
	});

	test("check allows a run with no labels and write refuses it", async () => {
		const spec = base({ id: "nolabels", commit: COMMIT_A, kind: "known_bad", labels: null });
		const built = build(spec);
		expect(validateCase(built.input)).toEqual([]);
		const root = await makeRoot();
		await writeTrial(root, [built]);
		const check = await runCli(["--check", "--root", root]);
		expect(check.exitCode).toBe(0);
		const wrote = await runCli(["--write", "--root", root]);
		expect(wrote.exitCode).toBe(1);
		expect(wrote.stderr).toContain("labels/nolabels.json is missing");
		expect(
			await fs.access(path.join(root, "summary.json")).then(
				() => true,
				() => false,
			),
		).toBe(false);
	});

	test("check fails when a run directory is missing", async () => {
		const spec = base({ id: "absent", commit: COMMIT_A, kind: "known_bad", skipRun: true });
		const root = await makeRoot();
		await writeTrial(root, [build(spec)]);
		const cli = await runCli(["--check", "--root", root]);
		expect(cli.exitCode).toBe(1);
		expect(cli.stderr).toContain("runs/absent is missing");
	});
});

describe("tally", () => {
	test("keeps a caught known_bad, a missed known_bad, and a zero-note ordinary case", async () => {
		const specs: Spec[] = [
			base({
				id: "caught",
				commit: COMMIT_A,
				kind: "known_bad",
				scope: "write path",
				known_defect: "null deref on an empty row",
				notes: [{ n: 1, text: "empty row" }],
				labels: [{ n: 1, label: "useful", rationale: "src/db.ts:42 empty row", matches_known_defect: true }],
			}),
			base({
				id: "missed",
				commit: COMMIT_B,
				kind: "known_bad",
				scope: "write path",
				known_defect: "stale cache read",
				notes: [{ n: 1, text: "unrelated" }],
				labels: [
					{ n: 1, label: "false_alarm", rationale: "src/db.ts:11 existing check", matches_known_defect: false },
				],
			}),
			base({
				id: "quiet",
				commit: COMMIT_C,
				kind: "ordinary",
				scope: "readme",
				known_defect: null,
				notes: [],
				labels: [],
			}),
		];
		const summary = await writeSummary(specs);
		expect(summary).toEqual({
			cases: 3,
			known_bad_cases: 2,
			known_defects_caught: 1,
			useful: 1,
			false_alarm: 1,
			per_case: [
				{
					id: "caught",
					commit: COMMIT_A,
					kind: "known_bad",
					notes: 1,
					useful: 1,
					false_alarm: 0,
					known_defect_caught: true,
				},
				{
					id: "missed",
					commit: COMMIT_B,
					kind: "known_bad",
					notes: 1,
					useful: 0,
					false_alarm: 1,
					known_defect_caught: false,
				},
				{
					id: "quiet",
					commit: COMMIT_C,
					kind: "ordinary",
					notes: 0,
					useful: 0,
					false_alarm: 0,
					known_defect_caught: null,
				},
			],
			rule: RULE,
			recommendation: "keep",
		});
		expect(RULE).toBe("keep iff useful >= false_alarm and known_defects_caught >= 1, else drop");
	});

	test("drops when no known defect is caught", async () => {
		const summary = await writeSummary([
			base({
				id: "near",
				commit: COMMIT_A,
				kind: "known_bad",
				notes: [{ n: 1 }],
				labels: [{ n: 1, label: "useful", rationale: "src/db.ts:9 other bug", matches_known_defect: false }],
			}),
		]);
		expect(summary.known_defects_caught).toBe(0);
		expect(summary.useful).toBe(1);
		expect(summary.false_alarm).toBe(0);
		expect(summary.per_case[0].known_defect_caught).toBe(false);
		expect(summary.recommendation).toBe("drop");
	});

	test("drops when false alarms outnumber useful findings", async () => {
		const summary = await writeSummary([
			base({
				id: "noisy",
				commit: COMMIT_A,
				kind: "known_bad",
				notes: [{ n: 1 }, { n: 2 }, { n: 3 }],
				labels: [
					{ n: 1, label: "useful", rationale: "src/db.ts:42 empty row", matches_known_defect: true },
					{ n: 2, label: "false_alarm", rationale: "src/db.ts:11 existing check", matches_known_defect: false },
					{ n: 3, label: "false_alarm", rationale: "src/db.ts:12 existing check", matches_known_defect: false },
				],
			}),
		]);
		expect(summary.known_defects_caught).toBe(1);
		expect(summary.useful).toBe(1);
		expect(summary.false_alarm).toBe(2);
		expect(summary.recommendation).toBe("drop");
	});

	test("counts a known defect once and ignores a false alarm that carries the flag", () => {
		const summary = tally([
			{
				id: "double",
				commit: COMMIT_A,
				kind: "known_bad",
				notes: [{ n: 1 }, { n: 2 }],
				labels: [
					{ n: 1, label: "useful", matches_known_defect: true },
					{ n: 2, label: "useful", matches_known_defect: true },
				],
			},
			{
				id: "flagged-alarm",
				commit: COMMIT_B,
				kind: "known_bad",
				notes: [{ n: 1 }],
				labels: [{ n: 1, label: "false_alarm", matches_known_defect: true }],
			},
		]);
		expect(summary.known_defects_caught).toBe(1);
		expect(summary.useful).toBe(2);
		expect(summary.false_alarm).toBe(1);
		expect(summary.per_case[0].known_defect_caught).toBe(true);
		expect(summary.per_case[1].known_defect_caught).toBe(false);
		expect(summary.recommendation).toBe("keep");
	});
});

async function writeSummary(specs: Spec[]) {
	const built = specs.map(build);
	for (const item of built) expect(validateCase(item.input)).toEqual([]);
	const direct = tally(specs.map(toTally));
	const root = await makeRoot();
	await writeTrial(root, built);
	const check = await runCli(["--check", "--root", root]);
	expect(check.exitCode).toBe(0);
	expect(check.stderr).toBe("");
	const wrote = await runCli(["--write", "--root", root]);
	expect(wrote.exitCode).toBe(0);
	expect(wrote.stderr).toBe("");
	const summary = JSON.parse(await fs.readFile(path.join(root, "summary.json"), "utf8"));
	expect(summary).toEqual(direct);
	return summary;
}
