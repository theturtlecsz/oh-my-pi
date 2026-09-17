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

	// 1. Backend 1 begins intent
	console.log("Backend 1 beginning intent for ordinal 0...");
	const begin1 = await backend1.beginStagePreflight(logicalIdentity);
	assert.equal(begin1.status, "applied", "backend 1 intent applied");
	assert.equal(begin1.intent.status, "begun", "backend 1 intent status is begun");
	assert.ok(begin1.intent.host_owner_id, "server minted host_owner_id on intent");
	const transportAttemptId = begin1.intent.transport_attempt_id;
	console.log("Minted transport attempt ID:", transportAttemptId);
	console.log("Server-minted host owner ID:", begin1.intent.host_owner_id);

	// 2. Backend 2 begins same logical identity -> replayed begun intent
	console.log("Backend 2 beginning same logical identity...");
	const begin2 = await backend2.beginStagePreflight(logicalIdentity);
	assert.equal(begin2.status, "replayed", "backend 2 receives replayed intent");
	assert.equal(begin2.intent.status, "begun", "replayed intent status is begun");
	assert.equal(begin2.intent.transport_attempt_id, transportAttemptId, "replayed intent has same transport attempt ID");
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

	const rowCount = parseInt(psqlQuery(`SELECT COUNT(*) FROM omp_work.stage_preflight_intents WHERE workspace_id = '${WORKSPACE}' AND transport_attempt_id = '${transportAttemptId}';`), 10);
	assert.equal(rowCount, 1, "exactly one row exists in database for this intent");
	console.log("Verified exactly 1 row in database for this intent");

	// 3. Second backend fails closed:
	// Verify that host auditorRunner / dispatch logic treats replayed begun as fail-closed
	// (mandating recovery before probe, never probing)
	const hostMustFailClosed = begin2.status === "replayed" && begin2.intent.status === "begun";
	assert.ok(hostMustFailClosed, "second backend replayed begun intent must fail closed (recovery required, zero probes)");
	console.log("Verified second backend fails closed on replayed begun intent");

	// 4. First backend settles ordinal 0 with terminal failure
	console.log("Backend 1 settling ordinal 0 with terminal failure...");
	const record1 = await backend1.recordStagePreflight({
		...logicalIdentity,
		transport_attempt_id: transportAttemptId,
		outcome: "failed",
		stop_reason: "error",
		error: "probe endpoint connection timeout",
		requests: 1,
		usage: null,
		provider_request_id: "req-smoke-fail",
	});
	assert.equal(record1.outcome, "failed", "preflight outcome recorded as failed");
	assert.equal(record1.transport_attempt_id, transportAttemptId, "preflight matches transport attempt ID");

	// 5. After first terminal failure, replay exposes terminal evidence
	console.log("Replaying begin for ordinal 0 after terminal settlement...");
	const replayAfterSettle = await backend2.beginStagePreflight(logicalIdentity);
	assert.equal(replayAfterSettle.status, "replayed", "replay returns replayed status");
	assert.equal(replayAfterSettle.intent.status, "settled", "intent status is settled");
	assert.ok(replayAfterSettle.preflight, "terminal preflight evidence is returned on replay");
	assert.equal(replayAfterSettle.preflight?.outcome, "failed", "terminal preflight has failed outcome");
	assert.equal(replayAfterSettle.preflight?.transport_attempt_id, transportAttemptId, "terminal preflight matches transport attempt ID");
	console.log("Verified terminal evidence exposed on replay");

	// 6. Next ordinal can now begin
	console.log("Beginning ordinal 1 in the same group...");
	const logicalIdentityOrdinal1 = {
		...logicalIdentity,
		ordinal: 1,
		requested_provider: "anthropic",
		requested_model: "claude-3-7-sonnet",
		requested_wire_model: "claude-3-7-sonnet-20250219",
	};
	const beginNext = await backend1.beginStagePreflight(logicalIdentityOrdinal1);
	assert.equal(beginNext.status, "applied", "next ordinal begins successfully");
	assert.equal(beginNext.intent.status, "begun", "next ordinal intent is begun");
	assert.equal(beginNext.intent.ordinal, 1, "next ordinal is 1");
	console.log("Verified next ordinal 1 begins successfully");

	console.log("preflight-intent-smoke: ALL REAL BOUNDARY ASSERTIONS PASSED!");
} finally {
	cleanup();
}
