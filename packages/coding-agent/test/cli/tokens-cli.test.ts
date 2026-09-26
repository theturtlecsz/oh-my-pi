import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { countTokens, Encoding } from "@oh-my-pi/pi-natives";
import { runTokenCounter } from "../../src/cli/tokens-cli";

const cliEntry = path.join(import.meta.dir, "..", "..", "src", "cli.ts");

function createStream(text: string): ReadableStream<Uint8Array> {
	return new Response(text).body!;
}

describe("tokens-cli", () => {
	it("processes two input lines into a profile line followed by token counts in input order", async () => {
		const textA = "first line text to count";
		const textB = "second line text with different content";
		const inputContent = `${JSON.stringify({ id: "item-1", text: textA })}\n${JSON.stringify({ id: "item-2", text: textB })}\n`;

		const written: string[] = [];
		await runTokenCounter(
			{ encoding: "ClaudeV5" },
			{
				input: createStream(inputContent),
				write: (line: string) => {
					written.push(line);
				},
			},
		);

		expect(written).toHaveLength(3);
		expect(written[0].endsWith("\n")).toBe(true);
		expect(written[1].endsWith("\n")).toBe(true);
		expect(written[2].endsWith("\n")).toBe(true);

		const profile = JSON.parse(written[0]);
		expect(profile).toEqual({
			api: "countTokens",
			encoding: "ClaudeV5",
		});

		const result1 = JSON.parse(written[1]);
		expect(result1).toEqual({
			id: "item-1",
			tokens: countTokens(textA, Encoding.ClaudeV5),
		});

		const result2 = JSON.parse(written[2]);
		expect(result2).toEqual({
			id: "item-2",
			tokens: countTokens(textB, Encoding.ClaudeV5),
		});
	});

	it("skips blank and whitespace-only lines without disrupting counting", async () => {
		const textA = "alpha";
		const textB = "beta";
		const inputContent = `   \n${JSON.stringify({ id: "a", text: textA })}\n\n\t  \n${JSON.stringify({ id: "b", text: textB })}\n\n`;

		const written: string[] = [];
		await runTokenCounter(
			{ encoding: "ClaudeV5" },
			{
				input: createStream(inputContent),
				write: (line: string) => {
					written.push(line);
				},
			},
		);

		expect(written).toHaveLength(3);
		expect(JSON.parse(written[0])).toEqual({ api: "countTokens", encoding: "ClaudeV5" });
		expect(JSON.parse(written[1])).toEqual({ id: "a", tokens: countTokens(textA, Encoding.ClaudeV5) });
		expect(JSON.parse(written[2])).toEqual({ id: "b", tokens: countTokens(textB, Encoding.ClaudeV5) });
	});

	it("throws and writes nothing when encoding is unknown", async () => {
		const written: string[] = [];
		const io = {
			input: createStream(`${JSON.stringify({ id: "a", text: "hello" })}\n`),
			write: (line: string) => {
				written.push(line);
			},
		};

		await expect(runTokenCounter({ encoding: "UnknownOrInvalidEncoding" }, io)).rejects.toThrow(
			"unsupported encoding: UnknownOrInvalidEncoding",
		);
		expect(written).toHaveLength(0);
	});

	it("throws naming line 2 after writing the first count when the second line is malformed JSON", async () => {
		const textA = "valid first line";
		const inputContent = `${JSON.stringify({ id: "1", text: textA })}\nthis is not valid json\n`;

		const written: string[] = [];
		const io = {
			input: createStream(inputContent),
			write: (line: string) => {
				written.push(line);
			},
		};

		await expect(runTokenCounter({ encoding: "ClaudeV5" }, io)).rejects.toThrow(/line 2/);
		expect(written).toHaveLength(2);
		expect(JSON.parse(written[0])).toEqual({ api: "countTokens", encoding: "ClaudeV5" });
		expect(JSON.parse(written[1])).toEqual({ id: "1", tokens: countTokens(textA, Encoding.ClaudeV5) });
	});

	it("throws naming line 2 after writing the first count when the second line has invalid shape", async () => {
		const textA = "valid first line";
		const inputContent = `${JSON.stringify({ id: "1", text: textA })}\n${JSON.stringify({ id: "2" })}\n`;

		const written: string[] = [];
		const io = {
			input: createStream(inputContent),
			write: (line: string) => {
				written.push(line);
			},
		};

		await expect(runTokenCounter({ encoding: "ClaudeV5" }, io)).rejects.toThrow(/line 2/);
		expect(written).toHaveLength(2);
		expect(JSON.parse(written[0])).toEqual({ api: "countTokens", encoding: "ClaudeV5" });
		expect(JSON.parse(written[1])).toEqual({ id: "1", tokens: countTokens(textA, Encoding.ClaudeV5) });
	});

	it("tracks line numbers accurately across intermediate blank lines", async () => {
		const textA = "valid first line";
		const inputContent = `${JSON.stringify({ id: "1", text: textA })}\n\nnot json at line 3\n`;

		const written: string[] = [];
		const io = {
			input: createStream(inputContent),
			write: (line: string) => {
				written.push(line);
			},
		};

		await expect(runTokenCounter({ encoding: "ClaudeV5" }, io)).rejects.toThrow(/line 3/);
		expect(written).toHaveLength(2);
		expect(JSON.parse(written[0])).toEqual({ api: "countTokens", encoding: "ClaudeV5" });
		expect(JSON.parse(written[1])).toEqual({ id: "1", tokens: countTokens(textA, Encoding.ClaudeV5) });
	});

	it("supports calling with input stream and stdout sink", async () => {
		const written: string[] = [];
		await runTokenCounter(
			{ encoding: "ClaudeV5" },
			createStream(`${JSON.stringify({ id: "direct", text: "hello" })}\n`),
			{
				write(line: string) {
					written.push(line);
				},
			},
		);

		expect(written).toHaveLength(2);
		expect(JSON.parse(written[0])).toEqual({ api: "countTokens", encoding: "ClaudeV5" });
		expect(JSON.parse(written[1])).toEqual({ id: "direct", tokens: countTokens("hello", Encoding.ClaudeV5) });
	});

	it("CLI prints two JSON lines and exits with 0 for valid input", async () => {
		const proc = Bun.spawn([process.execPath, cliEntry, "tokens", "count", "--encoding", "ClaudeV5"], {
			stdin: "pipe",
			stdout: "pipe",
			stderr: "pipe",
		});

		proc.stdin.write('{"id":"a","text":"hello world"}\n');
		proc.stdin.flush();
		proc.stdin.end();

		const [exitCode, stdout, stderr] = await Promise.all([
			proc.exited,
			new Response(proc.stdout).text(),
			new Response(proc.stderr).text(),
		]);

		expect(exitCode).toBe(0);
		expect(stderr).toBe("");
		const lines = stdout.trim().split("\n");
		expect(lines).toHaveLength(2);
		expect(JSON.parse(lines[0])).toEqual({ api: "countTokens", encoding: "ClaudeV5" });
		expect(JSON.parse(lines[1])).toEqual({ id: "a", tokens: countTokens("hello world", Encoding.ClaudeV5) });
	});

	it("CLI writes error to stderr and exits with 2 on unsupported encoding", async () => {
		const proc = Bun.spawn([process.execPath, cliEntry, "tokens", "count", "--encoding", "InvalidEncoding"], {
			stdin: "pipe",
			stdout: "pipe",
			stderr: "pipe",
		});

		proc.stdin.write('{"id":"a","text":"hello world"}\n');
		proc.stdin.flush();
		proc.stdin.end();

		const [exitCode, stdout, stderr] = await Promise.all([
			proc.exited,
			new Response(proc.stdout).text(),
			new Response(proc.stderr).text(),
		]);

		expect(exitCode).toBe(2);
		expect(stdout).toBe("");
		expect(stderr).toContain("unsupported encoding: InvalidEncoding");
	});

	it("CLI outputs prior counts, writes error to stderr and exits with 2 on malformed second line", async () => {
		const proc = Bun.spawn([process.execPath, cliEntry, "tokens", "count", "--encoding", "ClaudeV5"], {
			stdin: "pipe",
			stdout: "pipe",
			stderr: "pipe",
		});

		proc.stdin.write('{"id":"a","text":"hello world"}\nnot-json\n');
		proc.stdin.flush();
		proc.stdin.end();

		const [exitCode, stdout, stderr] = await Promise.all([
			proc.exited,
			new Response(proc.stdout).text(),
			new Response(proc.stderr).text(),
		]);

		expect(exitCode).toBe(2);
		const lines = stdout.trim().split("\n");
		expect(lines).toHaveLength(2);
		expect(JSON.parse(lines[0])).toEqual({ api: "countTokens", encoding: "ClaudeV5" });
		expect(JSON.parse(lines[1])).toEqual({ id: "a", tokens: countTokens("hello world", Encoding.ClaudeV5) });
		expect(stderr).toContain("line 2");
	});
});
