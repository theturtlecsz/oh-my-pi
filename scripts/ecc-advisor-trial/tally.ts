#!/usr/bin/env bun
/**
 * Tally one ECC Database Reviewer trial.
 *
 *   bun scripts/ecc-advisor-trial/tally.ts --check [--root <dir>]
 *   bun scripts/ecc-advisor-trial/tally.ts --write [--root <dir>]
 *
 * The default root is docs/report/ecc-database-advisor-trial.
 * --check requires runs/<id>/ for every case and validates labels/<id>.json when
 * that file is present. --write requires a run and a labels file for every case,
 * and writes <root>/summary.json when every case is clean.
 */

import * as path from "node:path";
import { stat } from "node:fs/promises";

export const ADVISOR_NAME = "ECC Database Reviewer";
export const DEFAULT_ROOT = "docs/report/ecc-database-advisor-trial";
export const RULE = "keep iff useful >= false_alarm and known_defects_caught >= 1, else drop";

const REQUIRED_FILES = ["prompt.md", "advisor-transcript.jsonl", "notes.json"] as const;
const COMMIT_RE = /^[0-9a-fA-F]{40}$/;
const ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
/** A path:line citation, such as src/db.ts:42 or README:12. */
const PATH_LINE_RE = /[^\s:]+:\d+/;

export interface CaseRow {
	id: string;
	commit: string;
	kind: "known_bad" | "ordinary";
	scope: string;
	known_defect: string | null;
}

export interface ValidateCaseInput {
	caseRow: CaseRow;
	/** Run-file bytes or text, keyed by basename. A missing key means the file is absent. */
	files: Readonly<Record<string, Uint8Array | string>>;
	/** Raw SHA256SUMS. Null means that file is absent. */
	sha256sums: Uint8Array | string | null;
	/**
	 * Raw labels/<id>.json. Null means the labels file is absent.
	 * Absence is not an error here; --write reports it.
	 */
	labelsText: Uint8Array | string | null;
}

export interface TallyNote {
	n: number;
}

export interface TallyLabel {
	n: number;
	label: string;
	matches_known_defect?: boolean;
}

export interface TallyCase {
	id: string;
	commit: string;
	kind: "known_bad" | "ordinary";
	notes: readonly TallyNote[];
	labels: readonly TallyLabel[];
}

export interface PerCaseSummary {
	id: string;
	commit: string;
	kind: "known_bad" | "ordinary";
	notes: number;
	useful: number;
	false_alarm: number;
	known_defect_caught: boolean | null;
}

export interface TrialSummary {
	cases: number;
	known_bad_cases: number;
	known_defects_caught: number;
	useful: number;
	false_alarm: number;
	per_case: PerCaseSummary[];
	rule: string;
	recommendation: "keep" | "drop";
}

function asBytes(value: Uint8Array | string): Uint8Array {
	return typeof value === "string" ? new TextEncoder().encode(value) : value;
}

function asText(value: Uint8Array | string): string {
	return typeof value === "string" ? value : new TextDecoder().decode(value);
}

function sha256Hex(value: Uint8Array | string): string {
	return new Bun.CryptoHasher("sha256").update(asBytes(value)).digest("hex");
}

function asRecord(value: unknown): Record<string, unknown> | null {
	if (typeof value === "object" && value !== null && !Array.isArray(value)) return value as Record<string, unknown>;
	return null;
}

function parseJsonObject(text: string): Record<string, unknown> | null {
	try {
		return asRecord(JSON.parse(text) as unknown);
	} catch {
		return null;
	}
}

function show(value: unknown): string {
	if (typeof value === "string") return value;
	if (value === undefined) return "";
	return JSON.stringify(value) ?? "";
}

function parseSums(text: string): { hashes: Map<string, string>; errors: string[] } {
	const hashes = new Map<string, string>();
	const errors: string[] = [];
	for (const raw of text.split("\n")) {
		const line = raw.replace(/\r$/, "");
		if (line.trim() === "") continue;
		const match = /^([0-9a-fA-F]{64}) (?:\*| ?)(\S+)$/.exec(line);
		if (!match) {
			errors.push(`SHA256SUMS line is unreadable: ${line}`);
			continue;
		}
		const name = match[2];
		const hash = match[1].toLowerCase();
		if (hashes.has(name)) errors.push(`SHA256SUMS lists ${name} more than once`);
		else hashes.set(name, hash);
	}
	return { hashes, errors };
}

