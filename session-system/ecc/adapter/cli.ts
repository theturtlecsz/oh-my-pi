/**
 * Non-interactive ECC install lifecycle.
 *
 * Usage: bun session-system/ecc/adapter/cli.ts <preview|install|update|remove> --project <dir> [--ecc-root <dir>]
 *
 * Calls apply.ts and prints one JSON object. Never prompts. InstallError and
 * usage errors print {"error":"..."} and exit 1.
 */
import * as path from "node:path";
import { buildPlan, install, previewPlan, remove, update } from "./apply";
import { ECC_ROOT, installablePackedAssets, loadManifest } from "./catalog";
import { readLock } from "./lock";

const COMMANDS = ["preview", "install", "update", "remove"] as const;
type Command = (typeof COMMANDS)[number];

interface CliArgs {
	command: Command;
	project: string;
	eccRoot: string;
}

function isCommand(value: string): value is Command {
	return (COMMANDS as readonly string[]).includes(value);
}

function takeOption(argv: string[], index: number, name: string): { value: string; next: number } {
	const arg = argv[index] ?? "";
	const prefix = `${name}=`;
	if (arg.startsWith(prefix)) {
		const value = arg.slice(prefix.length);
		if (value.length === 0) throw new Error(`${name} requires a value`);
		return { value, next: index };
	}
	const value = argv[index + 1];
	if (!value || value.startsWith("-")) throw new Error(`${name} requires a value`);
	return { value, next: index + 1 };
}

export function parseEccCliArgs(argv: string[]): CliArgs {
	let command: string | undefined;
	let project: string | undefined;
	let eccRoot = ECC_ROOT;
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i] ?? "";
		if (arg === "--project" || arg.startsWith("--project=")) {
			const taken = takeOption(argv, i, "--project");
			project = taken.value;
			i = taken.next;
			continue;
		}
		if (arg === "--ecc-root" || arg.startsWith("--ecc-root=")) {
			const taken = takeOption(argv, i, "--ecc-root");
			eccRoot = taken.value;
			i = taken.next;
			continue;
		}
		if (arg.startsWith("-")) throw new Error(`unknown argument ${arg}`);
		if (command) throw new Error(`unexpected argument ${arg}`);
		command = arg;
	}
	if (!command || !isCommand(command)) {
		throw new Error(
			"usage: cli.ts <preview|install|update|remove> --project <dir> [--ecc-root <dir>]",
		);
	}
	if (!project) throw new Error("--project requires a directory");
	return { command, project: path.resolve(project), eccRoot: path.resolve(eccRoot) };
}

/** Run one lifecycle command against apply.ts. Does not read stdin. */
export async function runEccCli(args: CliArgs): Promise<unknown> {
	const options = { eccRoot: args.eccRoot, projectRoot: args.project };
	if (args.command === "remove") return remove(options);
	const manifest = await loadManifest(args.eccRoot);
	const assets = installablePackedAssets(manifest);
	if (args.command === "preview") {
		const plan = await buildPlan(options, manifest, assets);
		const lock = await readLock(path.join(args.project, ".omp", "ecc", "adapted.lock.json"));
		return previewPlan(options, plan, lock);
	}
	if (args.command === "update") return update(options, manifest, assets);
	return install(options, manifest, assets);
}

async function main(): Promise<void> {
	try {
		const result = await runEccCli(parseEccCliArgs(process.argv.slice(2)));
		process.stdout.write(`${JSON.stringify(result)}\n`);
	} catch (err) {
		const message = err instanceof Error ? err.message : String(err);
		process.stdout.write(`${JSON.stringify({ error: message })}\n`);
		process.exitCode = 1;
	}
}

if (import.meta.main) {
	await main();
}
