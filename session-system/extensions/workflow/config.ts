/**
 * workflow/config.ts — Work Ledger client configuration (HOME-147).
 *
 * Config:  ~/.config/omp-work/client.json   (0600) — { base_url, workspace_id, owner_id, bearer_file? }
 * Bearer:  $OMP_WORK_BEARER, else the file named by bearer_file (0600, single line).
 * Loopback only: any non-loopback base_url is refused — the backend never
 * crosses the network boundary.
 */
import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export interface WorkClientConfig {
	baseUrl: string;
	workspaceId: string;
	ownerId: string;
	bearerFile?: string;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "::1", "localhost", "[::1]"]);

export function isLoopback(url: string): boolean {
	try {
		return LOOPBACK_HOSTS.has(new URL(url).hostname);
	} catch {
		return false;
	}
}

function configPath(): string {
	const xdg = process.env.XDG_CONFIG_HOME;
	return join(xdg || join(homedir(), ".config"), "omp-work", "client.json");
}

/** Durable pending-operation journal directory (workflow/pending-ops.ts). */
export function pendingOpsDir(): string {
	const xdg = process.env.XDG_CONFIG_HOME;
	return join(xdg || join(homedir(), ".config"), "omp-work", "pending-operations");
}

/** null = not configured (extension stays dormant); throws on malformed config. */
export function loadWorkConfig(): WorkClientConfig | null {
	let raw: string;
	try {
		raw = readFileSync(configPath(), "utf8");
	} catch {
		return null;
	}
	const parsed = JSON.parse(raw) as Record<string, unknown>;
	const baseUrl = typeof parsed.base_url === "string" ? parsed.base_url.replace(/\/+$/, "") : "";
	const workspaceId = typeof parsed.workspace_id === "string" ? parsed.workspace_id : "";
	const ownerId = typeof parsed.owner_id === "string" ? parsed.owner_id : "";
	if (!baseUrl || !UUID_RE.test(workspaceId) || !UUID_RE.test(ownerId)) {
		throw new Error(`${configPath()}: needs base_url, workspace_id, owner_id (UUIDs)`);
	}
	if (!isLoopback(baseUrl)) {
		throw new Error(`${configPath()}: base_url ${baseUrl} is not loopback — the Work Ledger client refuses non-loopback endpoints`);
	}
	const bearerFile = typeof parsed.bearer_file === "string" ? parsed.bearer_file : undefined;
	return { baseUrl, workspaceId, ownerId, bearerFile };
}

/** null = no bearer available. Env wins (raw token); the file is the capability
 *  JSON written by `omp_work ops capabilities` — 0600, token field. */
export function loadBearer(config: WorkClientConfig): string | null {
	const env = process.env.OMP_WORK_BEARER?.trim();
	if (env) return env;
	if (!config.bearerFile) return null;
	try {
		if ((statSync(config.bearerFile).mode & 0o777) !== 0o600) return null;
		const parsed = JSON.parse(readFileSync(config.bearerFile, "utf8")) as Record<string, unknown>;
		return typeof parsed.token === "string" && parsed.token ? parsed.token : null;
	} catch {
		return null;
	}
}