function integerList(items: readonly unknown[], kind: "note" | "label", errors: string[]): string[] | null {
	const keys: string[] = [];
	let ok = true;
	for (const item of items) {
		const record = asRecord(item);
		const n = record?.n;
		if (typeof n !== "number" || !Number.isInteger(n)) {
			errors.push(`${kind} n is not an integer`);
			ok = false;
			continue;
		}
		keys.push(String(n));
	}
	if (!ok) return null;
	return [...new Set(keys)].sort((a, b) => Number(a) - Number(b));
}

function recommendationFor(useful: number, falseAlarm: number, knownDefectsCaught: number): "keep" | "drop" {
	return useful >= falseAlarm && knownDefectsCaught >= 1 ? "keep" : "drop";
}

/** Errors for one case. An empty array means the files that are present agree. */
export function validateCase(input: ValidateCaseInput): string[] {
	const errors: string[] = [];
	const { caseRow } = input;
	if (caseRow.kind !== "known_bad" && caseRow.kind !== "ordinary") {
		errors.push("kind is not known_bad or ordinary");
	}

	let notes: Record<string, unknown> | null = null;
	const notesBytes = input.files["notes.json"];
	if (notesBytes === undefined) errors.push("notes.json is missing");
	else {
		notes = parseJsonObject(asText(notesBytes));
		if (!notes) errors.push("notes.json is not valid JSON");
	}

	let labels: Record<string, unknown> | null = null;
	if (input.labelsText !== null) {
		labels = parseJsonObject(asText(input.labelsText));
		if (!labels) errors.push("labels.json is not valid JSON");
	}

	if (notes && notes.case !== caseRow.id) {
		errors.push(`case differs: notes.json case '${show(notes.case)}' != cases.json id '${caseRow.id}'`);
	}
	if (labels && labels.case !== caseRow.id) {
		errors.push(`case differs: labels case '${show(labels.case)}' != cases.json id '${caseRow.id}'`);
	}
	if (notes && notes.commit !== caseRow.commit) {
		errors.push(`commit differs: notes.json '${show(notes.commit)}' != cases.json '${caseRow.commit}'`);
	}
	if (labels && labels.commit !== caseRow.commit) {
		errors.push(`commit differs: labels '${show(labels.commit)}' != cases.json '${caseRow.commit}'`);
	}

	const seenCommits = new Set<string>();
	for (const value of [caseRow.commit, notes?.commit, labels?.commit]) {
		if (value === undefined) continue;
		if (typeof value === "string" && COMMIT_RE.test(value)) continue;
		const shown = show(value);
		if (seenCommits.has(shown)) continue;
		seenCommits.add(shown);
		errors.push(`commit is not 40 hex: '${shown}'`);
	}

	if (input.sha256sums === null) {
		errors.push("SHA256SUMS is missing");
	} else {
		const parsed = parseSums(asText(input.sha256sums));
		errors.push(...parsed.errors);
		for (const name of REQUIRED_FILES) {
			const listed = parsed.hashes.get(name);
			const body = input.files[name];
			if (listed === undefined || body === undefined) {
				errors.push(`SHA256SUMS lacks ${name}`);
				continue;
			}
			if (listed !== sha256Hex(body)) errors.push(`SHA256SUMS hash differs for ${name}`);
		}
	}

	if (notes) {
		if (notes.advisor !== ADVISOR_NAME) errors.push(`advisor is not "${ADVISOR_NAME}"`);
		if (notes.omp_exit !== 0) errors.push("omp_exit is not 0");
		if (notes.advisor_completed !== true) errors.push("advisor_completed is not true");
	}

	const noteItems = notes && Array.isArray(notes.notes) ? notes.notes : null;
	const labelItems = labels && Array.isArray(labels.labels) ? labels.labels : null;
	if (notes && !noteItems) errors.push("notes is not an array");
	if (labels && !labelItems) errors.push("labels is not an array");

	if (noteItems && labelItems) {
		const noteKeys = integerList(noteItems, "note", errors);
		const labelKeys = integerList(labelItems, "label", errors);
		if (noteKeys && labelKeys && noteKeys.join(",") !== labelKeys.join(",")) {
			errors.push(
				`label n set differs from note n set: notes [${noteKeys.join(", ")}] labels [${labelKeys.join(", ")}]`,
			);
		}
		for (const item of labelItems) {
			const label = asRecord(item);
			if (!label) {
				errors.push("label is not an object");
				continue;
			}
			const n = show(label.n);
			if (label.label !== "useful" && label.label !== "false_alarm") {
				errors.push(`unknown label '${show(label.label)}' for n ${n}`);
			}
			if (typeof label.rationale !== "string" || !PATH_LINE_RE.test(label.rationale)) {
				errors.push(`rationale has no path:line for n ${n}`);
			}
			if (caseRow.kind === "ordinary" && label.matches_known_defect === true) {
				errors.push(`matches_known_defect on an ordinary case for n ${n}`);
			}
		}
	}

	return errors;
}

