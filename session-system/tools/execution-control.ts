import { homedir } from "node:os";
import { join } from "node:path";
import {
	WorkClient,
	WorkError,
	type ExecutionSnapshot as ClientExecutionSnapshot,
	type ExecutionView,
	type Fetch,
	type UUID,
} from "@oh-my-pi/pi-work-client";
import type { ExecutionSnapshot, WorkflowBackend } from "../extensions/workflow/backend";
import { loadBearer, loadWorkConfig, type WorkClientConfig } from "../extensions/workflow/config";
import { createWorkBackend } from "../extensions/workflow/work";

export interface PublicControl {
	grantRef: string;
	state: "active" | "paused" | "stopped" | "completed" | "canceled";
	grantVersion: number;
	mode: "single" | "queue";
	activeWorkKey: string | null;
	pausedAt: string | null;
	stoppedAt: string | null;
	expiresAt: string | null;
	terminalReason: string | null;
	canPause: boolean;
	canStop: boolean;
}

export interface ControlDeps {
	loadConfig?: () => WorkClientConfig | null;
	loadToken?: (config: WorkClientConfig) => string | null;
	createBackend?: typeof createWorkBackend;
	fetchImpl?: Fetch;
	pendingDir?: string;
	stdout?: (msg: string) => void;
	stderr?: (msg: string) => void;
}

export async function buildPublicControl(
	exec: ExecutionSnapshot | ExecutionView,
	backend: WorkflowBackend,
): Promise<PublicControl> {
	const grant = exec.grant;
	const activeItem = ("activeItem" in exec ? exec.activeItem : (exec as ExecutionView).active_item) ?? null;
	let activeWorkKey: string | null = null;
	if (activeItem?.work_id) {
		try {
			const issue = await backend.findIssue(activeItem.work_id);
			activeWorkKey = issue?.key ?? null;
		} catch {
			activeWorkKey = null;
		}
	}
	const rawReason = grant.terminal_reason;
	const terminalReason = rawReason
		? rawReason.replace(/[\x00-\x1f\x7f-\x9f]/g, "").slice(0, 120)
		: null;

	return {
		grantRef: grant.grant_id.slice(0, 8),
		state: grant.state,
		grantVersion: grant.grant_version,
		mode: grant.mode,
		activeWorkKey,
		pausedAt: grant.paused_at ?? null,
		stoppedAt: grant.stopped_at ?? null,
		expiresAt: grant.expires_at ?? null,
		terminalReason,
		canPause: grant.state === "active",
		canStop: grant.state === "active" || grant.state === "paused",
	};
}

function mapWorkErrorCode(err: unknown): string {
	if (err instanceof WorkError) {
		if (err.code === "revision_conflict") return "stale_version";
		if (err.code === "execution_judge_drift") return "judge_drift";
		if (err.code === "contract_mismatch") return "contract_mismatch";
		if (err.code === "unauthenticated" || err.status === 401 || err.status === 403) return "unauthorized";
		if (err.code === "unavailable" || err.status === 0) return "unavailable";
		if (err.code === "invalid_request") {
			if (err.message.includes("cannot pause grant in state")) return "not_active";
			if (err.message.includes("not found") || err.message.includes("execution grant not found")) return "no_grant";
			return "invalid_request";
		}
		if (err.code === "not_found" || err.status === 404) return "no_grant";
		return err.code;
	}
	return "unavailable";
}

