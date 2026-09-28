import * as fs from "node:fs";
import * as path from "node:path";

export const LOCK_COLUMNS = [
	"ID",
	"Lock",
	"Location",
	"Affected runs",
	"Breaks",
	"Upstream divergence",
] as const;

export const DIVERGENCE = [
	"none",
	"fork-only",
	"adds",
	"new",
	"modified",
] as const;

export type LockColumn = (typeof LOCK_COLUMNS)[number];
export type Divergence = (typeof DIVERGENCE)[number];
export type LockRow = Record<LockColumn, string>;

export const REPO_ROOT = path.resolve(import.meta.dir, "../../..");
export const REPORT_DIR = path.resolve(REPO_ROOT, "docs/reports/unattended-lockdown");

function splitTableRow(line: string): string[] {
	const trimmed = line.trim();
	if (!trimmed.includes("|")) return [];
	let content = trimmed;
	if (content.startsWith("|")) content = content.slice(1);
	if (content.endsWith("|")) content = content.slice(0, -1);

	const cells: string[] = [];
	let current = "";
	let escaped = false;
	for (let i = 0; i < content.length; i++) {
		const char = content[i];
		if (escaped) {
			current += char;
			escaped = false;
		} else if (char === "\\") {
			current += char;
			escaped = true;
		} else if (char === "|") {
			cells.push(current.trim());
			current = "";
		} else {
			current += char;
		}
	}
	cells.push(current.trim());
	return cells;
}

export function parseLockRows(md: string): LockRow[] {
	const lines = md.split("\n");
	const rows: LockRow[] = [];
	let inLockTable = false;

	for (let i = 0; i < lines.length; i++) {
		const line = lines[i]?.trim() ?? "";
		if (!line.includes("|")) {
			inLockTable = false;
			continue;
		}

		const cells = splitTableRow(line);
		if (!inLockTable) {
			if (
				cells.length === LOCK_COLUMNS.length &&
				cells.every((c, idx) => c.toLowerCase() === LOCK_COLUMNS[idx].toLowerCase())
			) {
				inLockTable = true;
				const nextLine = lines[i + 1]?.trim() ?? "";
				if (nextLine.includes("|")) {
					const nextCells = splitTableRow(nextLine);
					if (nextCells.every(c => /^:?-+:?$/.test(c))) {
						i++;
					}
				}
			}
		} else {
			if (cells.every(c => /^:?-+:?$/.test(c))) {
				continue;
			}
			if (cells.length !== LOCK_COLUMNS.length) {
				inLockTable = false;
				continue;
			}
			const row: LockRow = {
				ID: cells[0],
				Lock: cells[1],
				Location: cells[2],
				"Affected runs": cells[3],
				Breaks: cells[4],
				"Upstream divergence": cells[5],
			};
			rows.push(row);
		}
	}

	return rows;
}

export function unresolvedLinks(docRel: string, md: string): string[] {
	let docPath: string;
	if (path.isAbsolute(docRel)) {
		docPath = docRel;
	} else if (fs.existsSync(path.resolve(REPORT_DIR, docRel))) {
		docPath = path.resolve(REPORT_DIR, docRel);
	} else if (fs.existsSync(path.resolve(REPO_ROOT, docRel))) {
		docPath = path.resolve(REPO_ROOT, docRel);
	} else {
		docPath = path.resolve(REPORT_DIR, docRel);
	}
	const docDir = path.dirname(docPath);
	const unresolved: string[] = [];

	const linkRegex = /\[(?:[^\]]*)\]\(([^)]+)\)/g;
	let match: RegExpExecArray | null;

	while ((match = linkRegex.exec(md)) !== null) {
		let rawTarget = match[1].trim();
		if (rawTarget.startsWith("<") && rawTarget.endsWith(">")) {
			rawTarget = rawTarget.slice(1, -1).trim();
		}
		const spaceIdx = rawTarget.search(/\s+["']/);
		if (spaceIdx !== -1) {
			rawTarget = rawTarget.slice(0, spaceIdx).trim();
		}
		if (/^(?:https?|mailto):/i.test(rawTarget) || rawTarget.includes("://")) {
			continue;
		}
		if (rawTarget.startsWith("#")) {
			continue;
		}
		const cleanPath = rawTarget.split("#")[0].split("?")[0].trim();
		if (cleanPath === "") {
			continue;
		}
		const resolvedPath = path.resolve(docDir, cleanPath);
		if (!fs.existsSync(resolvedPath)) {
			unresolved.push(rawTarget);
		}
	}

	return unresolved;
}
