// Real boundary smoke test for durable preflight intent (OMP-233).
// Proves:
// 1. Two independent backends/correlation IDs beginning the same logical identity yield one begun row.
// 2. Second backend fails closed (replayed begun intent mandates recovery, zero probes).
// 3. After first terminal failure settlement, replay exposes terminal evidence.
// 4. Next ordinal in the group can begin.
// Runs only with disposable PostgreSQL/service:
//   OMP_WORK_POSTGRES_INTEGRATION=1 bun run session-system/tests/preflight-intent-smoke.ts

import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { WORK_CONTRACT_SHA256 } from "@oh-my-pi/pi-work-client";
import { createWorkBackend } from "../extensions/workflow/work";

if (process.env.OMP_WORK_POSTGRES_INTEGRATION !== "1") {
	console.log("preflight-intent-smoke: skipped (set OMP_WORK_POSTGRES_INTEGRATION=1; needs postgres/docker)");
	process.exit(0);
}

const WORKSPACE = "00000000-0000-4000-8000-0000000000aa";
const OWNER = "00000000-0000-4000-8000-0000000000bb";
const PROJECT = "00000000-0000-4000-8000-0000000000cc";
const PROJECT_NAME = "Preflight Intent Smoke";

const repoRoot = path.resolve(import.meta.dir, "../..");
const pythonDir = path.join(repoRoot, "python/omp-work");

function freePort(): number {
	const probe = Bun.listen({ hostname: "127.0.0.1", port: 0, socket: { data: () => {} } });
	const port = probe.port;
	probe.stop(true);
	return port;
}

const root = fs.mkdtempSync(path.join(os.tmpdir(), "omp-preflight-smoke-"));
const xdg = path.join(root, "xdg");
fs.mkdirSync(path.join(xdg, "omp"), { recursive: true });

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

process.on("exit", cleanup);
process.on("SIGINT", () => { cleanup(); process.exit(1); });
process.on("SIGTERM", () => { cleanup(); process.exit(1); });

