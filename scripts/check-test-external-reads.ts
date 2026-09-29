#!/usr/bin/env bun
// check-test-external-reads.ts — guard against test sources that read host files.
//
// A tracked test source must not read an absolute /home/ or /root/ path: such a
// test only passes on one machine's home layout and silently couples the suite
// to the developer's disk. This scans every tracked test source (git ls-files)
// for a read/existence call whose argument is a /home/ or /root/ string
// literal — directly, through path.join/resolve, or through a same-file constant
// bound to one — and fails on any hit.
//
//   bun scripts/check-test-external-reads.ts
//
// Exits 1 with `file:line: <call> <path>` for each hit, else 0 with PASS.

import { $ } from "bun";

export interface ExternalReadHit {
	/** 1-based line of the read call. */
	line: number;
	/** Callee as written, e.g. `fs.readFileSync`, `Bun.file`, `open`. */
	call: string;
	/** The offending /home/ or /root/ literal. */
	path: string;
}

type Dialect = "ts" | "py";

interface Token {
	kind: "ident" | "string" | "punct";
	value: string;
	line: number;
	hasSubstitution: boolean;
}

const HOME_RE = /^\/(?:home|root)\//;

/** Node/python read or existence calls; the callee's final identifier. */
const TS_READ_CALLS: ReadonlySet<string> = new Set([
	"readFileSync",
	"readFile",
	"existsSync",
	"statSync",
	"lstatSync",
	"open",
	"openSync",
	"access",
	"accessSync",
]);
const PY_READ_CALLS: ReadonlySet<string> = new Set([
	"open",
	"read_text",
	"read_bytes",
	"readlink",
	"exists",
	"isfile",
	"isdir",
	"stat",
	"lstat",
]);

function isHomeLiteral(text: string): boolean {
	return HOME_RE.test(text);
}

function isHomeString(token: Token): boolean {
	return token.kind === "string" && !token.hasSubstitution && isHomeLiteral(token.value);
}

/** Locate a string opening at `i` (accounting for python r/b/u/f prefixes). */
function stringStartAt(src: string, i: number, dialect: Dialect): { quotePos: number; quote: string } | null {
	const c = src[i];
	if (c === '"' || c === "'" || c === "`") return { quotePos: i, quote: c };
	if (dialect === "py" && /[rbufRBUF]/.test(c)) {
		let j = i;
		while (j < src.length && /[rbufRBUF]/.test(src[j])) j++;
		const quote = src[j];
		if (quote === '"' || quote === "'") return { quotePos: j, quote };
	}
	return null;
}

function readString(
	src: string,
	quotePos: number,
	quote: string,
	dialect: Dialect,
	line: number,
): { token: Token; next: number; lines: number } {
	const triple = dialect === "py" && src.startsWith(quote.repeat(3), quotePos);
	const delim = triple ? quote.repeat(3) : quote;
	let i = quotePos + delim.length;
	let inner = "";
	let lines = 0;
	while (i < src.length) {
		if (!triple && src[i] === "\n") break;
		if (src[i] === "\\") {
			inner += src[i] + (src[i + 1] ?? "");
			i += 2;
			continue;
		}
		if (src.startsWith(delim, i)) {
			i += delim.length;
			break;
		}
		if (src[i] === "\n") lines++;
		inner += src[i];
		i++;
	}
	return {
		token: { kind: "string", value: inner, line, hasSubstitution: quote === "`" && inner.includes("${") },
		next: i,
		lines,
	};
}

function tokenize(src: string, dialect: Dialect): Token[] {
	const tokens: Token[] = [];
	const n = src.length;
	let i = 0;
	let line = 1;
	while (i < n) {
		const c = src[i];
		if (c === "\n") {
			line++;
			i++;
			continue;
		}
		if (c === " " || c === "\t" || c === "\r") {
			i++;
			continue;
		}
		if (dialect === "ts" && c === "/" && src[i + 1] === "/") {
			while (i < n && src[i] !== "\n") i++;
			continue;
		}
		if (c === "/" && src[i + 1] === "*") {
			i += 2;
			while (i < n && !(src[i] === "*" && src[i + 1] === "/")) {
				if (src[i] === "\n") line++;
				i++;
			}
			i += 2;
			continue;
		}
		if (dialect === "py" && c === "#") {
			while (i < n && src[i] !== "\n") i++;
			continue;
		}
		const str = stringStartAt(src, i, dialect);
		if (str) {
			const read = readString(src, str.quotePos, str.quote, dialect, line);
			tokens.push(read.token);
			line += read.lines;
			i = read.next;
			continue;
		}
		if (/[A-Za-z_$]/.test(c)) {
			let j = i + 1;
			while (j < n && /[A-Za-z0-9_$]/.test(src[j])) j++;
			tokens.push({ kind: "ident", value: src.slice(i, j), line, hasSubstitution: false });
			i = j;
			continue;
		}
		tokens.push({ kind: "punct", value: c, line, hasSubstitution: false });
		i++;
	}
	return tokens;
}

