#!/usr/bin/env bun
/**
 * build-sets.ts
 *
 * Extracts measurement datasets from session history and, when present, the robomp sqlite DB:
 * - prompts: prompt + its auto thinking_level_change effort; manual change overrides
 * - turn ends: stop candidates; continue if retry reminder or continue/go on/proceed follows, else stop
 * - issues: issue_index rows whose labels_json holds one primary label, or an empty set with --no-robomp
 */

import { Database } from "bun:sqlite";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { isUnexpectedStopCandidate } from "@oh-my-pi/pi-coding-agent/session/unexpected-stop-classifier";

export const PRIMARY_LABELS = new Set([
	"bug",
	"enhancement",
	"question",
	"proposal",
	"documentation",
	"wontfix",
	"invalid",
	"duplicate",
]);

export interface PromptRecord {
	prompt: string;
	effort: string;
	label?: string;
	text?: string;
}

export interface TurnEndRecord {
	text: string;
	label: "continue" | "stop";
}

export interface IssueRecord {
	key: string;
	repo: string;
	number: number;
	title: string;
	body: string;
	label: string;
}

function extractText(content: unknown): string {
	if (typeof content === "string") return content;
	if (Array.isArray(content)) {
		return content
			.filter((c: any) => c && typeof c === "object" && (c.type === "text" || c.text))
			.map((c: any) => c.text ?? "")
			.join("\n")
			.trim();
	}
	return "";
}

function isRetryReminder(msg: any): boolean {
	if (!msg) return false;
	const text = extractText(msg.content);
	if (msg.role === "developer" || msg.role === "system") {
		return (
			text.includes("You said you would continue with a tool call or action but stopped") ||
			text.includes("<system-injection>") ||
			text.includes("Attempt #") ||
			text.includes("unexpected-stop-retry")
		);
	}
	return text.includes("You said you would continue with a tool call or action but stopped");
}

function isUserContinue(msg: any): boolean {
	if (!msg || msg.role !== "user") return false;
	const text = extractText(msg.content).trim();
	return /\b(continue|go on|proceed)\b/i.test(text);
}

export async function extractPromptsFromSession(entries: any[]): Promise<PromptRecord[]> {
	const records: PromptRecord[] = [];
	let currentPrompt: string | null = null;
	let currentEffort: string | null = null;
	let hadAutoThinking = false;

	const flush = () => {
		if (currentPrompt !== null && hadAutoThinking && currentEffort !== null) {
			records.push({
				prompt: currentPrompt,
				effort: currentEffort,
				label: currentEffort,
				text: currentPrompt,
			});
		}
	};

	for (const entry of entries) {
		if (!entry || typeof entry !== "object") continue;

		if (entry.type === "message" && entry.message?.role === "user") {
			flush();
			currentPrompt = extractText(entry.message.content);
			currentEffort = null;
			hadAutoThinking = false;
		} else if (entry.type === "thinking_level_change") {
			if (entry.configured === "auto") {
				currentEffort = entry.thinkingLevel ?? entry.resolved ?? null;
				hadAutoThinking = true;
			} else if (currentPrompt !== null && entry.thinkingLevel) {
				// Manual change before next prompt overrides
				currentEffort = entry.thinkingLevel;
			}
		}
	}
	flush();
	return records;
}

export async function extractTurnEndsFromSession(entries: any[]): Promise<TurnEndRecord[]> {
	const records: TurnEndRecord[] = [];

	for (let i = 0; i < entries.length; i++) {
		const entry = entries[i];
		if (entry?.type !== "message" || entry.message?.role !== "assistant") {
			continue;
		}

		const assistantMsg = entry.message;
		if (!isUnexpectedStopCandidate(assistantMsg)) {
			continue;
		}

		const assistantText = extractText(assistantMsg.content);

		// Look ahead to find the next meaningful follow-up turn
		let label: "continue" | "stop" = "stop";
		for (let j = i + 1; j < entries.length; j++) {
			const nextEntry = entries[j];
			if (nextEntry?.type === "message" && nextEntry.message) {
				const nextMsg = nextEntry.message;
				if (isRetryReminder(nextMsg) || isUserContinue(nextMsg)) {
					label = "continue";
					break;
				}
				// If a new user message arrives that is not a continue/proceed, it's a new topic
				if (nextMsg.role === "user") {
					label = "stop";
					break;
				}
			}
		}

		records.push({
			text: assistantText,
			label,
		});
	}

	return records;
}

export async function buildPromptsSet(sessionsDir: string): Promise<PromptRecord[]> {
	const prompts: PromptRecord[] = [];
	const files = await listJsonlFiles(sessionsDir);
	for (const file of files) {
		const entries = await readJsonl(file);
		const extracted = await extractPromptsFromSession(entries);
		prompts.push(...extracted);
	}
	return prompts;
}

export async function buildTurnEndsSet(sessionsDir: string): Promise<TurnEndRecord[]> {
	const turnEnds: TurnEndRecord[] = [];
	const files = await listJsonlFiles(sessionsDir);
	for (const file of files) {
		const entries = await readJsonl(file);
		const extracted = await extractTurnEndsFromSession(entries);
		turnEnds.push(...extracted);
	}
	return turnEnds;
}