/** Score cases that have already passed validateCase. */
export function tally(cases: readonly TallyCase[]): TrialSummary {
	const perCase: PerCaseSummary[] = cases.map(entry => {
		let useful = 0;
		let falseAlarm = 0;
		for (const label of entry.labels) {
			if (label.label === "useful") useful++;
			else if (label.label === "false_alarm") falseAlarm++;
		}
		const knownDefectCaught =
			entry.kind === "known_bad"
				? entry.labels.some(label => label.label === "useful" && label.matches_known_defect === true)
				: null;
		return {
			id: entry.id,
			commit: entry.commit,
			kind: entry.kind,
			notes: entry.notes.length,
			useful,
			false_alarm: falseAlarm,
			known_defect_caught: knownDefectCaught,
		};
	});
	const useful = perCase.reduce((sum, entry) => sum + entry.useful, 0);
	const falseAlarm = perCase.reduce((sum, entry) => sum + entry.false_alarm, 0);
	const knownDefectsCaught = perCase.filter(entry => entry.known_defect_caught === true).length;
	return {
		cases: perCase.length,
		known_bad_cases: perCase.filter(entry => entry.kind === "known_bad").length,
		known_defects_caught: knownDefectsCaught,
		useful,
		false_alarm: falseAlarm,
		per_case: perCase,
		rule: RULE,
		recommendation: recommendationFor(useful, falseAlarm, knownDefectsCaught),
	};
}

interface LoadedTally {
	errors: string[];
	cases: TallyCase[];
}

function labelFrom(value: unknown): TallyLabel {
	const record = asRecord(value) ?? {};
	return {
		n: typeof record.n === "number" ? record.n : 0,
		label: typeof record.label === "string" ? record.label : "",
		matches_known_defect: record.matches_known_defect === true,
	};
}

function noteFrom(value: unknown): TallyNote {
	const record = asRecord(value) ?? {};
	return { n: typeof record.n === "number" ? record.n : 0 };
}

function tallyCaseFrom(row: CaseRow, notesText: string, labelsText: string): TallyCase {
	const notes = parseJsonObject(notesText);
	const labels = parseJsonObject(labelsText);
	const noteItems = notes && Array.isArray(notes.notes) ? notes.notes : [];
	const labelItems = labels && Array.isArray(labels.labels) ? labels.labels : [];
	return {
		id: row.id,
		commit: row.commit,
		kind: row.kind,
		notes: noteItems.map(noteFrom),
		labels: labelItems.map(labelFrom),
	};
}

async function directoryExists(target: string): Promise<boolean> {
	try {
		return (await stat(target)).isDirectory();
	} catch {
		return false;
	}
}

async function readBytes(target: string): Promise<Uint8Array | null> {
	try {
		if (!(await stat(target)).isFile()) return null;
	} catch {
		return null;
	}
	return await Bun.file(target).bytes();
}

function parseCaseRow(value: unknown, index: number): { row: CaseRow | null; error: string | null } {
	const record = asRecord(value);
	if (!record) return { row: null, error: `cases.json[${index}] is not an object` };
	if (typeof record.id !== "string" || !ID_RE.test(record.id)) {
		return { row: null, error: `cases.json[${index}] has an invalid id` };
	}
	if (record.kind !== "known_bad" && record.kind !== "ordinary") {
		return { row: null, error: `${record.id}: kind is not known_bad or ordinary` };
	}
	return {
		row: {
			id: record.id,
			commit: typeof record.commit === "string" ? record.commit : "",
			kind: record.kind,
			scope: typeof record.scope === "string" ? record.scope : "",
			known_defect: typeof record.known_defect === "string" ? record.known_defect : null,
		},
		error: null,
	};
}