/** Index of the delimiter matching the opener at `open`. */
function matchParen(tokens: Token[], open: number): number {
	let depth = 0;
	for (let i = open; i < tokens.length; i++) {
		if (tokens[i].kind !== "punct") continue;
		const v = tokens[i].value;
		if (v === "(" || v === "[" || v === "{") depth++;
		else if (v === ")" || v === "]" || v === "}") {
			depth--;
			if (depth === 0) return i;
		}
	}
	return tokens.length - 1;
}

/** Member path to the callee, e.g. `fs.readFileSync`, `os.path.exists`, `Bun.file`. */
function calleeLabel(tokens: Token[], at: number): string {
	const parts = [tokens[at].value];
	let i = at - 1;
	while (i >= 1 && tokens[i].kind === "punct" && tokens[i].value === "." && tokens[i - 1]?.kind === "ident") {
		parts.unshift(tokens[i - 1].value);
		i -= 2;
	}
	return parts.join(".");
}

/** Inclusive end index of a statement's initializer expression. */
function exprEnd(tokens: Token[], start: number, dialect: Dialect, startLine: number): number {
	let depth = 0;
	for (let i = start; i < tokens.length; i++) {
		const token = tokens[i];
		if (token.kind === "punct") {
			const v = token.value;
			if (v === "(" || v === "[" || v === "{") depth++;
			else if (v === ")" || v === "]" || v === "}") depth--;
			else if (v === ";" && depth <= 0) return i - 1;
		}
		if (dialect === "py" && depth <= 0 && token.line > startLine) return i - 1;
	}
	return tokens.length - 1;
}

/**
 * Does the token slice denote a path value derived from a /home/ or /root/
 * literal? Accepts a bare home string, a same-file constant, and a
 * join/resolve-style call containing one; rejects object/array literals so a
 * fixture field is not mistaken for a path.
 */
function qualifyExpression(
	tokens: Token[],
	start: number,
	end: number,
	bindings: Map<string, string>,
): string | undefined {
	if (start > end) return undefined;
	const slice = tokens.slice(start, end + 1);
	if (slice.length === 1 && isHomeString(slice[0])) return slice[0].value;
	if (slice.length === 1 && slice[0].kind === "ident" && bindings.has(slice[0].value)) {
		return bindings.get(slice[0].value);
	}
	if (slice.some(t => t.value === "{" || t.value === "[")) return undefined;
	if (!slice.some(t => t.kind === "punct" && t.value === "(")) return undefined;
	for (const t of slice) {
		if (isHomeString(t)) return t.value;
	}
	for (const t of slice) {
		if (t.kind === "ident" && bindings.has(t.value)) return bindings.get(t.value);
	}
	return undefined;
}

/** Same-file constants whose initializer is a home/root path value. */
function collectBindings(tokens: Token[], dialect: Dialect): Map<string, string> {
	const bindings = new Map<string, string>();
	let i = 0;
	while (i < tokens.length) {
		const token = tokens[i];
		let name: Token | undefined;
		let eq = -1;
		if (dialect === "ts") {
			if (
				token.kind === "ident" &&
				(token.value === "const" || token.value === "let" || token.value === "var") &&
				tokens[i + 1]?.kind === "ident"
			) {
				const nameIndex = i + 1;
				let j = nameIndex + 1;
				// Stop at the statement's `=`; a closing paren before any `=` means
				// this is a `for (const x of y)` head, not a binding.
				while (j < tokens.length && tokens[j].value !== "=" && tokens[j].value !== ";" && tokens[j].value !== ")") {
					j++;
				}
				if (tokens[j]?.value === "=") {
					name = tokens[nameIndex];
					eq = j;
				}
			}
		} else {
			const prev = tokens[i - 1];
			const statementStart =
				i === 0 ||
				prev === undefined ||
				prev.value === ";" ||
				prev.value === ")" ||
				prev.value === ":" ||
				prev.line !== token.line;
			if (token.kind === "ident" && statementStart && tokens[i + 1]?.value === "=" && tokens[i + 2]?.value !== "=") {
				name = token;
				eq = i + 1;
			}
		}
		if (name && eq >= 0) {
			const end = exprEnd(tokens, eq + 1, dialect, token.line);
			const home = qualifyExpression(tokens, eq + 1, end, bindings);
			if (home) bindings.set(name.value, home);
			i = end + 1;
			continue;
		}
		i++;
	}
	return bindings;
}

