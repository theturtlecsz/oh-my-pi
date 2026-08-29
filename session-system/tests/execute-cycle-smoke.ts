// OMP-180 execution cycle smoke test (plan verification steps 4 & 5):
//   OMP_WORK_POSTGRES_INTEGRATION=1 bun run session-system/tests/execute-cycle-smoke.ts
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { canonicalJson, sha256Hex, WORK_CONTRACT_SHA256 } from "@oh-my-pi/pi-work-client";
import { pushCandidate, validateExecutionPath } from "../extensions/workflow/git";
import { computeAuditTcb } from "../extensions/workflow/audit-tcb";

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
							acceptance_criteria: ["AC-1 deliver smoke feature (stored verbatim)"],
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

	const runHarness = (scenario: string, phaseKey?: string, extraEnv?: Record<string, string>) => {
		const args = [path.join(import.meta.dir, "fixtures/execute-cycle-smoke-harness.ts"), probe, scenario];
		if (phaseKey) args.push(phaseKey);
		const child = Bun.spawnSync([process.execPath, ...args], {
			cwd: probe,
			env: { ...process.env, HOME: home, XDG_CONFIG_HOME: xdg, PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent"), ...extraEnv },
		});
		if (child.exitCode !== 0) throw new Error(`harness scenario "${scenario}" failed:\n${child.stderr.toString()}`);
		if (child.stderr.length > 0) console.error("HARNESS STDERR:", child.stderr.toString());
		const raw = child.stdout.toString().trim();
		try {
			return JSON.parse(raw) as Record<string, unknown>;
		} catch (err) {
			console.log("HARNESS RAW OUTPUT:", raw);
			throw err;
		}
	};

	// Test Scenario 0: Execution path validation — a planned NEW file at the repo
	// root must stamp (the root's own .git is not a submodule marker); a nested
	// parent carrying .git remains an embedded-repo refusal.
	const rootNewFile = validateExecutionPath("brand-new-root-file.ts", probe);
	assert.equal(rootNewFile.valid, true, `new root-level file must validate: ${rootNewFile.error ?? ""}`);
	fs.mkdirSync(path.join(probe, "embedded"), { recursive: true });
	fs.writeFileSync(path.join(probe, "embedded/.git"), "gitdir: elsewhere\n");
	const embeddedNewFile = validateExecutionPath("embedded/new-file.ts", probe);
	assert.equal(embeddedNewFile.valid, false, "embedded-repo parent must refuse");
	assert.ok(embeddedNewFile.error?.includes("submodule"), "refusal names submodule");
	fs.rmSync(path.join(probe, "embedded"), { recursive: true, force: true });

	// Test Scenario 1: Preflight Dirty Worktree Refusal
	const dirtyOut = runHarness("dirty", item1.key);
	assert.ok((dirtyOut.notices as string[])?.length > 0, "dirty preflight refused");
	assert.equal(dirtyOut.modelTurnCount, 0, "zero model turns executed when dirty at start");

	// Test Scenario 2: Full Single-Item Autonomous Execution Cycle via Host /execute
	const singleOut = runHarness("single", item1.key);
	assert.equal(singleOut.noBodyRefused, true, "begin_execution_review without body is refused");
	assert.ok(String(singleOut.review1).includes("NEEDS_FIX"), `first review yields NEEDS_FIX; got: ${singleOut.review1}`);
	assert.ok(String(singleOut.review2).includes("Execution grant completed") || String(singleOut.review2).includes("delivered and closed"), `execution completed on second review; got: ${singleOut.review2}`);
	// Prompt-shaped seal (no work param, mismatched derived proposal) must seal
	// the stored criteria verbatim and surface them to the session.
	assert.ok(
		String(singleOut.sealResult).includes("AC-1 deliver smoke feature (stored verbatim)"),
		"seal response reports stored criteria verbatim",
	);
	assert.ok(
		!String(singleOut.sealResult).includes("guessed paraphrase"),
		"derived proposal is discarded for AC-bearing items",
	);

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
					remote_ref: "refs/heads/main",
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
					remote_ref: "refs/heads/main",
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
					remote_ref: "refs/heads/main",
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
					remote_ref: "refs/heads/main",
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
					remote_ref: "refs/heads/main",
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

	// 5c. Append a valid, bound push receipt (repository, remote_ref = refs/heads/main, prior_tip, candidate_commit, result_tip)
	const validPushReceiptId = crypto.randomUUID();
	const validPushPayload = {
		repository: "repo",
		remote_url: remote,
		remote_ref: "refs/heads/main",
		prior_tip: headCommit,
		candidate_commit: headCommit,
		result_tip: headCommit,
	};
	const appendPushRes = await (await fetch(`${baseUrl}/v1/commands`, {
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
						payload: validPushPayload,
						payload_sha256: sha256Hex(canonicalJson(validPushPayload)),
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
	assert.equal(appendPushRes.result?.type, "append_evidence", "push receipt appended");

	// Attest attempt 3 checkpoint deliveries
	for (const event of [beginAttempt3.result.event, settle3.result.event]) {
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
					type: "attest_checkpoint_delivery",
					payload: {
						event_id: event.event_id,
						owner_session_id: "session-1",
						rendered_sha256: event.rendered_sha256,
						status: "delivered",
					},
				},
			}),
		})).json();
	}
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
	assert.equal(contractOut.resumedExecution?.items?.[0]?.phase, "planning", "item resumed to planning after approval");
	// Cancel grant 4 before starting scenario 7
	const cancelGrant = async (grantId?: string, grantVer?: number, judgeSha?: string) => {
		if (!grantId || grantVer === undefined) return;
		const res = await (await fetch(`${baseUrl}/v1/commands`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE,
				request_id: crypto.randomUUID(),
				correlation_id: crypto.randomUUID(),
				operation_id: crypto.randomUUID(),
				command: {
					type: "set_execution_state",
					payload: {
						grant_id: grantId,
						target_state: "canceled",
						expected_grant_version: grantVer,
						reason: "test_cleanup",
						judge_sha256: judgeSha ?? judgeManifestSha,
					},
				},
			}),
		})).json();
		if (res.error) throw new Error(`cancelGrant failed for ${grantId} v${grantVer}: ${JSON.stringify(res.error)}`);
		assert.equal(res.result?.type, "set_execution_state");
	};

	await cancelGrant(contractOut.resumedExecution.grant.grant_id, contractOut.resumedExecution.grant.grant_version, contractOut.resumedExecution.grant.judge_sha256);

	// Test Scenario 7: Independent Disposable Recovery & Drift Matrix
	let recoverySeq = 0;
	const createAndStartDisposableGrant = async (label: string, branchName?: string) => {
		recoverySeq++;
		fs.rmSync(path.join(probe, "python"), { recursive: true, force: true });
		fs.rmSync(path.join(path.dirname(probe), ".smoke-session-branch.json"), { force: true });
		Bun.spawnSync(["git", "clean", "-fdx"], { cwd: probe });
		if (branchName) {
			Bun.spawnSync(["git", "checkout", "-B", branchName], { cwd: probe });
		} else {
			Bun.spawnSync(["git", "checkout", "main"], { cwd: probe });
			Bun.spawnSync(["git", "reset", "--hard", headCommit], { cwd: probe });
		}
		const itemRes = await (await fetch(`${baseUrl}/v1/commands`, {
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
							client_ref: `smoke-rec-${recoverySeq}-${label}`,
							title: `Recovery Test ${label}`,
							description: `Verify recovery isolation for ${label}`,
							scope: "smoke",
							acceptance_criteria: [],
							state: "BACKLOG",
							project_id: PROJECT,
						}],
					},
				},
			}),
		})).json();
		const item = itemRes.result.items[0];
		const startOut = runHarness("start-only", item.key);
		assert.equal(startOut.exec?.grant?.state, "active", `grant for ${label} started`);
		assert.equal(startOut.exec?.grant?.continuations_scheduled, 0, `continuations start at 0 for ${label}`);
		return { item, startOut };
	};
	// Test Scenario 7a: Post-PASS Queue Dirt Stops Grant with execution_worktree_not_clean
	fs.rmSync(path.join(probe, "python"), { recursive: true, force: true });
	Bun.spawnSync(["git", "clean", "-fdx"], { cwd: probe });
	Bun.spawnSync(["git", "checkout", "main"], { cwd: probe });
	Bun.spawnSync(["git", "reset", "--hard", headCommit], { cwd: probe });
	const queueDirtBatch = await (await fetch(`${baseUrl}/v1/commands`, {
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
					items: [
						{
							client_ref: "smoke-qdirt-1",
							title: "Queue dirt item 1",
							description: "Queue dirt item 1 description",
							scope: "surface",
						},
						{
							client_ref: "smoke-qdirt-2",
							title: "Queue dirt item 2",
							description: "Queue dirt item 2 description",
							scope: "surface",
						},
					],
				},
			},
		}),
	})).json();
	const qDirt1 = queueDirtBatch.result.items[0];
	const queueDirtOut = runHarness("queue-dirt", qDirt1.key);
	assert.ok(fs.existsSync(path.join(probe, "residual-dirt.txt")), "residual file still exists on disk");
	assert.ok(String(queueDirtOut.reviewQ1).includes("execution_worktree_not_clean"), "queue advance with dirt refused");
	assert.equal(queueDirtOut.finalExecution?.grant?.state, "stopped", "grant stopped on queue dirt");
	assert.equal(queueDirtOut.finalExecution?.grant?.terminal_reason, "execution_worktree_not_clean", "terminal reason recorded for queue dirt");
	assert.ok(queueDirtOut.finalExecution?.grant?.stopped_at, "stopped_at is set");
	assert.equal(queueDirtOut.finalExecution?.items[0]?.phase, "completed", "delivered item remains completed");
	assert.equal(queueDirtOut.finalExecution?.items[1]?.phase, "skipped", "remaining claims are skipped");
	const qDirtFocus = await (await fetch(`${baseUrl}/v1/workspaces/${WORKSPACE}/focus/${OWNER}`, { headers })).json();
	assert.equal(qDirtFocus.work_id, null, "focus slot is cleared on queue dirt terminalization");
	assert.ok(!((queueDirtOut.sentMessages as Array<{ content?: string }>) || []).some(m => typeof m?.content === "string" && m.content.includes(queueDirtBatch.result.items[1].key)), "no next-item continuation emitted");
	assert.equal(queueDirtOut.finalExecution?.items[1]?.activated_at, null, "next item never activated");
	fs.rmSync(path.join(probe, "residual-dirt.txt"), { force: true });
	// 1. Happy Path Recovery
	const happy = await createAndStartDisposableGrant("happy");
	const happyOut1 = runHarness("recovery", happy.item.key);
	assert.equal((happyOut1.sentMessages as unknown[])?.length, 1, "happy path recovery sends exactly one turn");
	assert.equal(happyOut1.exec?.grant?.continuations_scheduled, 1, "continuations_scheduled incremented to 1");
	assert.equal(happyOut1.exec?.grant?.grant_version, 2, "grant_version incremented to 2");
	const happyOut2 = runHarness("recovery", happy.item.key);
	assert.equal((happyOut2.sentMessages as unknown[])?.length, 0, "duplicate replay sends zero turns");
	const happyFinalExec = happyOut2.exec ?? happyOut1.exec;
	await cancelGrant(happyFinalExec?.grant?.grant_id, happyFinalExec?.grant?.grant_version, happyFinalExec?.grant?.judge_sha256);

	// 1b. Crash-after-commit-before-append: journaled set_execution_state claim exists on disk
	const crashCase = await createAndStartDisposableGrant("crash-gap");
	const crashGrantId = crashCase.startOut.exec?.grant?.grant_id;
	const crashJudgeSha = crashCase.startOut.exec?.grant?.judge_sha256;
	const crashRes = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "set_execution_state",
				payload: {
					grant_id: crashGrantId,
					expected_grant_version: 1,
					target_state: "active",
					reason: "session_start_recovery",
					judge_sha256: crashJudgeSha,
				},
			},
		}),
	})).json();
	assert.equal(crashRes.result?.grant?.grant_version, 2, "ledger advanced to version 2");
	const pendingDir = path.join(xdg, "omp-work", "pending-operations");
	fs.mkdirSync(pendingDir, { recursive: true, mode: 0o700 });
	const crashClaimPath = path.join(pendingDir, "crash-gap-claim.json");
	fs.writeFileSync(crashClaimPath, JSON.stringify({
		envelope: {
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			operation_id: crypto.randomUUID(),
			command: {
				type: "set_execution_state",
				payload: {
					grant_id: crashGrantId,
					expected_grant_version: 1,
					target_state: "active",
					reason: "session_start_recovery",
					judge_sha256: crashJudgeSha,
				},
			},
		},
		result: crashRes.result,
		resolved_at: new Date().toISOString(),
	}));
	fs.rmSync(path.join(path.dirname(probe), ".smoke-session-branch.json"), { force: true });

	const recoveryCrash1 = runHarness("recovery", crashCase.item.key);
	assert.equal((recoveryCrash1.sentMessages as unknown[])?.length, 1, "recovery delivers the un-appended turn");
	assert.equal(recoveryCrash1.exec?.grant?.continuations_scheduled, 1, "continuations_scheduled remained 1");
	assert.equal(recoveryCrash1.exec?.grant?.grant_version, 2, "grant_version remained 2");

	const recoveryCrash2 = runHarness("recovery", crashCase.item.key);
	assert.equal((recoveryCrash2.sentMessages as unknown[])?.length, 0, "duplicate replay sends zero turns");
	fs.rmSync(crashClaimPath, { force: true });
	await cancelGrant(crashGrantId, 2, crashJudgeSha);

	// 1c. Corrupt/Unreadable claim on disk blocks recovery (fails closed, 0 turns)
	const corruptCase = await createAndStartDisposableGrant("corrupt");
	const corruptClaimPath = path.join(pendingDir, "corrupt-claim.json");
	fs.writeFileSync(corruptClaimPath, "corrupt json{");
	const recoveryCorrupt = runHarness("recovery", corruptCase.item.key);
	assert.equal((recoveryCorrupt.sentMessages as unknown[])?.length, 0, "corrupt claim blocks recovery");
	assert.ok(
		(recoveryCorrupt.uiCalls as string[])?.some(c => c.includes("Recovery blocked by unreadable claim") && c.includes("corrupt-claim.json")),
		"refusal names the unreadable claim scan",
	);
	assert.equal(recoveryCorrupt.exec?.grant?.grant_version, 1, "corrupt claim leaves grant_version unchanged");
	assert.equal(recoveryCorrupt.exec?.grant?.continuations_scheduled, 0, "corrupt claim consumes no continuation");
	fs.rmSync(corruptClaimPath, { force: true });
	await cancelGrant(corruptCase.startOut.exec?.grant?.grant_id, corruptCase.startOut.exec?.grant?.grant_version, corruptCase.startOut.exec?.grant?.judge_sha256);

	// 2. Drift Probe: Dirty worktree sends zero turns
	const dirtyCase = await createAndStartDisposableGrant("dirty");
	fs.writeFileSync(path.join(probe, "drift-dirt.txt"), "dirt\n");
	const recoveryDirt = runHarness("recovery", dirtyCase.item.key);
	assert.equal((recoveryDirt.sentMessages as unknown[])?.length, 0, "dirty worktree sends zero turns");
	fs.rmSync(path.join(probe, "drift-dirt.txt"), { force: true });
	await cancelGrant(dirtyCase.startOut.exec?.grant?.grant_id, dirtyCase.startOut.exec?.grant?.grant_version, dirtyCase.startOut.exec?.grant?.judge_sha256);
	const headCase = await createAndStartDisposableGrant("head");
	fs.writeFileSync(path.join(probe, "drift-head.txt"), "head drift\n");
	Bun.spawnSync(["git", "add", "drift-head.txt"], { cwd: probe });
	Bun.spawnSync(["git", "commit", "-m", "drift head"], { cwd: probe });
	const recoveryHead = runHarness("recovery", headCase.item.key);
	assert.equal((recoveryHead.sentMessages as unknown[])?.length, 0, "changed HEAD sends zero turns");
	Bun.spawnSync(["git", "reset", "--hard", headCommit], { cwd: probe });
	await cancelGrant(headCase.startOut.exec?.grant?.grant_id, headCase.startOut.exec?.grant?.grant_version, headCase.startOut.exec?.grant?.judge_sha256);

	// 4. Drift Probe: Changed revision sends zero turns
	const revCase = await createAndStartDisposableGrant("revision");
	const newRevId = crypto.randomUUID();
	const revTitle = "Recovery Test revision";
	const revDescription = "changed revision description";
	const revContentSha = sha256Hex(canonicalJson({
		title: revTitle,
		description: revDescription,
		scope: "smoke",
		acceptance_criteria: [],
	}));
	const reviseRes = await (await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			operation_id: crypto.randomUUID(),
			command: {
				type: "revise_work",
				payload: {
					work_id: revCase.item.work_id,
					expected_revision_id: revCase.item.revision_id,
					revision: {
						revision_id: newRevId,
						work_id: revCase.item.work_id,
						revision_number: 2,
						title: revTitle,
						description: revDescription,
						scope: "smoke",
						acceptance_criteria: [],
						content_sha256: revContentSha,
						created_by: "test",
						created_at: new Date().toISOString(),
					},
				},
			},
		}),
	})).json();
	assert.equal(reviseRes.result?.type, "revise_work", "revision applied");
	const recoveryRev = runHarness("recovery", revCase.item.key);
	assert.equal((recoveryRev.sentMessages as unknown[])?.length, 0, "changed revision sends zero turns");
	await cancelGrant(revCase.startOut.exec?.grant?.grant_id, revCase.startOut.exec?.grant?.grant_version, revCase.startOut.exec?.grant?.judge_sha256);

	// 4b. Drift Probe: Project drift sends zero turns
	const proj2Id = crypto.randomUUID();
	psql(`INSERT INTO omp_work.projects(project_id, workspace_id, name, kind) VALUES ('${proj2Id}', '${WORKSPACE}', 'Other Project', 'surface');`);
	const projCase = await createAndStartDisposableGrant("project");
	psql(`UPDATE omp_work.work_items SET project_id='${proj2Id}' WHERE work_id='${projCase.item.work_id}';`);
	const recoveryProj = runHarness("recovery", projCase.item.key);
	assert.equal((recoveryProj.sentMessages as unknown[])?.length, 0, "project drift sends zero turns");
	await cancelGrant(projCase.startOut.exec?.grant?.grant_id, projCase.startOut.exec?.grant?.grant_version, projCase.startOut.exec?.grant?.judge_sha256);

	// 4c. Drift Probe: Projectless-to-project drift sends zero turns
	const nullProjBatch = await (await fetch(`${baseUrl}/v1/commands`, {
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
						client_ref: "smoke-nullproj-item",
						title: "Null Project Item",
						description: "Item without project",
						scope: "smoke",
						acceptance_criteria: [],
						state: "BACKLOG",
					}],
				},
			},
		}),
	})).json();
	const nullProjItem = nullProjBatch.result.items[0];
	const nullProjStartOut = runHarness("start-only", nullProjItem.key);
	assert.equal(nullProjStartOut.exec?.grant?.state, "active", "grant for null project item started");
	assert.equal(nullProjStartOut.exec?.activeItem?.project_id, null, "activeItem project_id is null");
	psql(`UPDATE omp_work.work_items SET project_id='${proj2Id}' WHERE work_id='${nullProjItem.work_id}';`);
	const recoveryNullProj = runHarness("recovery", nullProjItem.key);
	assert.equal((recoveryNullProj.sentMessages as unknown[])?.length, 0, "null-to-project drift sends zero turns");
	assert.equal(recoveryNullProj.exec?.grant?.grant_version, nullProjStartOut.exec?.grant?.grant_version, "grant version unchanged on null-to-project drift");
	assert.equal(recoveryNullProj.exec?.grant?.continuations_scheduled, nullProjStartOut.exec?.grant?.continuations_scheduled, "continuations_scheduled unchanged on null-to-project drift");
	assert.ok(
		(recoveryNullProj.uiCalls as string[])?.some(c => c.includes("project mismatch")),
		"refusal reports project mismatch",
	);
	await cancelGrant(nullProjStartOut.exec?.grant?.grant_id, nullProjStartOut.exec?.grant?.grant_version, nullProjStartOut.exec?.grant?.judge_sha256);

	// 5. Drift Probe: Blocker drift sends zero turns
	const blockerCase = await createAndStartDisposableGrant("blocker");
	const blockerItemRes = await (await fetch(`${baseUrl}/v1/commands`, {
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
						client_ref: "smoke-blocker-item",
						title: "Active Blocker Item",
						description: "Blocks blockerCase item",
						scope: "smoke",
						acceptance_criteria: [],
						state: "BACKLOG",
						project_id: PROJECT,
					}],
				},
			},
		}),
	})).json();
	const blockerItem = blockerItemRes.result.items[0];
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
				type: "put_relation",
				payload: {
					relation: {
						workspace_id: WORKSPACE,
						source_work_id: blockerItem.work_id,
						target_work_id: blockerCase.item.work_id,
						kind: "blocks",
						active: true,
					},
				},
			},
		}),
	})).json();
	const recoveryBlocker = runHarness("recovery", blockerCase.item.key);
	assert.equal((recoveryBlocker.sentMessages as unknown[])?.length, 0, "active blocker sends zero turns");
	await cancelGrant(blockerCase.startOut.exec?.grant?.grant_id, blockerCase.startOut.exec?.grant?.grant_version, blockerCase.startOut.exec?.grant?.judge_sha256);
	// 6. Drift Probe: Missing yield-assembly source sends zero turns through recovery
	const yieldCase = await createAndStartDisposableGrant("yield");
	const recoveryMissingYield = runHarness("recovery", yieldCase.item.key, { OMP_WORK_SMOKE_MISSING_YIELD: "1" });
	assert.equal((recoveryMissingYield.sentMessages as unknown[])?.length, 0, "missing yield-assembly sends zero turns");
	await cancelGrant(yieldCase.startOut.exec?.grant?.grant_id, yieldCase.startOut.exec?.grant?.grant_version, yieldCase.startOut.exec?.grant?.judge_sha256);
	// 7. Continuation Cap Exhaustion: Schedule up to max_continuations (8) then fresh-process recovery while active at cap triggers terminalization
	const capBatch = await (await fetch(`${baseUrl}/v1/commands`, {
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
					items: [
						{
							client_ref: "smoke-cap-item-1",
							title: "Cap Item 1",
							description: "Cap item 1 description",
							scope: "surface",
							state: "BACKLOG",
							project_id: PROJECT,
						},
						{
							client_ref: "smoke-cap-item-2",
							title: "Cap Item 2",
							description: "Cap item 2 description",
							scope: "surface",
							state: "BACKLOG",
							project_id: PROJECT,
						},
					],
				},
			},
		}),
	})).json();
	const capItem1 = capBatch.result.items[0];
	const capItem2 = capBatch.result.items[1];
	const capStartOut = runHarness("start-only", `${capItem1.key} --queue`);
	assert.equal(capStartOut.exec?.grant?.state, "active", "queue grant started");
	assert.ok(capStartOut.exec?.items?.length >= 2, "queue grant contains at least 2 items");
	assert.equal(capStartOut.exec?.items?.find(i => i.work_id === capItem1.work_id)?.phase, "criteria_pending", "item 1 is criteria_pending");
	assert.equal(capStartOut.exec?.items?.find(i => i.work_id === capItem2.work_id)?.phase, "pending", "item 2 is pending");

	let grantVerCap = capStartOut.exec?.grant?.grant_version ?? 1;
	let scheduledCap = capStartOut.exec?.grant?.continuations_scheduled ?? 0;
	const capJudgeSha = capStartOut.exec?.grant?.judge_sha256;
	while (scheduledCap < 8) {
		const incRes = await (await fetch(`${baseUrl}/v1/commands`, {
			method: "POST",
			headers,
			body: JSON.stringify({
				api_version: "work.omp.dev/v1",
				workspace_id: WORKSPACE,
				request_id: crypto.randomUUID(),
				correlation_id: crypto.randomUUID(),
				operation_id: crypto.randomUUID(),
				command: {
					type: "set_execution_state",
					payload: {
						grant_id: capStartOut.exec?.grant?.grant_id,
						target_state: "active",
						expected_grant_version: grantVerCap,
						judge_sha256: capJudgeSha,
					},
				},
			}),
		})).json();
		assert.equal(incRes.result?.type, "set_execution_state", "continuation scheduled");
		grantVerCap = incRes.result.grant.grant_version;
		scheduledCap = incRes.result.grant.continuations_scheduled;
	}
	assert.equal(scheduledCap, 8, "continuations scheduled reached cap of 8");

	// Recovery while active at the cap calls set_execution_state which atomically stops and cleans up
	const recoveryCap = runHarness("recovery", capItem1.key);
	assert.equal((recoveryCap.sentMessages as unknown[])?.length, 0, "recovery while active at cap sends zero turns");

	assert.equal(recoveryCap.exec?.grant?.state, "stopped", "grant stopped on cap exhaustion during recovery");
	assert.equal(recoveryCap.exec?.grant?.terminal_reason, "max_continuations_exceeded", "terminal reason recorded");
	assert.ok(recoveryCap.exec?.grant?.stopped_at, "stopped_at is set on cap exhaustion");
	const capActiveItem = recoveryCap.exec?.items?.find(i => i.work_id === capItem1.work_id);
	assert.equal(capActiveItem?.phase, "abandoned", "active item abandoned on cap exhaustion");
	const capPendingItems = recoveryCap.exec?.items?.filter(i => i.work_id !== capItem1.work_id) ?? [];
	assert.ok(capPendingItems.length > 0, "pending items present in queue grant");
	assert.ok(capPendingItems.every(i => i.phase === "skipped" && i.terminal_reason === "grant_stopped"), "all pending items skipped on cap exhaustion with grant_stopped");
	const capFocusRes = await (await fetch(`${baseUrl}/v1/workspaces/${WORKSPACE}/focus/${OWNER}`, { headers })).json();
	assert.equal(capFocusRes.work_id, null, "focus slot is cleared on cap exhaustion");
	// Test Scenario 8: Non-main branch push binding success & negative refusal
	const nonMainCase = await createAndStartDisposableGrant("non-main", "release/omp-180-smoke");
	const grantNonMainId = nonMainCase.startOut.exec?.grant?.grant_id;
	const nonMainJudgeSha = nonMainCase.startOut.exec?.grant?.judge_sha256;
	assert.equal(nonMainCase.startOut.exec?.grant?.remote_ref, "refs/heads/release/omp-180-smoke", "grant binds non-main branch");
	// 1. Seal criteria
	const sealNonMain = await (await fetch(`${baseUrl}/v1/commands`, {
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
					grant_id: grantNonMainId,
					expected_grant_version: 1,
					work_id: nonMainCase.item.work_id,
					expected_revision_id: nonMainCase.item.revision_id,
					criteria: ["AC-1: deliver on release branch"],
					description_sha256: sha256Hex("Verify recovery isolation for non-main"),
					judge_sha256: nonMainJudgeSha,
				},
			},
		}),
	})).json();
	assert.equal(sealNonMain.result?.type, "seal_execution_criteria");
	const revNonMainId = sealNonMain.result.item.criteria_revision_id;

	// 2. Stamp plan
	const planBodyNonMain = "## Approach\n1. Write branch feature\n\n## Verification\n1. Verify branch feature\n";
	const planShaNonMain = sha256Hex(planBodyNonMain);
	const plannedCandNonMainId = crypto.randomUUID();
	const candShaNonMain = sha256Hex(JSON.stringify({ candidateId: plannedCandNonMainId, planSha: planShaNonMain }));
	const finalTreeShaNonMain = sha256Hex("final-content-non-main");
	const stampNonMain = await (await fetch(`${baseUrl}/v1/commands`, {
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
					grant_id: grantNonMainId,
					expected_grant_version: 2,
					work_id: nonMainCase.item.work_id,
					revision_id: revNonMainId,
					candidate_id: plannedCandNonMainId,
					plan_file: "local://execute-plan.md",
					plan_body: planBodyNonMain,
					plan_sha256: planShaNonMain,
					approach: ["Write branch feature"],
					verification: ["Verify branch feature"],
					paths: ["src/branch_feat.ts"],
					candidate_sha256: candShaNonMain,
					judge_sha256: nonMainJudgeSha,
				},
			},
		}),
	})).json();
	assert.equal(stampNonMain.result?.type, "stamp_execution_plan");

	// 3. Finalize candidate
	const finalCandNonMainId = crypto.randomUUID();
	const finalCandNonMain = await (await fetch(`${baseUrl}/v1/commands`, {
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
					work_id: nonMainCase.item.work_id,
					revision_id: revNonMainId,
					planned_candidate_id: plannedCandNonMainId,
					candidate_id: finalCandNonMainId,
					candidate_sha256: finalTreeShaNonMain,
					commit_sha: headCommit,
				},
			},
		}),
	})).json();
	assert.equal(finalCandNonMain.result?.type, "finalize_candidate");

	// 4. Append verification receipt
	const verifReceiptNonMainId = crypto.randomUUID();
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
						receipt_id: verifReceiptNonMainId,
						work_id: nonMainCase.item.work_id,
						revision_id: revNonMainId,
						candidate_id: finalCandNonMainId,
						kind: "verification",
						payload: { body: "branch verification passed" },
						payload_sha256: sha256Hex(JSON.stringify({ body: "branch verification passed" })),
						issuer: "test",
						issued_at: new Date().toISOString(),
						candidate_sha256: finalTreeShaNonMain,
						candidate_commit: headCommit,
						independent: false,
					},
				},
			},
		}),
	})).json();

	// 5. Begin close attempt & settle PASS
	const attemptNonMainId = crypto.randomUUID();
	const beginAttemptNonMain = await (await fetch(`${baseUrl}/v1/commands`, {
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
					attempt_id: attemptNonMainId,
					work_id: nonMainCase.item.work_id,
					authorization_ref: `execution:${grantNonMainId}:0:1`,
					owner_session_id: "smoke-non-main",
					owner_session_started_at: new Date().toISOString(),
					owner_session_start_commit: headCommit,
					repository: "repo",
					diff_sha256: "0".repeat(64),
					starting_dirty_paths: [],
					authorization_kind: "execution",
					execution_grant_id: grantNonMainId,
					candidate_tree_sha: finalTreeShaNonMain,
					original_request_sha256: stampNonMain.result.item.original_request_sha256,
					criteria_sha256: stampNonMain.result.item.criteria_sha256,
					plan_stamp_sha256: stampNonMain.result.item.plan_stamp_sha256,
					judge_sha256: nonMainJudgeSha,
					riders: [],
				},
			},
		}),
	})).json();
	assert.equal(beginAttemptNonMain.result?.status, "applied");
	const manifestNonMain = await (await fetch(`${baseUrl}/v1/commands`, {
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
					attempt_id: attemptNonMainId,
					verification_receipt_id: verifReceiptNonMainId,
				},
			},
		}),
	})).json();
	assert.equal(manifestNonMain.result?.type, "seal_audit_manifest");

	const launchNonMain = await (await fetch(`${baseUrl}/v1/commands`, {
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
					attempt_id: attemptNonMainId,
					task_sha256: manifestNonMain.result.manifest.task_sha256,
					tool_call_id: "launch-non-main-1",
				},
			},
		}),
	})).json();
	assert.equal(launchNonMain.result?.type, "reserve_auditor_launch");

	const settleNonMain = await (await fetch(`${baseUrl}/v1/commands`, {
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
					attempt_id: attemptNonMainId,
					launch_id: launchNonMain.result.launch.launch_id,
					transport_payload: {
						report: "VERDICT: PASS\n\nFINDINGS\n(none)\n\nACCEPTANCE COVERAGE\nAC-1 deliver on release branch\n\nOUT OF SCOPE\nnone\n\nCHECKS RUN\nbun test\n\nREMAINING QUESTIONS\nnone",
					},
					transport_failed: false,
				},
			},
		}),
	})).json();
	assert.equal(settleNonMain.result?.status, "applied");

	// Attest deliveries
	for (const ev of [beginAttemptNonMain.result.event, settleNonMain.result.event]) {
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
					type: "attest_checkpoint_delivery",
					payload: {
						event_id: ev.event_id,
						owner_session_id: "smoke-non-main",
						rendered_sha256: ev.rendered_sha256,
						status: "delivered",
					},
				},
			}),
		})).json();
	}

	// 6. Negative probe: push receipt with wrong remote_ref fails completion
	const wrongPushId = crypto.randomUUID();
	const wrongPushPayload = {
		repository: "repo",
		remote_url: remote,
		remote_ref: "refs/heads/wrong",
		prior_tip: headCommit,
		candidate_commit: headCommit,
		result_tip: headCommit,
	};
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
						receipt_id: wrongPushId,
						work_id: nonMainCase.item.work_id,
						revision_id: revNonMainId,
						candidate_id: finalCandNonMainId,
						kind: "push",
						payload: wrongPushPayload,
						payload_sha256: sha256Hex(canonicalJson(wrongPushPayload)),
						issuer: "test",
						issued_at: new Date().toISOString(),
						independent: false,
						remote_ref: "refs/heads/wrong",
						remote_commit: headCommit,
						candidate_commit: headCommit,
						candidate_sha256: finalTreeShaNonMain,
					},
				},
			},
		}),
	})).json();

	const badNonMainComplete = await (await fetch(`${baseUrl}/v1/commands`, {
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
					grant_id: grantNonMainId,
					expected_grant_version: 3,
					work_id: nonMainCase.item.work_id,
					attempt_id: attemptNonMainId,
					push_receipt_id: wrongPushId,
					judge_sha256: nonMainJudgeSha,
				},
			},
		}),
	})).json();
	assert.equal(badNonMainComplete.error?.code, "completion_blocked", "wrong branch push receipt blocked");
	assert.ok(badNonMainComplete.error?.diagnostics?.[0]?.includes("push receipt remote_ref mismatch"), "diagnostics cite remote_ref mismatch");

	// 7. Positive probe: pushCandidate pushes to release/omp-180-smoke and verifies remote
	const pushOutcome = pushCandidate(probe, headCommit, headCommit);
	assert.equal(pushOutcome.status === "pushed" || pushOutcome.status === "remote_commit", true, "pushCandidate succeeds on non-main branch");
	assert.equal(pushOutcome.remoteRef, "refs/heads/release/omp-180-smoke", "pushCandidate targeted release branch");
	const lsRemote = Bun.spawnSync(["git", "ls-remote", "origin", "refs/heads/release/omp-180-smoke"], { cwd: probe });
	assert.ok(lsRemote.stdout.toString().includes(headCommit), "remote origin holds commit at release branch ref");

	const correctPushId = crypto.randomUUID();
	const correctPushPayload = {
		repository: "repo",
		remote_url: pushOutcome.remoteUrl ?? remote,
		remote_ref: pushOutcome.remoteRef,
		prior_tip: pushOutcome.priorTip ?? headCommit,
		candidate_commit: headCommit,
		result_tip: pushOutcome.remoteCommit ?? headCommit,
	};
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
						receipt_id: correctPushId,
						work_id: nonMainCase.item.work_id,
						revision_id: revNonMainId,
						candidate_id: finalCandNonMainId,
						kind: "push",
						payload: correctPushPayload,
						payload_sha256: sha256Hex(canonicalJson(correctPushPayload)),
						issuer: "test",
						issued_at: new Date().toISOString(),
						independent: false,
						remote_ref: "refs/heads/release/omp-180-smoke",
						remote_commit: headCommit,
						candidate_commit: headCommit,
						candidate_sha256: finalTreeShaNonMain,
					},
				},
			},
		}),
	})).json();

	const goodNonMainComplete = await (await fetch(`${baseUrl}/v1/commands`, {
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
					grant_id: grantNonMainId,
					expected_grant_version: 3,
					work_id: nonMainCase.item.work_id,
					attempt_id: attemptNonMainId,
					push_receipt_id: correctPushId,
					judge_sha256: nonMainJudgeSha,
				},
			},
		}),
	})).json();
	assert.equal(goodNonMainComplete.result?.type, "complete_execution_item", "matching non-main push completion succeeded");
	assert.equal(goodNonMainComplete.result?.grant?.state, "completed", "non-main grant completed");

	const nonMainFinal = (await (await fetch(`${baseUrl}/v1/work-items/${nonMainCase.item.key}/workflow`, { headers })).json()) as { item: { state: string } };
	assert.equal(nonMainFinal.item.state, "DONE", "non-main item is DONE");

	Bun.spawnSync(["git", "checkout", "main"], { cwd: probe });
	Bun.spawnSync(["git", "branch", "-D", "release/omp-180-smoke"], { cwd: probe });
	// Verify audit receipt count remained unchanged after tamper attempts
	const postTamperView = (await (await fetch(`${baseUrl}/v1/work-items/${item1.key}/workflow`, { headers })).json()) as {
		receipts: { kind: string }[];
	};
	const postTamperAuditReceipts = postTamperView.receipts.filter(r => r.kind === "audit").length;
	assert.equal(postTamperAuditReceipts, initialAuditReceipts, "audit receipt count unchanged after tamper attempts");

	console.log("execute-cycle-smoke: PASS (clean preflight, single execution cycle with NEEDS_FIX remediation, queue mode, four tamper scenarios, negative & positive remote push verification, contract change pause gate, startup recovery & drift probes)");
} finally {
	cleanup();
}
