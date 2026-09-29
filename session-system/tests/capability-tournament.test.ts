import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import type { Api, AssistantMessage, Context, Model, SimpleStreamOptions, Usage } from "@oh-my-pi/pi-ai";
import { runTournamentCapability } from "../capabilities/tournament";

const repoRoot = path.resolve(import.meta.dir, "../..");
const cli = path.join(repoRoot, "session-system/capabilities/tournament.ts");

function usage(): Usage {
	return {
		input: 1,
		output: 1,
		cacheRead: 0,
		cacheWrite: 0,
		totalTokens: 2,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
	};
}

function complete(_model: Model<Api>, context: Context, _options?: SimpleStreamOptions): Promise<AssistantMessage> {
	const message = context.messages[0];
	const prompt = message && typeof message.content === "string" ? message.content : "";
	const labels = [...prompt.matchAll(/^Candidate ([^:\n]+):$/gm)].map(match => match[1]).filter(Boolean);
	const winner = labels[0];
	if (!winner) throw new Error("injected completer found no candidate label");
	return Promise.resolve({
		role: "assistant",
		api: "openai-completions",
		provider: "openai",
		model: "gpt-4o-mini",
		content: [{ type: "text", text: JSON.stringify({ winner }) }],
		stopReason: "stop",
		timestamp: Date.now(),
		usage: usage(),
	});
}

const threeHypotheses = {
	id: "t-3",
	question: "Which hypothesis is stronger?",
	hypotheses: [
		{ id: "h1", title: "First", writeUp: "First hypothesis argues from measurement." },
		{ id: "h2", title: "Second", writeUp: "Second hypothesis argues from mechanism." },
		{ id: "h3", title: "Third", writeUp: "Third hypothesis argues from replication." },
	],
	seed: 7,
	swissRoundCap: 3,
};

async function runCli(args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
	const env = { ...process.env };
	delete env.FORCE_COLOR;
	delete env.NO_COLOR;
	const proc = Bun.spawn(["bun", cli, ...args], {
		cwd: repoRoot,
		stdin: "ignore",
		stdout: "pipe",
		stderr: "pipe",
		env,
	});
	const [stdout, stderr, code] = await Promise.all([
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
		proc.exited,
	]);
	return { code, stdout, stderr };
}

describe("tournament capability", () => {
	test("a 3-hypothesis tournament with an injected completer passes with standings", async () => {
		const result = await runTournamentCapability(
			{
				...threeHypotheses,
				judges: [{ id: "judge-a", family: "alpha", model: "openai/gpt-4o-mini" }],
			},
			{ complete },
		);
		expect(result.capability).toBe("tournament");
		expect(result.passed).toBe(true);
		expect(result.standings.map(entry => entry.id).sort()).toEqual(["h1", "h2", "h3"]);
		expect(result.comparisons.length).toBeGreaterThan(0);
		for (const comparison of result.comparisons) {
			expect(comparison.judgeId).toBe("judge-a");
			expect(comparison.judgeFamily).toBe("alpha");
		}
	});

	test("two judges of the same family exit 1", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "tournament-cli-"));
		try {
			const inputPath = path.join(dir, "input.json");
			await Bun.write(
				inputPath,
				JSON.stringify({
					...threeHypotheses,
					judges: [
						{ id: "j1", family: "same", model: "openai/gpt-4o-mini" },
						{ id: "j2", family: "same", model: "openai/gpt-4o-mini" },
					],
				}),
			);
			const result = await runCli(["--input", inputPath]);
			expect(result.code).toBe(1);
			expect(result.stderr).toBe("");
			const body = JSON.parse(result.stdout) as { error?: string };
			expect(body.error).toContain("distinct families");
		} finally {
			await fs.rm(dir, { recursive: true, force: true });
		}
	}, 30_000);

	test("bad input exits 1 with an error object", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "tournament-cli-bad-"));
		try {
			const inputPath = path.join(dir, "input.json");
			await Bun.write(inputPath, "{");
			const result = await runCli(["--input", inputPath]);
			expect(result.code).toBe(1);
			const body = JSON.parse(result.stdout) as { error?: string };
			expect(typeof body.error).toBe("string");
			expect(body.error?.length).toBeGreaterThan(0);
		} finally {
			await fs.rm(dir, { recursive: true, force: true });
		}
	}, 30_000);
});
