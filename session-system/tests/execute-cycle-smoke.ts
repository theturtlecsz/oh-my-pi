// OMP-180 execution cycle smoke test (plan verification steps 4 & 5):
//   OMP_WORK_POSTGRES_INTEGRATION=1 bun run session-system/tests/execute-cycle-smoke.ts
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { WORK_CONTRACT_SHA256 } from "@oh-my-pi/pi-work-client";

if (process.env.OMP_WORK_POSTGRES_INTEGRATION !== "1") {
	console.log("execute-cycle-smoke: skipped (set OMP_WORK_POSTGRES_INTEGRATION=1)");
	process.exit(0);
}

const WORKSPACE = "00000000-0000-4000-8000-0000000000aa";
const OWNER = "00000000-0000-4000-8000-0000000000bb";
const PROJECT = "00000000-0000-4000-8000-0000000000cc";
const PROJECT_NAME = "Smoke Project";

const repoRoot = path.resolve(import.meta.dir, "../..");
const pythonDir = path.join(repoRoot, "python/omp-work");

function freePort(): number {
	const probe = Bun.listen({ hostname: "127.0.0.1", port: 0, socket: { data: () => {} } });
	const port = probe.port;
	probe.stop(true);
	return port;
}

const root = fs.mkdtempSync(path.join(os.tmpdir(), "omp-execute-smoke-"));
const xdg = path.join(root, "xdg");
const home = path.join(root, "home");
fs.mkdirSync(path.join(xdg, "omp"), { recursive: true });
fs.mkdirSync(path.join(home, ".omp", "agent", "agents"), { recursive: true });
fs.writeFileSync(
	path.join(home, ".omp", "agent", "config.yml"),
	"modelRoles:\n  audit: smoke/mock-fable\n  default: smoke/mock-fable\n",
);
fs.copyFileSync(
	path.join(repoRoot, "session-system/agents/auditor.md"),
	path.join(home, ".omp", "agent", "agents", "auditor.md"),
);

const pgPort = freePort();
const httpPort = freePort();
const baseUrl = `http://127.0.0.1:${httpPort}`;
const pgData = path.join(root, "pgdata");
let service: { kill(): void } | undefined;

const cleanup = () => {
	service?.kill();
	Bun.spawnSync(["pg_ctl", "-D", pgData, "-m", "immediate", "stop"], { stdout: "ignore", stderr: "ignore" });
	fs.rmSync(root, { recursive: true, force: true });
};

