import { Args, Command, Flags } from "@oh-my-pi/pi-utils/cli";
import { tokensHelp as commandHelp } from "../cli/command-help";
import { runTokenCounter } from "../cli/tokens-cli";

export default class Tokens extends Command {
	static description = commandHelp.description;
	static args = {
		action: Args.string({ description: "Token operation", required: true, options: ["count"] }),
	};
	static flags = {
		encoding: Flags.string({ description: "Native encoding name", required: true }),
	};

	async run(): Promise<void> {
		try {
			const { args, flags } = await this.parse(Tokens);
			if (args.action !== "count") {
				throw new Error(`unsupported token operation: ${args.action}`);
			}
			if (!flags.encoding) {
				throw new Error("missing required flag: --encoding");
			}
			await runTokenCounter(
				{ encoding: flags.encoding },
				{
					input: Bun.stdin.stream(),
					write: (line: string) => {
						process.stdout.write(line.endsWith("\n") ? line : `${line}\n`);
					},
				},
			);
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			process.stderr.write(`${message}\n`);
			process.exitCode = 2;
		}
	}
}
