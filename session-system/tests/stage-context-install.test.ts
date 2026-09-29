import { afterEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { CloseAttempt, EvidenceReceipt, WorkflowView } from "@oh-my-pi/pi-work-client";
import { defaultRun, stageContextLines } from "../extensions/workflow/stage-context";

const repoRoot = path.resolve(import.meta.dir, "..", "..");
const installSh = path.join(import.meta.dir, "..", "install.sh");
const venvDir = path.join(repoRoot, "python", "omp-knowledge", ".venv");
const skip = Bun.which("uv") == null || !fs.existsSync(venvDir);

const WORK_ID = "11111111-1111-4111-8111-111111111111";
const WORKSPACE_ID = "22222222-2222-4222-8222-222222222222";
const REVISION_ID = "33333333-3333-4333-8333-333333333333";
const CANDIDATE_ID = "44444444-4444-4444-8444-444444444444";
const PROJECT_ID = "55555555-5555-4555-8555-555555555555";
const RECEIPT_ID = "66666666-6666-4666-8666-666666666666";
const ATTEMPT_ID = "77777777-7777-4777-8777-777777777777";
const SHA_A = "a".repeat(64);
const SHA_B = "b".repeat(64);
const EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const ISSUED_AT = "2026-09-26T00:00:00Z";
const HEADER = /^STAGE CONTEXT (\S+) bundle=(\S+) sha256=([0-9a-f]{64})\b/;

const tempHomes: string[] = [];

afterEach(() => {
	for (const dir of tempHomes.splice(0)) {
		fs.rmSync(dir, { recursive: true, force: true });
	}
});

/** Install and stage env: no XDG_CONFIG_HOME or OMP_KNOWLEDGE_* except the state dir.
 *  PATH and the real cache dirs stay so uv and bun do not download. */
function launchEnv(home: string, stateDir: string): Record<string, string> {
	const env: Record<string, string> = {};
	for (const [key, value] of Object.entries(process.env)) {
		if (value === undefined) continue;
		if (key === "XDG_CONFIG_HOME" || key.startsWith("OMP_KNOWLEDGE_")) continue;
		env[key] = value;
	}
	env.HOME = home;
	env.OMP_KNOWLEDGE_STATE_DIR = stateDir;
	return env;
}

function workflowView(receipts: EvidenceReceipt[]): WorkflowView {
	return {
		item: {
			work_id: WORK_ID,
			workspace_id: WORKSPACE_ID,
			alias: { work_id: WORK_ID, key: "OMP-419", primary: true, origin: "local" },
			state: "in_progress",
			revision: {
				revision_id: REVISION_ID,
				work_id: WORK_ID,
				revision_number: 1,
				title: "Ship the context bundle",
				description: "compile the fleet context",
				scope: "python/omp-knowledge",
				acceptance_criteria: ["title is present", "budget holds"],
				content_sha256: SHA_A,
				created_by: "tester",
				created_at: ISSUED_AT,
			},
			candidate: {
				candidate_id: CANDIDATE_ID,
				work_id: WORK_ID,
				revision_id: REVISION_ID,
				candidate_sha256: SHA_B,
				commit_sha: null,
				kind: "planned",
				allocated_at: ISSUED_AT,
			},
			project_id: PROJECT_ID,
			archived: false,
		},
		relations: [],
		receipts,
		close_attempts: [],
		audit_manifest: null,
		auditor_launches: [],
		close_attempt_events: [],
		checkpoint_deliveries: [],
		project: null,
	};
}

function planReceipt(): EvidenceReceipt {
	return {
		receipt_id: RECEIPT_ID,
		work_id: WORK_ID,
		revision_id: REVISION_ID,
		candidate_id: CANDIDATE_ID,
		kind: "plan",
		payload: {},
		payload_sha256: EMPTY_SHA,
		issuer: "tester",
		issued_at: ISSUED_AT,
	};
}

function auditAttempt(): CloseAttempt {
	return { attempt_id: ATTEMPT_ID } as CloseAttempt;
}

describe("fresh install stage context", () => {
	test.skipIf(skip)(
		"persists a plan, implement, and audit bundle and reports not provisioned once settings are removed",
		async () => {
			const home = fs.mkdtempSync(path.join(os.tmpdir(), "omp-stage-context-install-"));
			tempHomes.push(home);
			const stateDir = path.join(home, "state");
			const env = launchEnv(home, stateDir);

			const installed = Bun.spawnSync(["bash", installSh], {
				env,
				stdout: "pipe",
				stderr: "pipe",
			});
			expect(installed.exitCode, installed.stderr.toString()).toBe(0);
			const configPath = path.join(home, ".config", "omp-knowledge", "context.json");
			expect(fs.existsSync(configPath)).toBe(true);

			const planView = workflowView([]);
			const implementView = workflowView([planReceipt()]);
			const cases = [
				{ stage: "plan", view: planView, liveAttempt: () => undefined },
				{ stage: "implement", view: implementView, liveAttempt: () => undefined },
				{ stage: "audit", view: implementView, liveAttempt: () => auditAttempt() },
			] as const;

			const headers: { stage: string; bundle_id: string; bundle_sha256: string }[] = [];
			for (const item of cases) {
				const lines = await stageContextLines(
					{ key: "OMP-419", cwd: repoRoot },
					{ workflow: async () => item.view, liveAttempt: item.liveAttempt, env, run: defaultRun },
				);
				const match = lines[0]?.match(HEADER);
				expect(match, lines.join("\n")).not.toBeNull();
				expect(match?.[1]).toBe(item.stage);
				const text = lines.slice(1).join("\n");
				expect(text).toContain("adr#docs/adr/");
				expect(text).toContain("repository#");
				expect(text).not.toContain("lesson#");
				headers.push({
					stage: match?.[1] ?? "",
					bundle_id: match?.[2] ?? "",
					bundle_sha256: match?.[3] ?? "",
				});
			}

			const db = new Database(path.join(stateDir, "context-bundles.sqlite"), { readonly: true });
			try {
				const rows = db
					.query("SELECT stage, bundle_id, bundle_sha256 FROM context_bundles")
					.all() as { stage: string; bundle_id: string; bundle_sha256: string }[];
				expect(rows).toHaveLength(3);
				const sortKey = (row: { stage: string; bundle_id: string; bundle_sha256: string }) =>
					`${row.stage}\0${row.bundle_id}\0${row.bundle_sha256}`;
				expect([...rows].sort((a, b) => sortKey(a).localeCompare(sortKey(b)))).toEqual(
					[...headers].sort((a, b) => sortKey(a).localeCompare(sortKey(b))),
				);
			} finally {
				db.close();
			}

			fs.rmSync(configPath);
			const missing = await stageContextLines(
				{ key: "OMP-419", cwd: repoRoot },
				{ workflow: async () => planView, liveAttempt: () => undefined, env, run: defaultRun },
			);
			expect(missing).toEqual(["STAGE CONTEXT: unavailable (not provisioned)"]);
		},
		{ timeout: 180_000 },
	);
});
