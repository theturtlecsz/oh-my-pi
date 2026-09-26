import { countTokens, Encoding } from "@oh-my-pi/pi-natives";
import { readLines } from "@oh-my-pi/pi-utils";

export interface TokenCounterArgs {
	encoding: string;
}

export interface TokenCounterIo {
	input: ReadableStream<Uint8Array>;
	write(line: string): void;
}

interface CounterInput {
	id: string;
	text: string;
}

export async function runTokenCounter(args: TokenCounterArgs, io: TokenCounterIo): Promise<void>;
export async function runTokenCounter(
	args: TokenCounterArgs,
	input: ReadableStream<Uint8Array>,
	stdout: { write(line: string): unknown },
): Promise<void>;
export async function runTokenCounter(
	args: TokenCounterArgs,
	ioOrInput: TokenCounterIo | ReadableStream<Uint8Array>,
	maybeStdout?: { write(line: string): unknown },
): Promise<void> {
	const io: TokenCounterIo =
		maybeStdout && ioOrInput instanceof ReadableStream
			? {
					input: ioOrInput,
					write: (line: string) => {
						maybeStdout.write(line.endsWith("\n") ? line : `${line}\n`);
					},
				}
			: (ioOrInput as TokenCounterIo);

	const encoding = Object.values(Encoding).find(value => value === args.encoding);
	if (!encoding) {
		throw new Error(`unsupported encoding: ${args.encoding}`);
	}

	io.write(`${JSON.stringify({ api: "countTokens", encoding })}\n`);

	const decoder = new TextDecoder("utf-8", { fatal: true });
	let lineNumber = 0;
	for await (const bytes of readLines(io.input)) {
		lineNumber++;
		let line: string;
		try {
			line = decoder.decode(bytes);
		} catch {
			throw new Error(`malformed UTF-8 input at line ${lineNumber}`);
		}
		if (!line.trim()) continue;

		let input: unknown;
		try {
			input = JSON.parse(line);
		} catch {
			throw new Error(`invalid counter input JSON at line ${lineNumber}`);
		}
		if (
			typeof input !== "object" ||
			input === null ||
			Array.isArray(input) ||
			!("id" in input) ||
			typeof (input as { id?: unknown }).id !== "string" ||
			!("text" in input) ||
			typeof (input as { text?: unknown }).text !== "string"
		) {
			throw new Error(`counter input requires string id and text at line ${lineNumber}`);
		}

		const item = input as CounterInput;
		io.write(`${JSON.stringify({ id: item.id, tokens: countTokens(item.text, encoding) })}\n`);
	}
}