try {
	console.log("Starting disposable PostgreSQL on port", pgPort);
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

	const runPg = Bun.spawnSync(["pg_ctl", "-D", pgData, "-w", "-l", path.join(root, "pg.log"), "-o", `-p ${pgPort} -k ${root} -c listen_addresses=127.0.0.1`, "start"], { stderr: "pipe" });
	if (runPg.exitCode !== 0) throw new Error(`pg_ctl start failed: ${runPg.stderr.toString()}`);

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

	const shaRun = Bun.spawnSync(["uv", "run", "python", "-c", "from omp_work.operations.database import migration_set_sha256; print(migration_set_sha256(), end='')"], { cwd: pythonDir });
	if (shaRun.exitCode !== 0) throw new Error(`migration_set_sha256 failed: ${shaRun.stderr.toString()}`);
	const migrationSetSha = shaRun.stdout.toString().trim();
	psqlAdmin(`INSERT INTO omp_control.runtime_compatibility (contract_version, contract_sha256, migration_set_sha256, postgres_major) VALUES ('work.omp.dev/v1', '${WORK_CONTRACT_SHA256}', '${migrationSetSha}', 18) ON CONFLICT (singleton) DO UPDATE SET contract_version=EXCLUDED.contract_version, contract_sha256=EXCLUDED.contract_sha256, migration_set_sha256=EXCLUDED.migration_set_sha256, postgres_major=EXCLUDED.postgres_major;`, "omp_work");

	py(["ops", "capabilities", "init", "--workspace-id", WORKSPACE, "--owner-id", OWNER, "--base-url", baseUrl]);

	const psql = (sql: string) => {
		const res = Bun.spawnSync(["psql", "-h", "127.0.0.1", "-p", String(pgPort), "-U", "postgres", "-d", "omp_work", "-v", "ON_ERROR_STOP=1", "-c", sql], {
			env: { ...process.env, PGPASSWORD: pgSecret },
		});
		if (res.exitCode !== 0) throw new Error(`psql failed: ${res.stderr.toString()}`);
	};

	psql(`INSERT INTO omp_control.workspaces(workspace_id) VALUES ('${WORKSPACE}') ON CONFLICT DO NOTHING; INSERT INTO omp_work.projects(project_id, workspace_id, name, kind) VALUES ('${PROJECT}', '${WORKSPACE}', '${PROJECT_NAME}', 'surface');`);
	psql(`INSERT INTO omp_control.cutover_epochs(workspace_id, epoch_id, state, candidate_manifest, candidate_manifest_sha256) VALUES ('${WORKSPACE}', '00000000-0000-4000-8000-0000000000dd', 'sealed', '{}'::jsonb, '${"0".repeat(64)}') ON CONFLICT DO NOTHING; INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id) VALUES ('${WORKSPACE}', '00000000-0000-4000-8000-0000000000dd') ON CONFLICT DO NOTHING;`);

	// Start WorkService
	console.log("Starting WorkService on port", httpPort);
	const serverScript = `
import sys
from pathlib import Path
import uvicorn
from omp_work.operations.config import OperationsConfig
from omp_work.v1.server import create_app

config = OperationsConfig.defaults()
caps_dir = Path(sys.argv[1])
port = int(sys.argv[2])
app = create_app(config, capabilities_dir=caps_dir)
uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)
`;

	service = Bun.spawn(
		["uv", "run", "python", "-c", serverScript, path.join(xdg, "omp/work-ledger/capabilities"), String(httpPort)],
		{
			cwd: pythonDir,
			env: { ...process.env, XDG_CONFIG_HOME: xdg, XDG_STATE_HOME: xdg, XDG_DATA_HOME: xdg, OMP_WORK_POSTGRES_PORT: String(pgPort) },
			stderr: "inherit",
		},
	);

	for (let attempt = 0; attempt < 60; attempt++) {
		try {
			if ((await fetch(`${baseUrl}/v1/health/live`)).ok) break;
		} catch {
			// wait for service
		}
		if (attempt === 59) throw new Error("service never became live");
		await Bun.sleep(500);
	}
	console.log("WorkService is live!");

	const token = (JSON.parse(fs.readFileSync(path.join(xdg, "omp/work-ledger/capabilities/owner.json"), "utf8")) as { token: string }).token;
	const tokenProvider = () => token;

	// Create work item
	const headers = {
		authorization: `Bearer ${token}`,
		"X-OMP-Workspace-ID": WORKSPACE,
		"X-OMP-Contract-SHA256": WORK_CONTRACT_SHA256,
		"Content-Type": "application/json",
	};

	const createOp = crypto.randomUUID();
	const createRes = await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			operation_id: createOp,
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			command: {
				type: "create_work_batch",
				payload: {
					items: [{ client_ref: "smoke-item-1", title: "Preflight Smoke Item" }],
					relations: [],
				},
			},
		}),
	});
	assert.equal(createRes.status, 200, "work item created");
	const createData = (await createRes.json()) as { result: { items: { work_id: string }[] } };
	const workId = createData.result.items[0].work_id;
	console.log("Created work item:", workId);

	// Setup two independent backends with distinct correlation IDs
	const backend1 = createWorkBackend(
		{ baseUrl, workspaceId: WORKSPACE, ownerId: OWNER },
		tokenProvider,
		undefined,
		path.join(root, "pending-1"),
	);

	const backend2 = createWorkBackend(
		{ baseUrl, workspaceId: WORKSPACE, ownerId: OWNER },
		tokenProvider,
		undefined,
		path.join(root, "pending-2"),
	);

	const logicalIdentity = {
		work_id: workId,
		role: "implement" as const,
		tool_call_id: "tool-smoke-call-1",
		task_sha256: "a".repeat(64),
		probe_sha256: "b".repeat(64),
		ordinal: 0,
		requested_selector: "gemini:gemini-3.8-flash",
		requested_provider: "gemini",
		requested_model: "gemini-3.8-flash",
		requested_api: "google-genai",
		requested_effort: "medium",
		requested_wire_model: "gemini-3.8-flash-preview",
		is_fallback: false,
	};

	// Wire boundary regression: sending session_id in begin_stage_preflight MUST fail with HTTP 400
	console.log("Testing wire boundary rejection of forbidden session_id...");
	const forbiddenRes = await fetch(`${baseUrl}/v1/commands`, {
		method: "POST",
		headers,
		body: JSON.stringify({
			api_version: "work.omp.dev/v1",
			workspace_id: WORKSPACE,
			operation_id: crypto.randomUUID(),
			request_id: crypto.randomUUID(),
			correlation_id: crypto.randomUUID(),
			command: {
				type: "begin_stage_preflight",
				payload: {
					...logicalIdentity,
					session_id: "forbidden-session-id",
				},
			},
		}),
	});
	assert.equal(forbiddenRes.status, 400, "begin_stage_preflight with session_id fails with HTTP 400");
	const forbiddenBody = await forbiddenRes.text();
	assert.ok(forbiddenBody.includes("session_id"), "error body mentions session_id");
	console.log("Verified wire boundary rejects begin_stage_preflight with session_id with HTTP 400");

	// 1. Two-host lost-begin cancellation and reissue:
	// Backend 1 begins intent for ordinal 0
	console.log("Backend 1 beginning intent for ordinal 0...");
	const begin1 = await backend1.beginStagePreflight(logicalIdentity);
	assert.equal(begin1.status, "applied", "backend 1 intent applied");
	assert.equal(begin1.intent.status, "begun", "backend 1 intent status is begun");
	assert.ok(begin1.intent.host_owner_id, "server minted host_owner_id on intent");
	const transportAttemptId0 = begin1.intent.transport_attempt_id;
	const logicalSha256_0 = begin1.intent.logical_sha256;
	console.log("Minted transport attempt ID:", transportAttemptId0);
	console.log("Server-minted host owner ID:", begin1.intent.host_owner_id);

	// Backend 2 begins same logical identity -> replayed begun intent
	console.log("Backend 2 beginning same logical identity (lost begin response recovery)...");
	const begin2 = await backend2.beginStagePreflight(logicalIdentity);
	assert.equal(begin2.status, "replayed", "backend 2 receives replayed intent");
	assert.equal(begin2.intent.status, "begun", "replayed intent status is begun");
	assert.equal(begin2.intent.transport_attempt_id, transportAttemptId0, "replayed intent has same transport attempt ID");
	assert.equal(begin2.intent.host_owner_id, begin1.intent.host_owner_id, "replayed intent preserves original host_owner_id");
	assert.equal(begin2.preflight, null, "replayed begun intent returns no preflight");

	// Verify exactly 1 row in database
	const psqlQuery = (sql: string) => {
		const res = Bun.spawnSync(["psql", "-h", "127.0.0.1", "-p", String(pgPort), "-U", "postgres", "-d", "omp_work", "-t", "-A", "-c", sql], {
			env: { ...process.env, PGPASSWORD: pgSecret },
		});
		if (res.exitCode !== 0) throw new Error(`psql failed: ${res.stderr.toString()}`);
		return res.stdout.toString().trim();
	};

	const rowCount = parseInt(psqlQuery(`SELECT COUNT(*) FROM omp_work.stage_preflight_intents WHERE workspace_id = '${WORKSPACE}' AND transport_attempt_id = '${transportAttemptId0}';`), 10);
	assert.equal(rowCount, 1, "exactly one row exists in database for this intent");
	console.log("Verified exactly 1 row in database for this intent");

	// Backend 2 cancels the replayed begun intent
	console.log("Backend 2 cancelling begun intent...");
	const cancel0 = await backend2.cancelStagePreflight({
		transport_attempt_id: transportAttemptId0,
		logical_sha256: logicalSha256_0,
		reason: "lost begin response recovery cancel",
	});
	assert.equal(cancel0.status, "applied", "cancel status applied");
	assert.equal(cancel0.intent.status, "cancelled_undispatched", "intent is cancelled_undispatched");
	console.log("Verified Backend 2 cancelled begun intent");

	// Backend 1 cannot admit cancelled intent (fails closed with HTTP 400 preflight_intent_cancelled)
	let admitCancelledFailed = false;
	try {
		await backend1.admitStagePreflight({
			transport_attempt_id: transportAttemptId0,
			logical_sha256: logicalSha256_0,
		});
	} catch (err: any) {
		admitCancelledFailed = true;
		assert.ok(String(err).includes("preflight_intent_cancelled"), "admit error mentions preflight_intent_cancelled");
	}
	assert.ok(admitCancelledFailed, "admit on cancelled intent must fail closed");
	console.log("Verified admit on cancelled intent fails closed");

	// Backend 2 reissues next ordinal (ordinal 1) in the group
	console.log("Backend 2 reissuing next ordinal 1 in group...");
	const logicalIdentityOrdinal1 = {
		...logicalIdentity,
		ordinal: 1,
		requested_provider: "anthropic",
		requested_model: "claude-3-7-sonnet",
		requested_wire_model: "claude-3-7-sonnet-20250219",
	};
	const begin1Next = await backend2.beginStagePreflight(logicalIdentityOrdinal1);
	assert.equal(begin1Next.status, "applied", "next ordinal begins successfully");
	assert.equal(begin1Next.intent.status, "begun", "next ordinal intent is begun");
	assert.equal(begin1Next.intent.ordinal, 1, "next ordinal is 1");
	const transportAttemptId1 = begin1Next.intent.transport_attempt_id;
	const logicalSha256_1 = begin1Next.intent.logical_sha256;
	console.log("Verified next ordinal 1 begun after cancellation");

	// 2. Valid begin -> admit -> record path:
	// Backend 2 admits ordinal 1
	console.log("Backend 2 admitting ordinal 1...");
	const admit1 = await backend2.admitStagePreflight({
		transport_attempt_id: transportAttemptId1,
		logical_sha256: logicalSha256_1,
	});
	assert.equal(admit1.status, "applied", "admit status applied");
	assert.equal(admit1.intent.status, "dispatched", "intent status is dispatched");
	console.log("Verified Backend 2 admitted ordinal 1");

	// Backend 2 records ordinal 1 terminal failure
	console.log("Backend 2 settling ordinal 1 with terminal failure...");
	const record1 = await backend2.recordStagePreflight({
		...logicalIdentityOrdinal1,
		transport_attempt_id: transportAttemptId1,
		outcome: "failed",
		stop_reason: "error",
		error: "probe endpoint connection timeout",
		requests: 1,
		usage: null,
		provider_request_id: "req-smoke-fail",
	});
	assert.equal(record1.outcome, "failed", "preflight outcome recorded as failed");
	assert.equal(record1.transport_attempt_id, transportAttemptId1, "preflight matches transport attempt ID");
	console.log("Verified Backend 2 recorded terminal preflight");

	// Replay after settle exposes terminal evidence
	const replayAfterSettle = await backend1.beginStagePreflight(logicalIdentityOrdinal1);
	assert.equal(replayAfterSettle.status, "replayed", "replay returns replayed status");
	assert.equal(replayAfterSettle.intent.status, "settled", "intent status is settled");
	assert.ok(replayAfterSettle.preflight, "terminal preflight evidence is returned on replay");
	assert.equal(replayAfterSettle.preflight?.outcome, "failed", "terminal preflight has failed outcome");
	assert.equal(replayAfterSettle.preflight?.transport_attempt_id, transportAttemptId1, "terminal preflight matches transport attempt ID");
	console.log("Verified terminal evidence exposed on replay after settle");

	// 3. Dispatched uncertainty blocks second probe:
	// Backend 1 begins ordinal 2 in same group
	console.log("Backend 1 beginning ordinal 2 in group...");
	const logicalIdentityOrdinal2 = {
		...logicalIdentity,
		ordinal: 2,
		requested_provider: "openai",
		requested_model: "gpt-4o",
		requested_wire_model: "gpt-4o",
	};
	const begin2Next = await backend1.beginStagePreflight(logicalIdentityOrdinal2);
	assert.equal(begin2Next.status, "applied", "ordinal 2 begins successfully");
	assert.equal(begin2Next.intent.status, "begun", "ordinal 2 is begun");
	const transportAttemptId2 = begin2Next.intent.transport_attempt_id;
	const logicalSha256_2 = begin2Next.intent.logical_sha256;

	// Backend 1 admits ordinal 2
	console.log("Backend 1 admitting ordinal 2...");
	const admit2 = await backend1.admitStagePreflight({
		transport_attempt_id: transportAttemptId2,
		logical_sha256: logicalSha256_2,
	});
	assert.equal(admit2.status, "applied", "ordinal 2 admit applied");
	assert.equal(admit2.intent.status, "dispatched", "ordinal 2 is dispatched");

	// Backend 2 (second host / replay) observes ordinal 2 as dispatched
	console.log("Backend 2 observing ordinal 2 as dispatched...");
	const replayDispatched = await backend2.beginStagePreflight(logicalIdentityOrdinal2);
	assert.equal(replayDispatched.status, "replayed", "replayed status");
	assert.equal(replayDispatched.intent.status, "dispatched", "intent status is dispatched");

	// Cancel on dispatched intent is refused (HTTP 409 preflight_intent_active)
	console.log("Verifying cancel on dispatched intent is refused...");
	let cancelDispatchedFailed = false;
	try {
		await backend2.cancelStagePreflight({
			transport_attempt_id: transportAttemptId2,
			logical_sha256: logicalSha256_2,
			reason: "try cancel dispatched",
		});
	} catch (err: any) {
		cancelDispatchedFailed = true;
		assert.equal(err.code, "preflight_intent_active", "cancel error code is preflight_intent_active");
		assert.equal(err.status, 409, "cancel error status is 409");
	}
	assert.ok(cancelDispatchedFailed, "cancel on dispatched intent must fail");
	console.log("Verified cancel on dispatched intent refused");

	// Next ordinal (ordinal 3) is blocked while ordinal 2 is dispatched (group sibling active)
	console.log("Verifying next ordinal 3 is blocked while ordinal 2 is dispatched...");
	let nextOrdinalBlocked = false;
	try {
		const logicalIdentityOrdinal3 = {
			...logicalIdentity,
			ordinal: 3,
			requested_provider: "anthropic",
			requested_model: "claude-3-5-sonnet",
			requested_wire_model: "claude-3-5-sonnet-20241022",
		};
		await backend2.beginStagePreflight(logicalIdentityOrdinal3);
	} catch (err: any) {
		nextOrdinalBlocked = true;
		assert.ok(String(err).includes("preflight_group_sibling_active"), "begin error mentions preflight_group_sibling_active");
	}
	assert.ok(nextOrdinalBlocked, "next ordinal must be blocked while sibling is dispatched");
	console.log("Verified next ordinal blocked (zero second probes possible absent provider reconciliation)");

	// Backend 1 completes ordinal 2 as selected terminal route
	console.log("Backend 1 recording terminal route for ordinal 2...");
	const record2 = await backend1.recordStagePreflight({
		...logicalIdentityOrdinal2,
		transport_attempt_id: transportAttemptId2,
		outcome: "selected",
		stop_reason: "stop",
		requests: 1,
		usage: null,
		provider_request_id: "req-smoke-ok",
	});
	assert.equal(record2.outcome, "selected", "ordinal 2 recorded as selected");
	console.log("Verified Backend 1 settled ordinal 2 as selected");

	console.log("preflight-intent-smoke: ALL REAL BOUNDARY ASSERTIONS PASSED!");
} finally {
	cleanup();
}
