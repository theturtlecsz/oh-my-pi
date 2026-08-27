// HOME-147 candidate smoke (plan verification step 5):
//   OMP_WORK_POSTGRES_INTEGRATION=1 bun run session-system/tests/work-service-candidate-smoke.ts
// Disposable local PostgreSQL (initdb in a temp dir) + the real omp_work
// loopback service + the real work-now extension + model-bookends drive
// /capture → /now → plan → /summary → verify → audit → close → /done end to
// end. Everything lands in a temp HOME/XDG tree; the git remote is a local bare
// repo. Opt-in only: when the env flag is unset the script prints a skip line
// and exits 0 (pytest skipif convention); when set, any failure exits non-zero
// with the real error.
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { WORK_CONTRACT_SHA256 } from "@oh-my-pi/pi-work-client";

if (process.env.OMP_WORK_POSTGRES_INTEGRATION !== "1") {
	console.log("work-service-candidate-smoke: skipped (set OMP_WORK_POSTGRES_INTEGRATION=1; needs docker)");
	process.exit(0);
}

// HOME-148 reuse mode (cutover coordinator): the coordinator owns postgres, the
// service, and capabilities; this script drives the probe repo + harness against
// the already-running service. Env: OMP_WORK_SMOKE_REUSE=1,
// OMP_WORK_SMOKE_BASE_URL, OMP_WORK_SMOKE_WORKSPACE, OMP_WORK_SMOKE_OWNER,
// OMP_WORK_SMOKE_XDG (isolated config root), OMP_WORK_SMOKE_CAPABILITIES
// (dir holding owner.json), OMP_WORK_SMOKE_PROJECT (imported project name).
const REUSE = process.env.OMP_WORK_SMOKE_REUSE === "1";
const WORKSPACE = process.env.OMP_WORK_SMOKE_WORKSPACE ?? "00000000-0000-4000-8000-0000000000aa";
const OWNER = process.env.OMP_WORK_SMOKE_OWNER ?? "00000000-0000-4000-8000-0000000000bb";
const PROJECT = "00000000-0000-4000-8000-0000000000cc";
const PROJECT_NAME = process.env.OMP_WORK_SMOKE_PROJECT ?? "Smoke Project";

const repoRoot = path.resolve(import.meta.dir, "../..");
const pythonDir = path.join(repoRoot, "python/omp-work");

function freePort(): number {
	// ponytail: bind-and-close race window, fine for a gated local smoke
	const probe = Bun.listen({ hostname: "127.0.0.1", port: 0, socket: { data: () => {} } });
	const port = probe.port;
	probe.stop(true);
	return port;
}

const root = fs.mkdtempSync(path.join(os.tmpdir(), "omp-work-smoke-"));
const xdg = REUSE ? (process.env.OMP_WORK_SMOKE_XDG ?? path.join(root, "xdg")) : path.join(root, "xdg");
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
const baseUrl = REUSE ? (process.env.OMP_WORK_SMOKE_BASE_URL ?? "") : `http://127.0.0.1:${httpPort}`;
if (REUSE && !baseUrl) throw new Error("reuse mode requires OMP_WORK_SMOKE_BASE_URL");
const pgData = path.join(root, "pgdata");
let service: { kill(): void } | undefined;
const cleanup = () => {
	if (REUSE) {
		// The coordinator owns postgres, the service, and the provided XDG tree.
		fs.rmSync(root, { recursive: true, force: true });
		return;
	}
	service?.kill();
	Bun.spawnSync(["pg_ctl", "-D", pgData, "-m", "immediate", "stop"], { stdout: "ignore", stderr: "ignore" });
	fs.rmSync(root, { recursive: true, force: true });
};

