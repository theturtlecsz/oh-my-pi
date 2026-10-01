import * as fs from "node:fs";

export interface ResolveRunTimeoutSource {
	argv: string[];
	readCmdline(pid: "self" | number): string[] | undefined;
	ppid: number;
}

const BUN_DEFAULT_TIMEOUT_MS = 5000;

function parseTimeoutValue(raw: string): number | undefined {
	if (!/^\d+$/.test(raw)) {
		return undefined;
	}
	const parsed = Number.parseInt(raw, 10);
	return Number.isFinite(parsed) ? parsed : undefined;
}

function extractTimeout(args: readonly string[]): number | undefined {
	for (let i = 0; i < args.length; i++) {
		const arg = args[i];
		if (!arg) continue;
		if (arg.startsWith("--timeout=")) {
			const parsed = parseTimeoutValue(arg.slice("--timeout=".length));
			if (parsed !== undefined) {
				return parsed;
			}
		} else if (arg === "--timeout" && i + 1 < args.length) {
			const nextArg = args[i + 1];
			if (nextArg) {
				const parsed = parseTimeoutValue(nextArg);
				if (parsed !== undefined) {
					return parsed;
				}
			}
		}
	}
	return undefined;
}

export function defaultReadCmdline(pid: "self" | number): string[] | undefined {
	if (typeof pid === "number" && (!Number.isFinite(pid) || pid <= 0)) {
		return undefined;
	}
	const procPath = pid === "self" ? "/proc/self/cmdline" : `/proc/${pid}/cmdline`;
	try {
		const raw = fs.readFileSync(procPath, "utf8");
		const args = raw.split("\0");
		while (args.length > 0 && args[args.length - 1] === "") {
			args.pop();
		}
		return args;
	} catch {
		return undefined;
	}
}

export function resolveRunTimeoutMs(src?: Partial<ResolveRunTimeoutSource>): number | undefined {
	const argv = src?.argv ?? process.argv;
	const readCmdline = src?.readCmdline ?? defaultReadCmdline;
	const ppid = src?.ppid ?? process.ppid;

	const fromArgv = extractTimeout(argv);
	if (fromArgv !== undefined) {
		return fromArgv;
	}

	const selfCmdline = readCmdline("self");
	if (selfCmdline !== undefined) {
		const fromSelf = extractTimeout(selfCmdline);
		if (fromSelf !== undefined) {
			return fromSelf;
		}
	}

	const parentCmdline = readCmdline(ppid);
	if (parentCmdline !== undefined) {
		const fromParent = extractTimeout(parentCmdline);
		if (fromParent !== undefined) {
			return fromParent;
		}
	}

	if (selfCmdline !== undefined || parentCmdline !== undefined) {
		return BUN_DEFAULT_TIMEOUT_MS;
	}

	return undefined;
}