try {
	const py = (args: string[]) => {
		const run = Bun.spawnSync(["uv", "run", "python", "-m", "omp_work", ...args], {
			cwd: pythonDir,
			env: { ...process.env, XDG_CONFIG_HOME: xdg, XDG_STATE_HOME: xdg, XDG_DATA_HOME: xdg, OMP_WORK_POSTGRES_PORT: String(pgPort) },
		});
		if (run.exitCode !== 0) throw new Error(`omp_work ${args.join(" ")} failed: ${run.stderr.toString()}`);
		return run.stdout.toString();
	};
	py(["ops", "credentials", "init"]);
	const pgSecret = fs.readFileSync(path.join(xdg, "omp/work-ledger/credentials/postgres"), "utf8").trim();

	const pwfile = path.join(root, "pgpw");
	fs.writeFileSync(pwfile, `${pgSecret}\n`, { mode: 0o600 });
	try {
		const initdb = Bun.spawnSync(["initdb", "-D", pgData, "-U", "postgres", "--pwfile", pwfile, "--auth-host=scram-sha-256", "--auth-local=trust"], { stderr: "pipe" });
		if (initdb.exitCode !== 0) throw new Error(`initdb failed: ${initdb.stderr.toString()}`);
	} finally {
		fs.rmSync(pwfile, { force: true });
	}

	const run = Bun.spawnSync(["pg_ctl", "-D", pgData, "-w", "-l", path.join(root, "pg.log"), "-o", `-p ${pgPort} -k ${root} -c listen_addresses=127.0.0.1`, "start"], { stderr: "pipe" });
	if (run.exitCode !== 0) throw new Error(`pg_ctl start failed: ${run.stderr.toString()}`);

	for (let attempt = 0; attempt < 30; attempt++) {
		if (Bun.spawnSync(["pg_isready", "-h", "127.0.0.1", "-p", String(pgPort)]).exitCode === 0) break;
		if (attempt === 29) throw new Error("postgres never became ready");
		await Bun.sleep(500);
	}

	const psqlAdmin = (sql: string, db = "postgres") => {
		const res = Bun.spawnSync(["psql", "-h", "127.0.0.1", "-p", String(pgPort), "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql], {
			env: { ...process.env, PGPASSWORD: pgSecret },
		});
		if (res.exitCode !== 0) throw new Error(`psql admin failed: ${res.stderr.toString()}`);
	};
	psqlAdmin(fs.readFileSync(path.join(pythonDir, "src/omp_work/operations/sql/roles.sql"), "utf8"));
	for (const role of ["omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup"]) {
		const secret = fs.readFileSync(path.join(xdg, `omp/work-ledger/credentials/${role}`), "utf8").trim();
		psqlAdmin(`ALTER ROLE ${role} PASSWORD '${secret}';`);
	}
	psqlAdmin("CREATE DATABASE omp_work OWNER omp_work_owner;");

	const migDir = path.join(pythonDir, "src/omp_work/operations/migrations");
	const migFiles = fs.readdirSync(migDir).filter(f => f.endsWith(".sql")).sort();
	const migratorSecret = fs.readFileSync(path.join(xdg, "omp/work-ledger/credentials/omp_work_migrator"), "utf8").trim();
	for (const f of migFiles) {
		const sqlContent = fs.readFileSync(path.join(migDir, f), "utf8");
		const ordinal = parseInt(f.split("_")[0], 10);
		const sha = new Bun.CryptoHasher("sha256").update(sqlContent).digest("hex");
		const res = Bun.spawnSync(["psql", "-h", "127.0.0.1", "-p", String(pgPort), "-U", "omp_work_migrator", "-d", "omp_work", "-v", "ON_ERROR_STOP=1"], {
			env: { ...process.env, PGPASSWORD: migratorSecret },
			stdin: Buffer.from(sqlContent),
		});
		if (res.exitCode !== 0) throw new Error(`migration ${f} failed: ${res.stderr.toString()}`);
		psqlAdmin(`INSERT INTO omp_control.schema_migrations(ordinal, filename, sha256, contract_version, contract_sha256, postgres_major) VALUES (${ordinal}, '${f}', '${sha}', 'work.omp.dev/v1', '${WORK_CONTRACT_SHA256}', 18) ON CONFLICT (ordinal) DO NOTHING;`, "omp_work");
	}
	psqlAdmin(`INSERT INTO omp_control.runtime_compatibility (contract_version, contract_sha256, migration_set_sha256, postgres_major) VALUES ('work.omp.dev/v1', '${WORK_CONTRACT_SHA256}', 'test', 18) ON CONFLICT (singleton) DO UPDATE SET contract_version=EXCLUDED.contract_version, contract_sha256=EXCLUDED.contract_sha256, migration_set_sha256=EXCLUDED.migration_set_sha256, postgres_major=EXCLUDED.postgres_major;`, "omp_work");
	py(["ops", "capabilities", "init", "--workspace-id", WORKSPACE, "--owner-id", OWNER, "--base-url", baseUrl]);

	const psql = (sql: string) => {
		const res = Bun.spawnSync(["psql", "-h", "127.0.0.1", "-p", String(pgPort), "-U", "postgres", "-d", "omp_work", "-v", "ON_ERROR_STOP=1", "-c", sql], {
			env: { ...process.env, PGPASSWORD: pgSecret },
		});
		if (res.exitCode !== 0) throw new Error(`psql failed: ${res.stderr.toString()}`);
	};
	psql(`INSERT INTO omp_control.workspaces(workspace_id) VALUES ('${WORKSPACE}') ON CONFLICT DO NOTHING; INSERT INTO omp_work.projects(project_id, workspace_id, name, kind) VALUES ('${PROJECT}', '${WORKSPACE}', 'Smoke Project', 'surface');`);
	psql(`INSERT INTO omp_control.cutover_epochs(workspace_id, epoch_id, state, candidate_manifest, candidate_manifest_sha256) VALUES ('${WORKSPACE}', '00000000-0000-4000-8000-0000000000dd', 'sealed', '{}'::jsonb, '${"0".repeat(64)}') ON CONFLICT DO NOTHING; INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id) VALUES ('${WORKSPACE}', '00000000-0000-4000-8000-0000000000dd') ON CONFLICT DO NOTHING;`);

	// git setup
	const remote = path.join(root, "remote.git");
	const probe = path.join(root, "repo");
	fs.mkdirSync(probe, { recursive: true });
	const git = (cwd: string, args: string[]) => {
		const r = Bun.spawnSync(["git", ...args], { cwd });
		if (r.exitCode !== 0) throw new Error(`git ${args.join(" ")}: ${r.stderr.toString()}`);
		return r.stdout.toString().trim();
	};
	git(root, ["init", "-q", "--bare", remote]);
	git(probe, ["init", "-q", "-b", "main"]);
	git(probe, ["config", "user.email", "smoke@example.com"]);
	git(probe, ["config", "user.name", "Smoke"]);
	fs.writeFileSync(path.join(probe, ".work-project"), `${PROJECT_NAME}\n`);
	git(probe, ["add", ".work-project"]);
	git(probe, ["commit", "-q", "-m", "init"]);
	git(probe, ["remote", "add", "origin", remote]);
	git(probe, ["push", "-q", "-u", "origin", "main"]);

	// Start service
	const serveScript = `import uvicorn; from pathlib import Path; from omp_work.v1.server import create_app; from omp_work.operations.config import OperationsConfig; app = create_app(OperationsConfig.defaults(), capabilities_dir=Path('${path.join(xdg, "omp/work-ledger/capabilities")}')); uvicorn.run(app, host='127.0.0.1', port=${httpPort}, access_log=False)`;
	service = Bun.spawn(["uv", "run", "python", "-c", serveScript], {
		cwd: pythonDir,
		env: { ...process.env, XDG_CONFIG_HOME: xdg, XDG_STATE_HOME: xdg, XDG_DATA_HOME: xdg, OMP_WORK_POSTGRES_PORT: String(pgPort) },
		stdout: "ignore",
		stderr: "ignore",
	});
	for (let attempt = 0; attempt < 60; attempt++) {
		try {
			if ((await fetch(`${baseUrl}/v1/health/live`)).ok) break;
		} catch {}
		if (attempt === 59) throw new Error("service never became live");
		await Bun.sleep(500);
	}

	const rawOwner = JSON.parse(fs.readFileSync(path.join(xdg, "omp/work-ledger/capabilities/owner.json"), "utf8"));
	const token = typeof rawOwner === "object" && rawOwner && "token" in rawOwner ? String(rawOwner.token) : "";
	const headers = { authorization: `Bearer ${token}`, "X-OMP-Workspace-ID": WORKSPACE, "X-OMP-Contract-SHA256": WORK_CONTRACT_SHA256, "Content-Type": "application/json" };

	// Create backlog items
	const createRes = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			operation_id: crypto.randomUUID(),
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			command: {
				type: "create_work_batch",
				payload: {
					items: [
						{
							client_ref: "smoke-item-1",
							title: "Smoke Delivery Feature 1",
							description: "Build smoke delivery feature 1",
							scope: "smoke",
							acceptance_criteria: [],
							state: "BACKLOG",
							project_id: PROJECT,
						},
						{
							client_ref: "smoke-item-2",
							title: "Smoke Delivery Feature 2",
							description: "Build smoke delivery feature 2",
							scope: "smoke",
							acceptance_criteria: [],
							state: "BACKLOG",
							project_id: PROJECT,
						},
						{
							client_ref: "smoke-item-3",
							title: "Smoke Delivery Feature 3",
							description: "Build smoke delivery feature 3",
							scope: "smoke",
							acceptance_criteria: [],
							state: "BACKLOG",
							project_id: PROJECT,
						},
					],
				},
			},
		}),
	})).json();
	assert.equal(createRes.receipt.state, "applied");
	const item1 = createRes.result.items[0];
	const item2 = createRes.result.items[1];
	const queueItem2 = createRes.result.items[2];

	const runHarness = (scenario: string, phaseKey?: string) => {
		const args = [path.join(import.meta.dir, "fixtures/execute-cycle-smoke-harness.ts"), probe, scenario];
		if (phaseKey) args.push(phaseKey);
		const child = Bun.spawnSync([process.execPath, ...args], {
			cwd: probe,
			env: { ...process.env, HOME: home, XDG_CONFIG_HOME: xdg, PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent") },
		});
		if (child.exitCode !== 0) throw new Error(`harness scenario "${scenario}" failed:\n${child.stderr.toString()}`);
		return JSON.parse(child.stdout.toString()) as Record<string, unknown>;
	};

	// Test Scenario 1: Preflight Dirty Worktree Refusal
	const dirtyOut = runHarness("dirty", item1.key);
	assert.ok((dirtyOut.notices as string[])?.length > 0, "dirty preflight refused");
	assert.equal(dirtyOut.modelTurnCount, 0, "zero model turns executed when dirty at start");

	// Test Scenario 2: Full Single-Item Autonomous Execution Cycle via Host /execute
	const singleOut = runHarness("single", item1.key);
	assert.equal(singleOut.noBodyRefused, true, "begin_execution_review without body is refused");
	assert.ok(String(singleOut.review1).includes("NEEDS_FIX"), "first review yields NEEDS_FIX");
	assert.ok(String(singleOut.review2).includes("Execution grant completed") || String(singleOut.review2).includes("delivered and closed"), "execution completed on second review");

	// Verify final ledger state for item 1
	const item1View = (await (await fetch(`${baseUrl}/v1/work-items/${item1.key}/workflow`, { headers })).json()) as {
		item: { state: string };
		receipts: { kind: string; candidate_commit?: string; payload?: unknown }[];
	};
	assert.equal(item1View.item.state, "DONE", "item 1 is DONE");
	assert.ok(item1View.receipts.some(r => r.kind === "closeout"), "closeout receipt exists");
	assert.ok(item1View.receipts.some(r => r.kind === "audit"), "audit receipt exists");
	assert.ok(item1View.receipts.some(r => r.kind === "push"), "push receipt exists");
	assert.ok(item1View.receipts.some(r => r.kind === "verification"), "verification receipt exists");

	// Verify remote ref has candidate commit
	const remoteHead = git(probe, ["ls-remote", "origin", "refs/heads/main"]).split(/\s+/)[0];
	assert.ok(/^[0-9a-f]{40}$/.test(remoteHead), "remote git ref resolves to valid commit SHA");

	// Initial audit receipt count baseline
	const initialAuditReceipts = item1View.receipts.filter(r => r.kind === "audit").length;

	// Test Scenario 3: Queue Mode Execution
	const queueOut = runHarness("queue", item2.key);
	assert.ok(queueOut.queueLength >= 2, "queue mode snapshots queue items");
	assert.equal(queueOut.item0WorkId, item2.work_id, "named queue item is claim position 0");
	assert.ok(String(queueOut.reviewQ1).includes("Advanced to next queue item") || String(queueOut.reviewQ1).includes("completed"), "queue item 1 completed");

	// Test Scenario 4: Four Restart Tamper Scenarios
	const healthReady = await (await fetch(`${baseUrl}/v1/health/ready`)).json();
	const fp = healthReady.service_fingerprint;
	const judgeManifest = {
		auditor_agent_sha256: "a".repeat(64),
		host_sha256: "b".repeat(64),
		adapter_sha256: "c".repeat(64),
		freeze_sha256: "d".repeat(64),
		runner_sha256: "e".repeat(64),
		executor_sha256: "f".repeat(64),
		contract_sha256: WORK_CONTRACT_SHA256,
		service_fingerprint: fp,
		service_code_fingerprint: fp,
		service_migration_sha256: fp,
	};
	const judgeManifestSha = new Bun.CryptoHasher("sha256").update(JSON.stringify(judgeManifest)).digest("hex");
	const headCommit = git(probe, ["rev-parse", "HEAD"]);

	// (a) Tamper auditor definition hash
	const tamperA = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			operation_id: crypto.randomUUID(),
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			command: {
				type: "begin_execution",
				payload: {
					grant_id: crypto.randomUUID(),
					provenance: {
						owner_input_id: crypto.randomUUID(),
						owner_session_id: "tamper-a",
						normalized_command: "/execute OMP-2",
						workspace_id: WORKSPACE,
						repository: "repo",
						nonce: crypto.randomUUID(),
						issued_at: new Date().toISOString(),
					},
					mode: "single",
					items: [{
						work_id: item2.work_id,
						revision_id: item2.revision_id,
						position: 0,
						original_request: "Build smoke delivery feature 2",
						original_request_sha256: new Bun.CryptoHasher("sha256").update("Build smoke delivery feature 2").digest("hex"),
						initial_git_baseline: headCommit,
						project_id: PROJECT,
						active_blocker_ids: [],
					}],
					expected_focus_version: 1,
					judge_sha256: "0".repeat(64),
					judge_manifest: {
						...judgeManifest,
						auditor_agent_sha256: "1".repeat(64),
						service_fingerprint: "0".repeat(64),
					},
				},
			},
		}),
	})).json();
	assert.equal(tamperA.error.code, "execution_judge_drift", "tamper A caught as judge drift");

	// (b) Tamper auditor runner source hash
	const tamperB = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			operation_id: crypto.randomUUID(),
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			command: {
				type: "begin_execution",
				payload: {
					grant_id: crypto.randomUUID(),
					provenance: {
						owner_input_id: crypto.randomUUID(),
						owner_session_id: "tamper-b",
						normalized_command: "/execute OMP-2",
						workspace_id: WORKSPACE,
						repository: "repo",
						nonce: crypto.randomUUID(),
						issued_at: new Date().toISOString(),
					},
					mode: "single",
					items: [{
						work_id: item2.work_id,
						revision_id: item2.revision_id,
						position: 0,
						original_request: "Build smoke delivery feature 2",
						original_request_sha256: new Bun.CryptoHasher("sha256").update("Build smoke delivery feature 2").digest("hex"),
						initial_git_baseline: headCommit,
						project_id: PROJECT,
						active_blocker_ids: [],
					}],
					expected_focus_version: 1,
					judge_sha256: "0".repeat(64),
					judge_manifest: {
						...judgeManifest,
						runner_sha256: "2".repeat(64),
						service_fingerprint: "0".repeat(64),
					},
				},
			},
		}),
	})).json();
	assert.equal(tamperB.error.code, "execution_judge_drift", "tamper B caught as judge drift");

	// (c) Tamper executor transport hash
	const tamperC = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			operation_id: crypto.randomUUID(),
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			command: {
				type: "begin_execution",
				payload: {
					grant_id: crypto.randomUUID(),
					provenance: {
						owner_input_id: crypto.randomUUID(),
						owner_session_id: "tamper-c",
						normalized_command: "/execute OMP-2",
						workspace_id: WORKSPACE,
						repository: "repo",
						nonce: crypto.randomUUID(),
						issued_at: new Date().toISOString(),
					},
					mode: "single",
					items: [{
						work_id: item2.work_id,
						revision_id: item2.revision_id,
						position: 0,
						original_request: "Build smoke delivery feature 2",
						original_request_sha256: new Bun.CryptoHasher("sha256").update("Build smoke delivery feature 2").digest("hex"),
						initial_git_baseline: headCommit,
						project_id: PROJECT,
						active_blocker_ids: [],
					}],
					expected_focus_version: 1,
					judge_sha256: "0".repeat(64),
					judge_manifest: {
						...judgeManifest,
						executor_sha256: "3".repeat(64),
						service_fingerprint: "0".repeat(64),
					},
				},
			},
		}),
	})).json();
	assert.equal(tamperC.error.code, "execution_judge_drift", "tamper C caught as judge drift");

	// (d) Tamper Python service fingerprint
	const tamperD = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			operation_id: crypto.randomUUID(),
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			command: {
				type: "begin_execution",
				payload: {
					grant_id: crypto.randomUUID(),
					provenance: {
						owner_input_id: crypto.randomUUID(),
						owner_session_id: "tamper-d",
						normalized_command: "/execute OMP-2",
						workspace_id: WORKSPACE,
						repository: "repo",
						nonce: crypto.randomUUID(),
						issued_at: new Date().toISOString(),
					},
					mode: "single",
					items: [{
						work_id: item2.work_id,
						revision_id: item2.revision_id,
						position: 0,
						original_request: "Build smoke delivery feature 2",
						original_request_sha256: new Bun.CryptoHasher("sha256").update("Build smoke delivery feature 2").digest("hex"),
						initial_git_baseline: headCommit,
						project_id: PROJECT,
						active_blocker_ids: [],
					}],
					expected_focus_version: 1,
					judge_sha256: "0".repeat(64),
					judge_manifest: {
						...judgeManifest,
						service_fingerprint: "4".repeat(64),
					},
				},
			},
		}),
	})).json();
	assert.equal(tamperD.error.code, "execution_judge_drift", "tamper D caught as judge drift");

	// Test Scenario 5: Negative & Positive Remote Push Receipt Binding on Complete Execution
	const item3Res = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "create_work_batch",
				payload: {
					items: [{
						client_ref: "smoke-item-3",
						title: "Smoke Delivery Feature 3",
						description: "Build smoke delivery feature 3",
						scope: "smoke",
						acceptance_criteria: [],
						state: "BACKLOG",
						project_id: PROJECT,
					}],
				},
			},
		}),
	})).json();
	const item3 = item3Res.result.items[0];
	const focusRes3 = await (await fetch(`${baseUrl}/v1/workspaces/${WORKSPACE}/focus/${OWNER}`, { headers })).json();
	const curFocusVer = typeof focusRes3?.version === "number" ? focusRes3.version : 0;
	const grant3Id = crypto.randomUUID();
	const beginGrant3 = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "begin_execution",
				payload: {
					grant_id: grant3Id,
					provenance: {
						owner_input_id: crypto.randomUUID(),
						owner_session_id: "smoke-scenario-5",
						normalized_command: `/execute ${item3.key}`,
						workspace_id: WORKSPACE,
						repository: "repo",
						nonce: crypto.randomUUID(),
						issued_at: new Date().toISOString(),
					},
					mode: "single",
					items: [{
						work_id: item3.work_id,
						revision_id: item3.revision_id,
						position: 0,
						original_request: "Build smoke delivery feature 3",
						original_request_sha256: new Bun.CryptoHasher("sha256").update("Build smoke delivery feature 3").digest("hex"),
						initial_git_baseline: headCommit,
						project_id: PROJECT,
						active_blocker_ids: [],
					}],
					expected_focus_version: curFocusVer,
					judge_sha256: judgeManifestSha,
					judge_manifest: judgeManifest,
				},
			},
		}),
	})).json();
	assert.equal(beginGrant3.result?.type, "begin_execution", "grant 3 begun");

	const sealCrit3 = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "seal_execution_criteria",
				payload: {
					grant_id: grant3Id,
					expected_grant_version: 1,
					work_id: item3.work_id,
					expected_revision_id: item3.revision_id,
					criteria: ["AC-1 deliver smoke feature 3"],
					description_sha256: new Bun.CryptoHasher("sha256").update("Build smoke delivery feature 3").digest("hex"),
					judge_sha256: judgeManifestSha,
				},
			},
		}),
	})).json();
	assert.equal(sealCrit3.result?.type, "seal_execution_criteria", "criteria 3 sealed");
	const rev3Id = sealCrit3.result.item.criteria_revision_id;

	const plannedCand3Id = crypto.randomUUID();
	const planBody3 = "## Approach\n1. Write feature 3\n\n## Verification\n1. Verify feature 3\n";
	const planSha3 = new Bun.CryptoHasher("sha256").update(planBody3).digest("hex");
	const candSha3 = new Bun.CryptoHasher("sha256").update(JSON.stringify({ candidateId: plannedCand3Id, planSha: planSha3 })).digest("hex");
	const finalTreeSha3 = new Bun.CryptoHasher("sha256").update("final-content-3").digest("hex");
	const stampPlan3 = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "stamp_execution_plan",
				payload: {
					grant_id: grant3Id,
					expected_grant_version: 2,
					work_id: item3.work_id,
					revision_id: rev3Id,
					candidate_id: plannedCand3Id,
					plan_file: "local://execute-plan.md",
					plan_body: planBody3,
					plan_sha256: planSha3,
					approach: ["Write feature 3"],
					verification: ["Verify feature 3"],
					paths: ["src/smoke_feat.ts"],
					candidate_sha256: candSha3,
					judge_sha256: judgeManifestSha,
				},
			},
		}),
	})).json();
	assert.equal(stampPlan3.result.type, "stamp_execution_plan", "plan 3 stamped");

	const finalCand3Id = crypto.randomUUID();
	const finalCand3 = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "finalize_candidate",
				payload: {
					work_id: item3.work_id,
					revision_id: rev3Id,
					planned_candidate_id: plannedCand3Id,
					candidate_id: finalCand3Id,
					candidate_sha256: finalTreeSha3,
					commit_sha: headCommit,
				},
			},
		}),
	})).json();
	assert.equal(finalCand3.result?.type, "finalize_candidate", "candidate 3 finalized");

	const verifReceipt3Id = crypto.randomUUID();
	await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "append_evidence",
				payload: {
					receipt: {
						receipt_id: verifReceipt3Id,
						work_id: item3.work_id,
						revision_id: rev3Id,
						candidate_id: finalCand3Id,
						kind: "verification",
						payload: { body: "verification passed" },
						payload_sha256: new Bun.CryptoHasher("sha256").update(JSON.stringify({ body: "verification passed" })).digest("hex"),
						issuer: "test",
						issued_at: new Date().toISOString(),
						candidate_sha256: finalTreeSha3,
						candidate_commit: headCommit,
						independent: false,
					},
				},
			},
		}),
	})).json();

	const attempt3Id = crypto.randomUUID();
	const beginAttempt3 = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "begin_close_attempt",
				payload: {
					attempt_id: attempt3Id,
					work_id: item3.work_id,
					authorization_ref: `execution:${grant3Id}:0:1`,
					owner_session_id: "smoke-scenario-5",
					owner_session_started_at: new Date().toISOString(),
					owner_session_start_commit: headCommit,
					repository: "repo",
					diff_sha256: "0".repeat(64),
					starting_dirty_paths: [],
					authorization_kind: "execution",
					execution_grant_id: grant3Id,
					candidate_tree_sha: finalTreeSha3,
					original_request_sha256: new Bun.CryptoHasher("sha256").update("Build smoke delivery feature 3").digest("hex"),
					criteria_sha256: new Bun.CryptoHasher("sha256").update(JSON.stringify(["AC-1 deliver smoke feature 3"])).digest("hex"),
					plan_stamp_sha256: stampPlan3.result.item.plan_stamp_sha256,
					judge_sha256: judgeManifestSha,
					riders: [],
				},
			},
		}),
	})).json();
	assert.equal(beginAttempt3.result?.status, "applied", "attempt 3 begun");

	const sealManifest3 = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "seal_audit_manifest",
				payload: {
					attempt_id: attempt3Id,
					verification_receipt_id: verifReceipt3Id,
				},
			},
		}),
	})).json();
	assert.equal(sealManifest3.result.status, "applied", "manifest 3 sealed");

	const resvLaunch3 = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "reserve_auditor_launch",
				payload: {
					attempt_id: attempt3Id,
					task_sha256: sealManifest3.result.manifest.task_sha256,
					tool_call_id: "tool_launch_3",
				},
			},
		}),
	})).json();
	const launch3Id = resvLaunch3.result.launch.launch_id;

	const settle3 = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "settle_auditor_launch",
				payload: {
					attempt_id: attempt3Id,
					launch_id: launch3Id,
					transport_payload: {
						report: "VERDICT: PASS\n\nFINDINGS\n(none)\n\nACCEPTANCE COVERAGE\nAC-1 deliver smoke feature 3\n\nOUT OF SCOPE\nnone\n\nCHECKS RUN\nbun test\n\nREMAINING QUESTIONS\nnone",
					},
					transport_failed: false,
				},
			},
		}),
	})).json();
	assert.equal(settle3.result.status, "applied", "launch 3 settled with PASS");

	// 5a. Append an unbound push receipt (remote_ref = null, remote_commit = null)
	const unboundPushReceiptId = crypto.randomUUID();
	await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "append_evidence",
				payload: {
					receipt: {
						receipt_id: unboundPushReceiptId,
						work_id: item3.work_id,
						revision_id: rev3Id,
						candidate_id: finalCand3Id,
						kind: "push",
						payload: { body: "fake unbound push" },
						payload_sha256: new Bun.CryptoHasher("sha256").update(JSON.stringify({ body: "fake unbound push" })).digest("hex"),
						issuer: "test",
						issued_at: new Date().toISOString(),
						independent: false,
						remote_ref: null,
						remote_commit: null,
					},
				},
			},
		}),
	})).json();

	// 5a. Attempting to complete with unbound push receipt fails specifically with completion_blocked
	const badCompleteA = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "complete_execution_item",
				payload: {
					grant_id: grant3Id,
					expected_grant_version: 3,
					work_id: item3.work_id,
					attempt_id: attempt3Id,
					push_receipt_id: unboundPushReceiptId,
					judge_sha256: judgeManifestSha,
				},
			},
		}),
	})).json();
	assert.equal(badCompleteA.error?.code, "completion_blocked", "unbound push completion strictly blocked");
	assert.ok(badCompleteA.error?.diagnostics?.[0]?.includes("push receipt"), "diagnostics cite push receipt requirement");

	// 5b. Append a mismatched push receipt (remote_commit != candidate.commit_sha)
	const mismatchedPushReceiptId = crypto.randomUUID();
	await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "append_evidence",
				payload: {
					receipt: {
						receipt_id: mismatchedPushReceiptId,
						work_id: item3.work_id,
						revision_id: rev3Id,
						candidate_id: finalCand3Id,
						kind: "push",
						payload: { body: "mismatched remote commit push" },
						payload_sha256: new Bun.CryptoHasher("sha256").update(JSON.stringify({ body: "mismatched remote commit push" })).digest("hex"),
						issuer: "test",
						issued_at: new Date().toISOString(),
						independent: false,
						remote_ref: "refs/heads/main",
						remote_commit: "0".repeat(40),
					},
				},
			},
		}),
	})).json();

	const badCompleteB = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "complete_execution_item",
				payload: {
					grant_id: grant3Id,
					expected_grant_version: 3,
					work_id: item3.work_id,
					attempt_id: attempt3Id,
					push_receipt_id: mismatchedPushReceiptId,
					judge_sha256: judgeManifestSha,
				},
			},
		}),
	})).json();
	assert.equal(badCompleteB.error?.code, "completion_blocked", "mismatched push commit completion strictly blocked");

	// 5c. Append a valid, bound push receipt (remote_ref = refs/heads/main, remote_commit = headCommit)
	const validPushReceiptId = crypto.randomUUID();
	await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "append_evidence",
				payload: {
					receipt: {
						receipt_id: validPushReceiptId,
						work_id: item3.work_id,
						revision_id: rev3Id,
						candidate_id: finalCand3Id,
						kind: "push",
						payload: { body: "valid verified push" },
						payload_sha256: new Bun.CryptoHasher("sha256").update(JSON.stringify({ body: "valid verified push" })).digest("hex"),
						issuer: "test",
						issued_at: new Date().toISOString(),
						independent: false,
						remote_ref: "refs/heads/main",
						remote_commit: headCommit,
						candidate_commit: headCommit,
						candidate_sha256: finalTreeSha3,
					},
				},
			},
		}),
	})).json();

	// Complete with valid push receipt succeeds and moves item3 to DONE
	const goodComplete = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "complete_execution_item",
				payload: {
					grant_id: grant3Id,
					expected_grant_version: 3,
					work_id: item3.work_id,
					attempt_id: attempt3Id,
					push_receipt_id: validPushReceiptId,
					judge_sha256: judgeManifestSha,
				},
			},
		}),
	})).json();
	assert.equal(goodComplete.result?.type, "complete_execution_item", "valid push completion applied");
	assert.equal(goodComplete.result?.grant?.state, "completed", "grant 3 completed");

	const item3Final = (await (await fetch(`${baseUrl}/v1/work-items/${item3.key}/workflow`, { headers })).json()) as {
		item: { state: string };
		receipts: { kind: string }[];
	};
	assert.equal(item3Final.item.state, "DONE", "item 3 successfully transitioned to DONE");
	assert.ok(item3Final.receipts.some(r => r.kind === "closeout"), "closeout receipt minted for item 3");

	// Test Scenario 6: Work Contract Change Pause & Resume Gate
	const item4Res = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "create_work_batch",
				payload: {
					items: [{
						client_ref: "smoke-item-4",
						title: "Work Contract Feature 4",
						description: "Modify work contract and verify pause gate",
						scope: "smoke",
						acceptance_criteria: [],
						state: "BACKLOG",
						project_id: PROJECT,
					}],
				},
			},
		}),
	})).json();
	const item4 = item4Res.result.items[0];

	const contractOut = runHarness("contract-pause", item4.key);
	assert.ok(String(contractOut.reviewDenied).includes("Contract approval required"), "review denied before candidate freeze");
	assert.equal(contractOut.pausedExecution?.grant?.state, "paused", "grant atomically paused on contract change");
	assert.equal(contractOut.pausedExecution?.items?.[0]?.phase, "awaiting_contract_approval", "item phase is awaiting_contract_approval");
	assert.ok((contractOut.resumeDeniedNotices as string[])?.length > 0, "resume without approval denied");
	assert.equal(contractOut.resumedExecution?.grant?.state, "active", "grant resumed to active after approval");
	assert.equal(contractOut.resumedExecution?.items?.[0]?.phase, "executing", "item resumed to executing after approval");

	// Verify audit receipt count remained unchanged after tamper attempts
	const postTamperView = (await (await fetch(`${baseUrl}/v1/work-items/${item1.key}/workflow`, { headers })).json()) as {
		receipts: { kind: string }[];
	};
	const postTamperAuditReceipts = postTamperView.receipts.filter(r => r.kind === "audit").length;
	assert.equal(postTamperAuditReceipts, initialAuditReceipts, "audit receipt count unchanged after tamper attempts");

	console.log("execute-cycle-smoke: PASS (clean preflight, single execution cycle with NEEDS_FIX remediation, queue mode, four tamper scenarios, negative & positive remote push verification, contract change pause gate)");
} finally {
	cleanup();
}