try {
	if (REUSE) {
		// Wire the isolated XDG exactly like `capabilities init` would: owner
		// capability + the shared client.json contract (see capabilities.py).
		const capsDir = path.join(xdg, "omp/work-ledger/capabilities");
		fs.mkdirSync(capsDir, { recursive: true });
		fs.copyFileSync(path.join(process.env.OMP_WORK_SMOKE_CAPABILITIES ?? "", "owner.json"), path.join(capsDir, "owner.json"));
		const clientDir = path.join(xdg, "omp-work");
		fs.mkdirSync(clientDir, { recursive: true });
		fs.writeFileSync(path.join(clientDir, "client.json"), JSON.stringify({ base_url: baseUrl, workspace_id: WORKSPACE, owner_id: OWNER, bearer_file: path.join(capsDir, "owner.json") }, null, 2), { mode: 0o600 });
	}
	const py = (args: string[]) => {
		const run = Bun.spawnSync(["uv", "run", "python", "-m", "omp_work", ...args], {
			cwd: pythonDir,
			env: { ...process.env, XDG_CONFIG_HOME: xdg, XDG_STATE_HOME: xdg, XDG_DATA_HOME: xdg, OMP_WORK_POSTGRES_PORT: String(pgPort) },
		});
		if (run.exitCode !== 0) throw new Error(`omp_work ${args.join(" ")} failed: ${run.stderr.toString()}`);
		return run.stdout.toString();
	};
	if (!REUSE) py(["ops", "credentials", "init"]);
	const pgSecret = REUSE ? "" : fs.readFileSync(path.join(xdg, "omp/work-ledger/credentials/postgres"), "utf8").trim();
	if (!REUSE) {
		// local postgres: initdb as the current user, TCP loopback + a private socket dir
		const pwfile = path.join(root, "pgpw");
		fs.writeFileSync(pwfile, `${pgSecret}\n`, { mode: 0o600 });
		const initdb = (() => {
			try {
				return Bun.spawnSync(["initdb", "-D", pgData, "-U", "postgres", "--pwfile", pwfile, "--auth-host=scram-sha-256", "--auth-local=trust"], { stderr: "pipe" });
			} finally {
				// initdb is the only consumer; the superuser password must not linger in the smoke tree.
				fs.rmSync(pwfile, { force: true });
			}
		})();
		if (initdb.exitCode !== 0) throw new Error(`initdb failed: ${initdb.stderr.toString()}`);
		const run = Bun.spawnSync(["pg_ctl", "-D", pgData, "-w", "-l", path.join(root, "pg.log"), "-o", `-p ${pgPort} -k ${root} -c listen_addresses=127.0.0.1`, "start"], { stderr: "pipe" });
		if (run.exitCode !== 0) throw new Error(`pg_ctl start failed: ${run.stderr.toString()}`);
	}
	const psql = (sql: string) => {
		const res = Bun.spawnSync(["psql", "-h", "127.0.0.1", "-p", String(pgPort), "-U", "postgres", "-d", "omp_work", "-v", "ON_ERROR_STOP=1", "-c", sql], {
			env: { ...process.env, PGPASSWORD: pgSecret },
		});
		if (res.exitCode !== 0) throw new Error(`psql failed: ${res.stderr.toString()}`);
	};
	if (!REUSE) {
		for (let attempt = 0; attempt < 30; attempt++) {
			// Integration exception: awaits a real postgres condition (pg_isready exit code); the sleep is only the poll interval.
			if (Bun.spawnSync(["pg_isready", "-h", "127.0.0.1", "-p", String(pgPort)]).exitCode === 0) break;
			if (attempt === 29) throw new Error("postgres never became ready");
			await Bun.sleep(500);
		}
		py(["ops", "bootstrap"]);
		py(["ops", "capabilities", "init", "--workspace-id", WORKSPACE, "--owner-id", OWNER, "--base-url", baseUrl]);
		// Projects enter the ledger only via the Linear import (no v1 command
		// creates them). Seed the smoke project the way the importer leaves it.
		psql(`INSERT INTO omp_control.workspaces(workspace_id) VALUES ('${WORKSPACE}') ON CONFLICT DO NOTHING; INSERT INTO omp_work.projects(project_id, workspace_id, name, kind) VALUES ('${PROJECT}', '${WORKSPACE}', 'Smoke Project', 'surface');`);
		// Test-only authority seeding (mirrors tests/pg_native.py::seed_authority):
		// the HOME-148 mutation fence refuses every write until Work is authoritative.
		psql(`INSERT INTO omp_control.cutover_epochs(workspace_id, epoch_id, state, candidate_manifest, candidate_manifest_sha256) VALUES ('${WORKSPACE}', '00000000-0000-4000-8000-0000000000dd', 'sealed', '{}'::jsonb, '${"0".repeat(64)}') ON CONFLICT DO NOTHING; INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id) VALUES ('${WORKSPACE}', '00000000-0000-4000-8000-0000000000dd') ON CONFLICT DO NOTHING;`);
	}

	// git: bare remote + probe repo scoped to the smoke project
	const remote = path.join(root, "remote.git");
	const probe = path.join(root, "repo");
	fs.mkdirSync(probe, { recursive: true });
	const git = (cwd: string, args: string[]) => {
		const run = Bun.spawnSync(["git", ...args], { cwd });
		if (run.exitCode !== 0) throw new Error(`git ${args.join(" ")}: ${run.stderr.toString()}`);
		return run.stdout.toString().trim();
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
	const initialSha = git(probe, ["rev-parse", "HEAD"]);

	// service up (spawned only now: __main__ checks DB readiness before uvicorn starts)
	if (!REUSE) service = Bun.spawn(["uv", "run", "python", "-m", "omp_work", "serve", "--port", String(httpPort), "--capabilities-dir", path.join(xdg, "omp/work-ledger/capabilities")], {
		cwd: pythonDir,
		env: { ...process.env, XDG_CONFIG_HOME: xdg, XDG_STATE_HOME: xdg, XDG_DATA_HOME: xdg, OMP_WORK_POSTGRES_PORT: String(pgPort) },
		stdout: "ignore",
		stderr: "ignore",
	});
	for (let attempt = 0; attempt < 60; attempt++) {
		// Integration exception: awaits a real service condition (health endpoint); the sleep is only the poll interval.
		try {
			if ((await fetch(`${baseUrl}/v1/health/live`)).ok) break;
		} catch {
			/* not up yet */
		}
		if (attempt === 59) throw new Error("service never became live");
		await Bun.sleep(500);
	}

	const token = (JSON.parse(fs.readFileSync(path.join(xdg, "omp/work-ledger/capabilities/owner.json"), "utf8")) as { token: string }).token;
	const headers = { authorization: `Bearer ${token}`, "X-OMP-Workspace-ID": WORKSPACE, "X-OMP-Contract-SHA256": WORK_CONTRACT_SHA256 };

	const runHarness = (phase: "audit" | "closeout" | "done", phaseKey?: string) => {
		const args = [path.join(import.meta.dir, "fixtures/work-service-smoke-harness.ts"), probe, phase];
		if (phaseKey) args.push(phaseKey);
		const child = Bun.spawnSync([process.execPath, ...args], {
			cwd: probe,
			env: { ...process.env, HOME: home, XDG_CONFIG_HOME: xdg, PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent") },
		});
		if (child.exitCode !== 0) throw new Error(`harness phase "${phase}" failed:\n${child.stderr.toString()}`);
		const parsed = JSON.parse(child.stdout.toString()) as Record<string, unknown>;
		if (process.env.SMOKE_DEBUG) console.error(`PHASE ${phase} STDERR:`, child.stderr.toString(), `\nPHASE ${phase} STDOUT:`, JSON.stringify(parsed, null, 1));
		return parsed;
	};

	// Phase 1: audit (fresh process)
	const auditOut = runHarness("audit");
	const key = String(auditOut.key);
	const auditWorkflow = (await (await fetch(`${baseUrl}/v1/work-items/${key}/workflow`, { headers })).json()) as {
		close_attempts: { attempt_id: string; state: string }[];
		receipts: { kind: string; verdict?: string }[];
	};
	const liveAudited = (auditWorkflow.close_attempts ?? []).filter(a => a.state === "audited");
	assert.equal(liveAudited.length, 1, "exactly one live audited attempt");
	const auditedAttemptId = liveAudited[0].attempt_id;
	const auditReceipts = (auditWorkflow.receipts ?? []).filter(r => r.kind === "audit");
	assert.equal(auditReceipts.length, 1, "exactly one audit receipt");
	assert.equal(auditReceipts[0].verdict, "PASS", "audit receipt verdict is PASS");

	// Phase 2: closeout (fresh process restart, resumes attempt)
	const closeoutOut = runHarness("closeout", key);
	const closeoutWorkflow = (await (await fetch(`${baseUrl}/v1/work-items/${key}/workflow`, { headers })).json()) as {
		close_attempts: { attempt_id: string; state: string }[];
		close_attempt_events: { event_type: string; reason_code?: string; reason?: string }[];
		receipts: { kind: string }[];
	};
	const closeoutAttempt = (closeoutWorkflow.close_attempts ?? []).find(a => a.attempt_id === auditedAttemptId);
	assert.equal(closeoutAttempt?.state, "closeout_requested", "same attempt transitioned to closeout_requested");
	const closeoutReceipts = (closeoutWorkflow.receipts ?? []).filter(r => r.kind === "closeout");
	assert.equal(closeoutReceipts.length, 1, "exactly one closeout receipt exists");
	assert.ok(
		!(closeoutWorkflow.close_attempt_events ?? []).some(e => e.reason_code === "superseded_by_new_summary" || e.reason === "superseded_by_new_summary"),
		"no attempt superseded on resume",
	);

	// Phase 3: done (fresh process restart, closes work)
	const doneOut = runHarness("done", key);
	const doneWorkflow = (await (await fetch(`${baseUrl}/v1/work-items/${key}/workflow`, { headers })).json()) as {
		item: { state: string };
		close_attempts: { attempt_id: string; state: string }[];
	};
	const doneAttempt = (doneWorkflow.close_attempts ?? []).find(a => a.attempt_id === auditedAttemptId);
	assert.equal(doneAttempt?.state, "completed", "same attempt transitioned to completed");
	assert.equal(doneWorkflow.item.state, "DONE", "item is DONE");

	const out: Record<string, unknown> = { ...auditOut, ...closeoutOut, ...doneOut };
	const urls = [
		...((auditOut.fetchUrls as string[]) ?? []),
		...((closeoutOut.fetchUrls as string[]) ?? []),
		...((doneOut.fetchUrls as string[]) ?? []),
	];
	out.fetchUrls = urls;
	assert.match(key, /^(HOME|OMP)-\d+$/, "captured item key");
	assert.ok(String(out.firstScreen).length > 0 && !String(out.firstScreen).includes("error"), "first screen renders");
	assert.ok(String(out.captured).includes("Captured →"), "/capture filed the item");
	assert.ok(String(out.nowAfterSelect).includes(key), "/now selected the item");
	assert.equal(out.plan, "stamped", "plan stamp landed");
	assert.equal(out.hasRunAuditNextAction, true, "get_work renders next required action run_audit");
	assert.equal(out.noSealedTaskBytes, true, "get_work omits sealed task bytes");
	assert.ok(String(out.getWork).includes("PLAN PACKET"), "get_work renders the plan packet");
	assert.ok(String(out.packetPlanBody).includes("## Verification"), "packet carries the exact stored plan body");
	assert.match(String(out.packetReceiptSha), /^[0-9a-f]{64}$/, "packet cites the plan receipt sha256");
	assert.deepEqual(out.packetCriteria, ["AC-1 the item closes done with a pushed candidate"], "description-fallback criteria flow into the packet");
	assert.ok(String(out.verification).includes("verification receipt recorded"), "verification receipt");
	assert.ok(String(out.verification).includes("audit manifest sealed"), "verification append seals the manifest");
	assert.ok(String(out.audit).includes("verdict_pass") || String(out.audit).includes("the auditor reported PASS"), "settle minted the audit outcome");
	assert.equal(out.isResumeDigest, true, "resumed summary in fresh session injects short re-entry digest");
	assert.ok(String(out.closeout).includes("closeout receipt recorded"), "closeout receipt");
	assert.ok(String(out.closeout).includes("Yield the turn now before the next close step"), "closeout carries queued delivery yield note");
	assert.ok(String(out.closeout).includes("CLOSE ATTEMPT"), "closeout carries server-rendered checkpoint");
	const notices = ((out.doneUi as string[]) ?? []).join("\n");
	assert.ok(notices.includes("pushed"), "/done pushed the candidate");
	assert.ok(notices.includes("done"), "/done completed the work");
	assert.equal(out.batchFileExists, false, "staged cancel batch was consumed");
	assert.ok((out.consumedBatchFiles as string[]).length > 0, "consumed cancel batch file archived");
	assert.ok(String(out.now).includes("NOW unset"), "focus cleared after /done");

	// the freeze actually committed the dirty file as a new candidate commit
	const headSha = git(probe, ["rev-parse", "HEAD"]);
	assert.notEqual(headSha, initialSha, "freeze created a new commit");
	assert.equal(out.packetCommit, headSha, "PLAN PACKET commit is the frozen candidate commit");
	assert.equal(git(probe, ["show", "HEAD:smoke.txt"]), "candidate payload", "candidate commit carries the work");
	assert.equal(fs.readFileSync(path.join(probe, "owner.txt"), "utf8"), "owner setting\n", "pre-session owner file survives");
	assert.equal(git(probe, ["status", "--porcelain"]), "?? owner.txt", "pre-session owner file stays outside candidate");

	// the bare remote carries the exact candidate commit
	const remoteSha = git(probe, ["ls-remote", "origin", "refs/heads/main"]).split(/\s+/)[0];
	assert.equal(remoteSha, headSha, "remote ref resolves to the candidate commit");

	// network guard: the backend never crossed the loopback boundary
	assert.ok(urls.length > 0, "backend made requests");
	assert.ok(urls.every(url => ["127.0.0.1", "::1", "localhost", "[::1]"].includes(new URL(url).hostname)), `loopback only, saw: ${urls.join(", ")}`);

	// service-side read-back: closed done with the receipts bound
	const view = (await (await fetch(`${baseUrl}/v1/work-items/${key}/workflow`, { headers })).json()) as {
		item: { state: string; candidate: { kind: string; commit_sha: string; candidate_id: string } | null };
		receipts: { kind: string; candidate_id: string; remote_commit: string | null; candidate_commit: string | null }[];
	};
	assert.equal(view.item.state, "DONE", "item state");
	assert.equal(view.item.candidate?.kind, "final", "final candidate");
	assert.equal(view.item.candidate?.commit_sha, headSha, "candidate binds the exact commit");

	// service-side read-back: target was canceled in batch
	const targetKey = String(out.targetKey);
	const targetView = (await (await fetch(`${baseUrl}/v1/work-items/${targetKey}/workflow`, { headers })).json()) as {
		item: { state: string };
		close_attempt_events: { event_type: string; reason: string }[];
	};
	assert.equal(targetView.item.state, "CANCELED", "cancel batch target state is CANCELED");
	assert.ok(targetView.close_attempt_events.some(e => e.event_type === "batch_canceled"), "batch_canceled event recorded on target");
	const finalCandidateId = view.item.candidate?.candidate_id;
	// Completion binds receipts to the FINAL candidate — the planned candidate's
	// plan receipt is historical; assert the bound set, not the global one.
	const bound = view.receipts.filter(r => r.candidate_id === finalCandidateId);
	assert.deepEqual(bound.map(r => r.kind).sort(), ["audit", "closeout", "plan", "push", "verification"], "receipt set bound to final candidate");
	assert.equal(bound.find(r => r.kind === "push")?.remote_commit, headSha, "push receipt binds the remote commit");
	assert.equal(bound.find(r => r.kind === "audit")?.candidate_commit, headSha, "audit receipt binds the exact candidate commit");

	// HOME-148: machine-readable results for the cutover coordinator. Each entry
	// is recomputed from the real harness output — never a literal — so a softened
	// assertion above cannot silently turn into a PASS here.
	const resultsPath = process.env.OMP_WORK_SMOKE_RESULTS;
	if (resultsPath) {
		const results = [
			{ command_type: "first_screen", passed: String(out.firstScreen).length > 0 && !String(out.firstScreen).includes("error") },
			{ command_type: "capture", passed: /^(HOME|OMP)-\d+$/.test(key) && String(out.captured).includes("Captured →") },
			{ command_type: "now_select", passed: String(out.nowAfterSelect).includes(key) },
			{ command_type: "plan_stamp", passed: out.plan === "stamped" },
			{ command_type: "summary_freeze", passed: headSha !== initialSha && git(probe, ["show", "HEAD:smoke.txt"]) === "candidate payload" },
			{ command_type: "plan_packet", passed: String(out.getWork).includes("PLAN PACKET") && out.packetCommit === headSha && /^[0-9a-f]{64}$/.test(String(out.packetReceiptSha)) },
			{ command_type: "auditor_spawn", passed: out.hasRunAuditNextAction === true && out.noSealedTaskBytes === true },
			{ command_type: "verification", passed: String(out.verification).includes("verification receipt recorded") && String(out.verification).includes("audit manifest sealed") },
			{ command_type: "audit", passed: String(out.audit).includes("the auditor reported PASS") },
			{ command_type: "audit_binding", passed: bound.find(r => r.kind === "audit")?.candidate_commit === headSha },
			{ command_type: "closeout", passed: String(out.closeout).includes("closeout review recorded") || String(out.closeout).includes("receipt recorded") },
			{ command_type: "done_push", passed: notices.includes("pushed") && notices.includes("done") && remoteSha === headSha },
			{ command_type: "focus_cleared", passed: String(out.now).includes("NOW unset") },
			{ command_type: "loopback_only", passed: urls.length > 0 && urls.every(url => ["127.0.0.1", "::1", "localhost", "[::1]"].includes(new URL(url).hostname)) },
			{ command_type: "workflow_view", passed: view.item.state === "DONE" && view.item.candidate?.commit_sha === headSha && bound.map(r => r.kind).sort().join(",") === "audit,closeout,plan,push,verification" },
		];
		fs.mkdirSync(path.dirname(resultsPath), { recursive: true });
		fs.writeFileSync(resultsPath, JSON.stringify({ results }, null, 1));
	}

	console.log(`work-service-candidate-smoke: PASS (${key} done, candidate ${headSha.slice(0, 12)} pushed, ${urls.length} loopback requests)`);
} finally {
	cleanup();
}
