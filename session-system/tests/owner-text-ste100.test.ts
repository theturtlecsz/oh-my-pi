import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { setAgentDir } from "@oh-my-pi/pi-utils";
import { clearCache } from "../../packages/coding-agent/src/capability/fs";
import { buildSystemPrompt } from "../../packages/coding-agent/src/system-prompt";

const STE100_PARAGRAPH =
	"Owner text format is ASD-STE100 (Simplified Technical English; owner directive, 2026-10-02). Any text output to Chris must follow ASD-STE100: approved words with their approved meanings, technical names and verbs where needed, short sentences (procedures at most 20 words, descriptions at most 25), one instruction per sentence, imperative for instructions, active voice, simple tenses, articles kept, no contractions, no idioms. Code, commands, quoted errors, identifiers and Work Ledger evidence stay exact. This rule governs all text output Chris reads and takes precedence over caveman, ponytail, older style laws, and other terse or compressed styles for that text.";

function normalizeWs(text: string): string {
	return text.replace(/\s+/g, " ").trim();
}

const tempDirs: string[] = [];
let originalAgentDir: string | undefined;

beforeEach(() => {
	originalAgentDir = process.env.PI_CODING_AGENT_DIR;
	clearCache();
});

afterEach(async () => {
	if (originalAgentDir !== undefined) setAgentDir(originalAgentDir);
	else delete process.env.PI_CODING_AGENT_DIR;
	clearCache();
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
});

async function makeTempDir(prefix: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

describe("owner text ASD-STE100 system prompt contract", () => {
	test("an omp session gets the rule in its system prompt and removes household terms", async () => {
		const agentDir = await makeTempDir("omp-ste100-agent-");
		const repoDoctrine = path.resolve(import.meta.dir, "../agents/omp-AGENTS.md");
		await fs.symlink(repoDoctrine, path.join(agentDir, "AGENTS.md"));
		setAgentDir(agentDir);

		const emptyCwd = await makeTempDir("omp-ste100-cwd-");
		const { systemPrompt } = await buildSystemPrompt({ cwd: emptyCwd });

		const joined = normalizeWs(systemPrompt.join("\n\n"));
		const normalizedParagraph = normalizeWs(STE100_PARAGRAPH);

		const occurrences = joined.split(normalizedParagraph).length - 1;
		expect(occurrences).toBe(1);
		expect(joined).not.toContain("household terms");
	});
});