export async function buildIssuesSet(dbPath: string): Promise<IssueRecord[]> {
	const issues: IssueRecord[] = [];
	const db = new Database(dbPath, { readonly: true });
	try {
		const rows = db
			.query<{ repo: string; number: number; title: string; body: string; labels_json: string }, []>(
				"SELECT repo, number, title, body, labels_json FROM issue_index",
			)
			.all();

		for (const row of rows) {
			let labels: string[] = [];
			try {
				labels = JSON.parse(row.labels_json || "[]");
			} catch {
				labels = [];
			}

			const primaryMatches = labels.filter(l => PRIMARY_LABELS.has(l));
			if (primaryMatches.length === 1) {
				const label = primaryMatches[0];
				issues.push({
					key: `${row.repo}#${row.number}`,
					repo: row.repo,
					number: row.number,
					title: row.title,
					body: row.body,
					label,
				});
			}
		}
	} finally {
		db.close();
	}
	return issues;
}

async function listJsonlFiles(dir: string): Promise<string[]> {
	const results: string[] = [];
	try {
		const entries = await fs.readdir(dir, { withFileTypes: true });
		for (const ent of entries) {
			const full = path.join(dir, ent.name);
			if (ent.isDirectory()) {
				results.push(...(await listJsonlFiles(full)));
			} else if (ent.isFile() && ent.name.endsWith(".jsonl")) {
				results.push(full);
			}
		}
	} catch {
		// Directory may not exist or not readable
	}
	return results;
}

async function readJsonl(file: string): Promise<any[]> {
	try {
		const text = await Bun.file(file).text();
		return text
			.split("\n")
			.map(l => l.trim())
			.filter(Boolean)
			.map(l => JSON.parse(l));
	} catch {
		return [];
	}
}

export async function buildSets(options: {
	sessionsDir: string;
	outDir: string;
	dbPath?: string;
	noRobomp?: boolean;
}): Promise<{ promptCount: number; turnEndCount: number; issueCount: number }> {
	// Exactly one source: a robomp DB, or an explicit empty issues set.
	if (Boolean(options.noRobomp) === Boolean(options.dbPath)) {
		throw new Error("buildSets requires exactly one of dbPath or noRobomp");
	}

	await fs.mkdir(options.outDir, { recursive: true });

	const prompts = await buildPromptsSet(options.sessionsDir);
	const turnEnds = await buildTurnEndsSet(options.sessionsDir);
	const issues = options.noRobomp ? [] : await buildIssuesSet(options.dbPath!);

	const promptsFile = path.join(options.outDir, "prompts.jsonl");
	const turnEndsFile = path.join(options.outDir, "turn-ends.jsonl");
	const turnEndsFileAlt = path.join(options.outDir, "turn_ends.jsonl");
	const issuesFile = path.join(options.outDir, "issues.jsonl");

	await Bun.write(promptsFile, prompts.map(p => JSON.stringify(p)).join("\n") + (prompts.length ? "\n" : ""));
	const turnEndsText = turnEnds.map(t => JSON.stringify(t)).join("\n") + (turnEnds.length ? "\n" : "");
	await Bun.write(turnEndsFile, turnEndsText);
	await Bun.write(turnEndsFileAlt, turnEndsText);
	await Bun.write(issuesFile, issues.map(i => JSON.stringify(i)).join("\n") + (issues.length ? "\n" : ""));

	return {
		promptCount: prompts.length,
		turnEndCount: turnEnds.length,
		issueCount: issues.length,
	};
}

async function main() {
	const args = process.argv.slice(2);
	let sessionsDir = "";
	let dbPath = "";
	let outDir = "";
	let noRobomp = false;

	for (let i = 0; i < args.length; i++) {
		const arg = args[i];
		if (arg === "--sessions" && i + 1 < args.length) {
			sessionsDir = args[++i];
		} else if (arg.startsWith("--sessions=")) {
			sessionsDir = arg.slice("--sessions=".length);
		} else if (arg === "--robomp-db" && i + 1 < args.length) {
			dbPath = args[++i];
		} else if (arg.startsWith("--robomp-db=")) {
			dbPath = arg.slice("--robomp-db=".length);
		} else if (arg === "--no-robomp") {
			noRobomp = true;
		} else if (arg === "--out" && i + 1 < args.length) {
			outDir = args[++i];
		} else if (arg.startsWith("--out=")) {
			outDir = arg.slice("--out=".length);
		}
	}

	if (!sessionsDir || !outDir || noRobomp === Boolean(dbPath)) {
		console.error("Usage: build-sets.ts --sessions <dir> (--robomp-db <sqlite> | --no-robomp) --out <dir>");
		process.exit(1);
	}

	const counts = await buildSets(noRobomp ? { sessionsDir, outDir, noRobomp: true } : { sessionsDir, outDir, dbPath });
	console.log(`Generated sets in ${outDir}:`);
	console.log(`- prompts.jsonl: ${counts.promptCount} items`);
	console.log(`- turn-ends.jsonl: ${counts.turnEndCount} items`);
	console.log(`- issues.jsonl: ${counts.issueCount} items`);
}

if (import.meta.main) {
	await main();
}
