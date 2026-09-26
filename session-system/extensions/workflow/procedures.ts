import { spawnSync } from "node:child_process";

export interface ProcedureInput {
	workKey: string;
	projectId?: string | null;
	cwd: string;
}

export interface RunResult {
	status?: number | null;
	exitCode?: number | null;
	stdout?: string | Buffer | null;
	stderr?: string | Buffer | null;
	error?: Error | null;
	timedOut?: boolean;
}

export type ProcedureRunner = (
	command: string[],
	options: { timeout: number },
) => Promise<RunResult> | RunResult;

export interface ProcedureDeps {
	env?: Record<string, string | undefined>;
	run?: ProcedureRunner;
}

export const spawnRunner: ProcedureRunner = (command, options) => {
	if (command.length === 0) {
		return { exitCode: 1, error: new Error("Empty command") };
	}
	const res = spawnSync(command[0]!, command.slice(1), {
		encoding: "utf8",
		timeout: options.timeout,
	});
	const timedOut = Boolean(
		(res.error && "code" in res.error && (res.error as { code: string }).code === "ETIMEDOUT") ||
		res.signal === "SIGTERM" ||
		res.signal === "SIGKILL",
	);
	return {
		status: res.status,
		exitCode: res.status,
		stdout: res.stdout,
		stderr: res.stderr,
		error: res.error,
		timedOut,
	};
};

const DEFAULT_TIMEOUT_MS = 5000;
const ENV_VAR_NAME = "OMP_KNOWLEDGE_LEARNING_CMD";
const MODULE_TOKEN = "omp_knowledge.learning";
const SUPPLY_FLAGS = new Set(["--state-dir", "--workspace", "--work-key", "--project-id", "--cwd", "--limit", "--json"]);

// The learning CLI is `python -m omp_knowledge.learning <subcommand> ...`; argparse only accepts
// `--state-dir`/`--workspace` on the `supply` subparser, so the subcommand must be spliced in ahead
// of whichever flags the operator folded into OMP_KNOWLEDGE_LEARNING_CMD — not appended after them.
function buildSupplyCommand(baseCmd: string[], input: ProcedureInput): string[] {
	const args = ["--work-key", input.workKey];
	if (input.projectId && input.projectId.trim() !== "") {
		args.push("--project-id", input.projectId);
	}
	args.push("--cwd", input.cwd, "--json");

	const existingSupply = baseCmd.indexOf("supply");
	if (existingSupply >= 0) {
		return [...baseCmd.slice(0, existingSupply + 1), ...args, ...baseCmd.slice(existingSupply + 1)];
	}

	const moduleIndex = baseCmd.findIndex(token => token === MODULE_TOKEN || token.endsWith(`/${MODULE_TOKEN}`));
	let insertAt = moduleIndex >= 0 ? moduleIndex + 1 : baseCmd.length;
	if (moduleIndex < 0) {
		const flagIndex = baseCmd.findIndex(token => SUPPLY_FLAGS.has(token));
		if (flagIndex >= 0) {
			insertAt = flagIndex;
		}
	}
	return [...baseCmd.slice(0, insertAt), "supply", ...args, ...baseCmd.slice(insertAt)];
}

export async function procedureDigestLines(
	input: ProcedureInput,
	deps?: ProcedureDeps,
): Promise<string[]> {
	try {
		const env = deps?.env ?? process.env;
		const rawCmd = env[ENV_VAR_NAME];
		if (!rawCmd || rawCmd.trim() === "") {
			return [];
		}

		let baseCmd: unknown;
		try {
			baseCmd = JSON.parse(rawCmd);
		} catch {
			return ["PROCEDURES: unavailable (bad JSON)"];
		}

		if (!Array.isArray(baseCmd) || baseCmd.length === 0 || !baseCmd.every(item => typeof item === "string")) {
			return ["PROCEDURES: unavailable (invalid OMP_KNOWLEDGE_LEARNING_CMD)"];
		}

		const fullCommand = buildSupplyCommand(baseCmd, input);

		const runner = deps?.run ?? spawnRunner;

		const { promise: timeoutPromise, reject } = Promise.withResolvers<never>();
		const timer = setTimeout(() => {
			reject(new Error("timeout"));
		}, DEFAULT_TIMEOUT_MS);

		let result: RunResult;
		try {
			result = await Promise.race([
				Promise.resolve(runner(fullCommand, { timeout: DEFAULT_TIMEOUT_MS })),
				timeoutPromise,
			]);
		} catch (error: unknown) {
			const msg = error instanceof Error ? error.message : String(error);
			const isTimeout = msg.toLowerCase().includes("timeout");
			return [`PROCEDURES: unavailable (${isTimeout ? "timeout" : msg})`];
		} finally {
			clearTimeout(timer);
		}

		if (result.timedOut) {
			return ["PROCEDURES: unavailable (timeout)"];
		}

		if (result.error) {
			const errCode = "code" in result.error ? (result.error as { code: string }).code : "";
			if (errCode === "ETIMEDOUT" || result.error.message.toLowerCase().includes("timeout")) {
				return ["PROCEDURES: unavailable (timeout)"];
			}
			return [`PROCEDURES: unavailable (${result.error.message})`];
		}

		const code = result.exitCode ?? result.status ?? 0;
		if (code !== 0) {
			return [`PROCEDURES: unavailable (exit ${code})`];
		}

		const stdoutStr = typeof result.stdout === "string"
			? result.stdout
			: (result.stdout ? result.stdout.toString("utf8") : "");

		let parsed: unknown;
		try {
			parsed = JSON.parse(stdoutStr);
		} catch {
			return ["PROCEDURES: unavailable (bad JSON)"];
		}

		if (!Array.isArray(parsed)) {
			return ["PROCEDURES: unavailable (bad JSON)"];
		}

		return parsed.map(String);
	} catch (error: unknown) {
		const msg = error instanceof Error ? error.message : String(error);
		return [`PROCEDURES: unavailable (${msg})`];
	}
}
