/**
 * workflow/config.ts — Work Ledger client configuration (HOME-147).
 *
 * Config:  ~/.config/omp-work/client.json   (0600) — { base_url, workspace_id, owner_id, bearer_file? }
 * Bearer:  $OMP_WORK_BEARER, else the mode-0600 JSON capability named by bearer_file; its token field is returned.
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

/** Shared XDG-aware config root (client.json, pending ops). */
export function ompWorkConfigDir(): string {
	return join(process.env.XDG_CONFIG_HOME || join(homedir(), ".config"), "omp-work");
}

function configPath(): string {
	return join(ompWorkConfigDir(), "client.json");
}

/** Durable pending-operation journal directory (workflow/pending-ops.ts). */
export function pendingOpsDir(): string {
	return join(ompWorkConfigDir(), "pending-operations");
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

/** Compute the prospective contract SHA256 from the contract files on disk. */
export function computeContractSha256FromDisk(contractDir: string): string {
	const manifestPath = join(contractDir, "manifest.json");
	try {
		const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as { paths: string[] };
		const hasher = new Bun.CryptoHasher("sha256");
		for (const relPath of manifest.paths) {
			const filePath = join(contractDir, relPath);
			const fileBytes = readFileSync(filePath);
			const fileSha = new Bun.CryptoHasher("sha256").update(fileBytes).digest("hex");
			hasher.update(relPath);
			hasher.update("\0");
			hasher.update(fileSha);
			hasher.update("\n");
		}
		return hasher.digest("hex");
	} catch {
		return "";
	}
}

/** Check if prospective Work contract changes in the working tree match owner approval in approval.json. */
export function checkProspectiveContract(cwd: string): { prospectiveDigest: string; approvedDigest: string; approved: boolean } {
	const contractDir = join(cwd, "python/omp-work/src/omp_work/contracts/v1");
	const prospectiveDigest = computeContractSha256FromDisk(contractDir);
	const approvalPath = join(contractDir, "approval.json");
	try {
		const approval = JSON.parse(readFileSync(approvalPath, "utf8")) as { contract_sha256?: string };
		const approvedDigest = approval?.contract_sha256 ?? "";
		return {
			prospectiveDigest,
			approvedDigest,
			approved: Boolean(prospectiveDigest && approvedDigest && prospectiveDigest === approvedDigest),
		};
	} catch {
		return { prospectiveDigest, approvedDigest: "", approved: false };
	}
}