async function readCases(root: string): Promise<{ rows: CaseRow[]; errors: string[] }> {
	const target = path.join(root, "cases.json");
	const bytes = await readBytes(target);
	if (!bytes) return { rows: [], errors: [] };
	let parsed: unknown;
	try {
		parsed = JSON.parse(new TextDecoder().decode(bytes)) as unknown;
	} catch {
		return { rows: [], errors: ["cases.json is not valid JSON"] };
	}
	if (!Array.isArray(parsed)) return { rows: [], errors: ["cases.json is not an array"] };
	const rows: CaseRow[] = [];
	const errors: string[] = [];
	const seen = new Set<string>();
	for (let index = 0; index < parsed.length; index++) {
		const result = parseCaseRow(parsed[index], index);
		if (!result.row || result.error) {
			errors.push(result.error ?? `cases.json[${index}] is not an object`);
			continue;
		}
		if (seen.has(result.row.id)) {
			errors.push(`duplicate case id '${result.row.id}'`);
			continue;
		}
		seen.add(result.row.id);
		rows.push(result.row);
	}
	return { rows, errors };
}

async function evaluate(root: string, mode: "check" | "write"): Promise<LoadedTally> {
	if (!(await directoryExists(root))) return { errors: [`root is missing: ${root}`], cases: [] };
	const { rows, errors } = await readCases(root);
	const cases: TallyCase[] = [];
	for (const row of rows) {
		const caseErrors: string[] = [];
		const runDir = path.join(root, "runs", row.id);
		if (!(await directoryExists(runDir))) {
			caseErrors.push(`runs/${row.id} is missing`);
		} else {
			const files: Record<string, Uint8Array> = {};
			for (const name of REQUIRED_FILES) {
				const bytes = await readBytes(path.join(runDir, name));
				if (bytes) files[name] = bytes;
			}
			const sums = await readBytes(path.join(runDir, "SHA256SUMS"));
			const labelsBytes = await readBytes(path.join(root, "labels", `${row.id}.json`));
			if (mode === "write" && !labelsBytes) caseErrors.push(`labels/${row.id}.json is missing`);
			caseErrors.push(
				...validateCase({
					caseRow: row,
					files,
					sha256sums: sums,
					labelsText: labelsBytes,
				}),
			);
			if (caseErrors.length === 0 && labelsBytes && files["notes.json"]) {
				cases.push(
					tallyCaseFrom(row, new TextDecoder().decode(files["notes.json"]), new TextDecoder().decode(labelsBytes)),
				);
			}
		}
		for (const error of caseErrors) errors.push(`${row.id}: ${error}`);
	}
	return { errors, cases };
}

function parseArgs(argv: readonly string[]): { mode: "check" | "write"; root: string } | { error: string } {
	let mode: "check" | "write" | null = null;
	let root = DEFAULT_ROOT;
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--check" || arg === "--write") {
			if (mode) return { error: "pass only one of --check or --write" };
			mode = arg === "--check" ? "check" : "write";
		} else if (arg === "--root") {
			const value = argv[++i];
			if (!value) return { error: "--root needs a directory" };
			root = value;
		} else if (arg.startsWith("--root=")) {
			root = arg.slice("--root=".length);
			if (!root) return { error: "--root needs a directory" };
		} else {
			return { error: `unknown argument '${arg}'` };
		}
	}
	if (!mode) return { error: "pass --check or --write" };
	return { mode, root };
}

function writeLine(stream: NodeJS.WriteStream, line: string): void {
	stream.write(`${line}\n`);
}

async function main(argv: readonly string[]): Promise<number> {
	const parsed = parseArgs(argv);
	if ("error" in parsed) {
		writeLine(process.stderr, parsed.error);
		writeLine(process.stderr, "usage: bun scripts/ecc-advisor-trial/tally.ts --check|--write [--root <dir>]");
		return 2;
	}
	const outcome = await evaluate(parsed.root, parsed.mode);
	for (const error of outcome.errors) writeLine(process.stderr, error);
	if (outcome.errors.length > 0) return 1;
	if (parsed.mode === "write") {
		const out = path.join(parsed.root, "summary.json");
		await Bun.write(out, `${JSON.stringify(tally(outcome.cases), null, "\t")}\n`);
		writeLine(process.stdout, `wrote ${out}`);
	}
	return 0;
}

if (import.meta.main) {
	process.exit(await main(process.argv.slice(2)));
}