export async function runControl(
	argv: string[],
	deps: ControlDeps = {},
): Promise<{ exitCode: number; data?: unknown }> {
	const printOut = deps.stdout ?? ((msg: string) => process.stdout.write(`${msg}\n`));
	const printErr = deps.stderr ?? ((msg: string) => process.stderr.write(`${msg}\n`));

	let verb: "inspect" | "pause" | "stop" | undefined;
	let grantRef: string | undefined;
	let expectedVersion: number | undefined;
	let reason: string | undefined;

	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (!arg) continue;
		if (arg === "inspect" || arg === "pause" || arg === "stop") {
			if (verb && verb !== arg) {
				printErr("error: duplicate or conflicting verb specified");
				return { exitCode: 1 };
			}
			verb = arg;
		} else if (arg === "--json") {
			// json output is standard
		} else if (arg === "--grant-ref") {
			if (i + 1 >= argv.length) {
				printErr("error: missing argument for --grant-ref");
				return { exitCode: 1 };
			}
			grantRef = argv[++i];
		} else if (arg.startsWith("--grant-ref=")) {
			grantRef = arg.slice("--grant-ref=".length);
		} else if (arg === "--expected-version") {
			if (i + 1 >= argv.length) {
				printErr("error: missing argument for --expected-version");
				return { exitCode: 1 };
			}
			const rawVal = argv[++i];
			if (!/^\d+$/.test(rawVal)) {
				printErr("error: expected-version must be a non-negative integer");
				return { exitCode: 1 };
			}
			const parsed = Number.parseInt(rawVal, 10);
			if (!Number.isSafeInteger(parsed)) {
				printErr("error: expected-version out of range");
				return { exitCode: 1 };
			}
			expectedVersion = parsed;
		} else if (arg.startsWith("--expected-version=")) {
			const rawVal = arg.slice("--expected-version=".length);
			if (!/^\d+$/.test(rawVal)) {
				printErr("error: expected-version must be a non-negative integer");
				return { exitCode: 1 };
			}
			const parsed = Number.parseInt(rawVal, 10);
			if (!Number.isSafeInteger(parsed)) {
				printErr("error: expected-version out of range");
				return { exitCode: 1 };
			}
			expectedVersion = parsed;
		} else if (arg === "--reason") {
			if (i + 1 >= argv.length) {
				printErr("error: missing argument for --reason");
				return { exitCode: 1 };
			}
			reason = argv[++i];
		} else if (arg.startsWith("--reason=")) {
			reason = arg.slice("--reason=".length);
		} else {
			printErr("error: unrecognized or invalid argument");
			return { exitCode: 1 };
		}
	}

	if (!verb) {
		printErr("usage: omp-execution-control <inspect|pause|stop> --json [options]");
		return { exitCode: 1 };
	}

	if (grantRef !== undefined && !/^[0-9a-f]{8}$/i.test(grantRef)) {
		printErr("error: grant-ref must be 8 hex characters");
		return { exitCode: 1 };
	}

	if (verb === "pause") {
		if (grantRef === undefined || expectedVersion === undefined) {
			printErr("error: pause requires --grant-ref and --expected-version");
			return { exitCode: 1 };
		}
	} else if (verb === "stop") {
		if (grantRef === undefined || expectedVersion === undefined || reason === undefined) {
			printErr("error: stop requires --grant-ref, --expected-version, and --reason");
			return { exitCode: 1 };
		}
	}

	let config: WorkClientConfig | null;
	let token: string | null;
	try {
		config = (deps.loadConfig ?? loadWorkConfig)();
		if (!config) {
			printErr("WorkService config missing or unavailable");
			return { exitCode: 2 };
		}
		token = (deps.loadToken ?? loadBearer)(config);
		if (!token) {
			printErr("Bearer token missing or unreadable");
			return { exitCode: 2 };
		}
	} catch (err) {
		printErr(String(err));
		return { exitCode: 2 };
	}

	const client = new WorkClient(config.baseUrl, config.workspaceId as UUID, () => token, deps.fetchImpl);
	const backend = (deps.createBackend ?? createWorkBackend)(config, () => token, deps.fetchImpl, deps.pendingDir);

	// 1. Read current execution view via typed raw client
	let execView: ExecutionView | null = null;
	try {
		execView = await client.execution("");
	} catch (err) {
		const code = mapWorkErrorCode(err);
		const out = { ok: false, code };
		printOut(JSON.stringify(out));
		return { exitCode: 0, data: out };
	}

	if (!execView || !execView.grant) {
		const out = { ok: false, code: "no_grant" };
		printOut(JSON.stringify(out));
		return { exitCode: 0, data: out };
	}

	const currentControl = await buildPublicControl(execView, backend);

	if (verb === "inspect") {
		const out = { ok: true, control: currentControl };
		printOut(JSON.stringify(out));
		return { exitCode: 0, data: out };
	}

	// 2. Fence validation
	if (!grantRef || grantRef !== currentControl.grantRef) {
		const out = { ok: false, code: "grant_mismatch", control: currentControl };
		printOut(JSON.stringify(out));
		return { exitCode: 0, data: out };
	}

	if (expectedVersion === undefined || Number.isNaN(expectedVersion) || currentControl.grantVersion !== expectedVersion) {
		const out = { ok: false, code: "stale_version", control: currentControl };
		printOut(JSON.stringify(out));
		return { exitCode: 0, data: out };
	}

	// 3. State transition pre-checks
	if (verb === "pause") {
		if (currentControl.state !== "active") {
			const out = { ok: false, code: "not_active", control: currentControl };
			printOut(JSON.stringify(out));
			return { exitCode: 0, data: out };
		}

		try {
			const updated = await backend.setExecutionState({
				grantId: execView.grant.grant_id,
				expectedGrantVersion: expectedVersion,
				targetState: "paused",
				reason: "webui_pause",
				judgeSha256: execView.grant.judge_sha256,
			});
			const newControl = await buildPublicControl(updated, backend);
			const out = { ok: true, control: newControl };
			printOut(JSON.stringify(out));
			return { exitCode: 0, data: out };
		} catch (err) {
			const code = mapWorkErrorCode(err);
			let freshControl: PublicControl | undefined;
			try {
				const fresh = await client.execution("");
				freshControl = await buildPublicControl(fresh, backend);
			} catch {}
			const out = { ok: false, code, ...(freshControl ? { control: freshControl } : {}) };
			printOut(JSON.stringify(out));
			return { exitCode: 0, data: out };
		}
	}

	if (verb === "stop") {
		if (!currentControl.canStop) {
			const out = { ok: false, code: "already_terminal", control: currentControl };
			printOut(JSON.stringify(out));
			return { exitCode: 0, data: out };
		}

		if (!reason || !reason.trim()) {
			const out = { ok: false, code: "invalid_reason", control: currentControl };
			printOut(JSON.stringify(out));
			return { exitCode: 0, data: out };
		}

		const cleanReason = reason.replace(/[\x00-\x1f\x7f-\x9f]/g, "").trim().slice(0, 200);
		if (!cleanReason) {
			const out = { ok: false, code: "invalid_reason", control: currentControl };
			printOut(JSON.stringify(out));
			return { exitCode: 0, data: out };
		}
		const stopReason = cleanReason.startsWith("webui_stop: ") ? cleanReason : `webui_stop: ${cleanReason}`;

		try {
			const updated = await backend.setExecutionState({
				grantId: execView.grant.grant_id,
				expectedGrantVersion: expectedVersion,
				targetState: "stopped",
				reason: stopReason,
				judgeSha256: execView.grant.judge_sha256,
			});
			const newControl = await buildPublicControl(updated, backend);
			const out = { ok: true, control: newControl };
			printOut(JSON.stringify(out));
			return { exitCode: 0, data: out };
		} catch (err) {
			const code = mapWorkErrorCode(err);
			let freshControl: PublicControl | undefined;
			try {
				const fresh = await client.execution("");
				freshControl = await buildPublicControl(fresh, backend);
			} catch {}
			const out = { ok: false, code, ...(freshControl ? { control: freshControl } : {}) };
			printOut(JSON.stringify(out));
			return { exitCode: 0, data: out };
		}
	}

	return { exitCode: 1 };
}

if (import.meta.main) {
	const { exitCode } = await runControl(process.argv.slice(2));
	process.exit(exitCode);
}
