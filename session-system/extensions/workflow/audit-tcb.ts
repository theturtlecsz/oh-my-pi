/**
 * workflow/audit-tcb.ts — Audit Judge Trusted Computing Base (TCB) Sealing (OMP-180).
 *
 * Seals the exact audit judge identity: auditor agent definition, workflow host,
 * adapter, freeze policy, runner sources, executor transport, contract digest,
 * and service runtime fingerprint into an immutable judge manifest and judge_sha256.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { discoverAgents, getAgent } from "@oh-my-pi/pi-coding-agent/task";
import {
	canonicalJson,
	type ExecutionJudgeManifestV2,
	sha256Hex,
	WORK_CONTRACT_SHA256,
	type WorkClient,
} from "@oh-my-pi/pi-work-client";
import { resolveAuditPolicy, type AuditPolicy, type AuditRoute } from "./audit-policy";

const workflowDir = import.meta.dir;
const hostBytes = readFileSync(join(workflowDir, "host.ts"));
const hostSha256 = new Bun.CryptoHasher("sha256").update(hostBytes).digest("hex");

const adapterBytes = readFileSync(join(workflowDir, "work.ts"));
const adapterSha256 = new Bun.CryptoHasher("sha256").update(adapterBytes).digest("hex");

const freezeBytes = readFileSync(join(workflowDir, "git.ts"));
const freezeSha256 = new Bun.CryptoHasher("sha256").update(freezeBytes).digest("hex");

const runnerBytes = readFileSync(join(workflowDir, "auditor-runner.ts"));
const runnerSha256 = new Bun.CryptoHasher("sha256").update(runnerBytes).digest("hex");

export type SourceResolver = (specifier: string) => string | undefined;

const nativeStageSourceFiles = ["audit-policy.ts", "native-stage-profile.ts", "native-stage-dispatch.ts"] as const;

export function getNativeStageSha(): string {
	const hasher = new Bun.CryptoHasher("sha256");
	for (const file of nativeStageSourceFiles) {
		const bytes = readFileSync(join(workflowDir, file));
		hasher.update(`file:${file}\n`).update(bytes);
	}
	return hasher.digest("hex");
}

export function getExecutorSha(sourceResolver?: SourceResolver): string {
	const hasher = new Bun.CryptoHasher("sha256");
	const requiredSpecifiers = [
		"@oh-my-pi/pi-coding-agent/task/executor",
		"@oh-my-pi/pi-coding-agent/task/yield-assembly",
		"@oh-my-pi/pi-coding-agent/sdk",
	];
	for (const specifier of requiredSpecifiers) {
		const resolver = sourceResolver ?? ((s: string) => import.meta.resolve(s));
		const resolved = resolver(specifier);
		if (!resolved) {
			throw new Error(`Failed to resolve required audit transport source: ${specifier}`);
		}
		const filePath = resolved.startsWith("file://") ? resolved.slice(7) : resolved;
		const bytes = readFileSync(filePath);
		hasher.update(`file:${specifier}\n`).update(bytes);
	}
	return hasher.digest("hex");
}

export async function computeAuditTcb(
	ctx: ExtensionContext,
	workClient: WorkClient,
	sourceResolver?: SourceResolver,
): Promise<{ judgeSha256: string; judgeManifest: ExecutionJudgeManifestV2; auditPolicy: AuditPolicy; auditRoute: AuditRoute }> {
	const discovery = await discoverAgents(ctx.cwd);
	const agent = getAgent(discovery.agents, "auditor");
	if (!agent) {
		throw new Error('Installed "auditor" agent definition not found');
	}
	if (!agent.output) {
		throw new Error('Installed "auditor" agent definition is missing required output schema');
	}
	const auditorAgentSha256 = sha256Hex(canonicalJson(agent));

	const health = await workClient.healthReady();
	if (!health.service_fingerprint) {
		throw new Error("WorkService health report is missing required service_fingerprint");
	}
	const serviceFingerprint = health.service_fingerprint;

	const { policy, policySha256, route } = resolveAuditPolicy(ctx.models);
	const nativeStageSha256 = getNativeStageSha();

	const judgeManifest: ExecutionJudgeManifestV2 = {
		manifest_version: 2,
		auditor_agent_sha256: auditorAgentSha256,
		host_sha256: hostSha256,
		adapter_sha256: adapterSha256,
		freeze_sha256: freezeSha256,
		runner_sha256: runnerSha256,
		executor_sha256: getExecutorSha(sourceResolver),
		contract_sha256: WORK_CONTRACT_SHA256,
		service_fingerprint: serviceFingerprint,
		service_code_fingerprint: serviceFingerprint,
		service_migration_sha256: serviceFingerprint,
		audit_policy_sha256: policySha256,
		native_stage_sha256: nativeStageSha256,
	};

	const judgeSha256 = sha256Hex(canonicalJson(judgeManifest));
	return { judgeSha256, judgeManifest, auditPolicy: policy, auditRoute: route };
}