/** Home literal passed to a `Path(...)`/const receiver of a Python method call. */
function receiverHome(tokens: Token[], at: number, bindings: Map<string, string>): string | undefined {
	if (tokens[at - 1]?.value !== ".") return undefined;
	const recvEnd = at - 2;
	const recv = tokens[recvEnd];
	if (recv?.kind === "ident" && bindings.has(recv.value)) return bindings.get(recv.value);
	if (recv?.value !== ")") return undefined;
	let depth = 0;
	let j = recvEnd;
	for (; j >= 0; j--) {
		if (tokens[j].kind !== "punct") continue;
		if (tokens[j].value === ")") depth++;
		else if (tokens[j].value === "(") {
			depth--;
			if (depth === 0) break;
		}
	}
	const callee = tokens[j - 1];
	if (callee?.kind !== "ident" || (callee.value !== "Path" && callee.value !== "Pathlib")) return undefined;
	const args = tokens.slice(j + 1, recvEnd);
	for (const a of args) {
		if (isHomeString(a)) return a.value;
	}
	for (const a of args) {
		if (a.kind === "ident" && bindings.has(a.value)) return bindings.get(a.value);
	}
	return undefined;
}

/** Scan one source file for reads of /home/ or /root/ absolute paths. */
export function scan(source: string, path: string): ExternalReadHit[] {
	const dialect: Dialect = path.toLowerCase().endsWith(".py") ? "py" : "ts";
	const tokens = tokenize(source, dialect);
	const bindings = collectBindings(tokens, dialect);
	const readCalls = dialect === "py" ? PY_READ_CALLS : TS_READ_CALLS;
	const hits: ExternalReadHit[] = [];
	const seen = new Set<string>();

	for (let i = 0; i < tokens.length; i++) {
		const token = tokens[i];
		if (token.kind !== "ident") continue;
		let isRead = readCalls.has(token.value);
		// `Bun.file` — final identifier `file` qualified by the `Bun` receiver.
		if (
			!isRead &&
			dialect === "ts" &&
			token.value === "file" &&
			tokens[i - 1]?.value === "." &&
			tokens[i - 2]?.value === "Bun"
		) {
			isRead = true;
		}
		if (!isRead) continue;
		if (tokens[i + 1]?.value !== "(") continue;
		const close = matchParen(tokens, i + 1);
		const args = tokens.slice(i + 2, close);
		let home = args.find(isHomeString)?.value;
		if (!home) {
			for (const a of args) {
				if (a.kind === "ident" && bindings.has(a.value)) {
					home = bindings.get(a.value);
					break;
				}
			}
		}
		if (!home && dialect === "py") home = receiverHome(tokens, i, bindings);
		if (!home) continue;
		const call = calleeLabel(tokens, i);
		const key = `${token.line}:${call}:${home}`;
		if (seen.has(key)) continue;
		seen.add(key);
		hits.push({ line: token.line, call, path: home });
	}
	hits.sort((a, b) => a.line - b.line);
	return hits;
}

/** Tracked test sources: `*.test.ts[x]`, `test_*.py`, `*_test.py`, and ts/js/py under a `tests/` segment. */
export function isTestSource(filePath: string): boolean {
	const p = filePath.replaceAll("\\", "/");
	if (/\.test\.(ts|tsx)$/.test(p)) return true;
	if (/(^|\/)test_[^/]*\.py$/.test(p)) return true;
	if (/(^|\/)[^/]*_test\.py$/.test(p)) return true;
	if (/(^|\/)tests\//.test(p) && /\.(ts|tsx|js|mjs|py)$/.test(p)) return true;
	return false;
}

/** Scan every tracked test source under the current directory. */
export async function scanRepository(): Promise<Array<ExternalReadHit & { file: string }>> {
	const result = await $`git ls-files`.quiet().nothrow();
	if (result.exitCode !== 0) {
		throw new Error(`git ls-files failed: ${result.stderr.toString().trim()}`);
	}
	const files = result
		.text()
		.split("\n")
		.map(line => line.trim())
		.filter(line => line.length > 0 && isTestSource(line));
	const hits: Array<ExternalReadHit & { file: string }> = [];
	for (const file of files) {
		let source: string;
		try {
			source = await Bun.file(file).text();
		} catch {
			continue;
		}
		for (const hit of scan(source, file)) hits.push({ file, ...hit });
	}
	hits.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line);
	return hits;
}

async function main(): Promise<void> {
	const hits = await scanRepository();
	for (const hit of hits) console.log(`${hit.file}:${hit.line}: ${hit.call} ${hit.path}`);
	if (hits.length > 0) {
		console.error(`FAIL: ${hits.length} test source read(s) of an absolute /home/ or /root/ path`);
		process.exit(1);
	}
	console.log("PASS: no tracked test source reads an absolute /home/ or /root/ path");
}

if (import.meta.main) {
	await main();
}
