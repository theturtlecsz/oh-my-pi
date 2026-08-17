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

if (process.env.OMP_WORK_POSTGRES_INTEGRATION !== "1") {
	console.log("work-service-candidate-smoke: skipped (set OMP_WORK_POSTGRES_INTEGRATION=1; needs docker)");
	process.exit(0);
}

const WORKSPACE = "00000000-0000-4000-8000-0000000000aa";
const OWNER = "00000000-0000-4000-8000-0000000000bb";
const PROJECT = "00000000-0000-4000-8000-0000000000cc";

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
const xdg = path.join(root, "xdg");
const home = path.join(root, "home");
fs.mkdirSync(xdg, { recursive: true });
fs.mkdirSync(home, { recursive: true });
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
	// local postgres: initdb as the current user, TCP loopback + a private socket dir
	const pwfile = path.join(root, "pgpw");
	fs.writeFileSync(pwfile, `${pgSecret}\n`, { mode: 0o600 });
	let run = Bun.spawnSync(["initdb", "-D", pgData, "-U", "postgres", "--pwfile", pwfile, "--auth-host=scram-sha-256", "--auth-local=trust"], { stderr: "pipe" });
	if (run.exitCode !== 0) throw new Error(`initdb failed: ${run.stderr.toString()}`);
	run = Bun.spawnSync(["pg_ctl", "-D", pgData, "-w", "-l", path.join(root, "pg.log"), "-o", `-p ${pgPort} -k ${root} -c listen_addresses=127.0.0.1`, "start"], { stderr: "pipe" });
	if (run.exitCode !== 0) throw new Error(`pg_ctl start failed: ${run.stderr.toString()}`);
	const psql = (sql: string) => {
		const res = Bun.spawnSync(["psql", "-h", "127.0.0.1", "-p", String(pgPort), "-U", "postgres", "-d", "omp_work", "-v", "ON_ERROR_STOP=1", "-c", sql], {
			env: { ...process.env, PGPASSWORD: pgSecret },
		});
		if (res.exitCode !== 0) throw new Error(`psql failed: ${res.stderr.toString()}`);
	};
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
	fs.writeFileSync(path.join(probe, ".work-project"), "Smoke Project\n");
	git(probe, ["add", ".work-project"]);
	git(probe, ["commit", "-q", "-m", "init"]);
	git(probe, ["remote", "add", "origin", remote]);
	git(probe, ["push", "-q", "-u", "origin", "main"]);
	const initialSha = git(probe, ["rev-parse", "HEAD"]);

	// service up (spawned only now: __main__ checks DB readiness before uvicorn starts)
	service = Bun.spawn(["uv", "run", "python", "-m", "omp_work", "serve", "--port", String(httpPort), "--capabilities-dir", path.join(xdg, "omp/work-ledger/capabilities")], {
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

	// drive the real extensions in a child with isolated env
	const child = Bun.spawnSync([process.execPath, path.join(import.meta.dir, "fixtures/work-service-smoke-harness.ts"), probe], {
		cwd: probe,
		env: { ...process.env, HOME: home, XDG_CONFIG_HOME: xdg },
	});
	if (child.exitCode !== 0) throw new Error(`harness failed: ${child.stderr.toString()}`);
	const out = JSON.parse(child.stdout.toString()) as Record<string, unknown>;
	if (process.env.SMOKE_DEBUG) console.error(JSON.stringify(out, null, 1));
	const key = String(out.key);
	assert.match(key, /^(HOME|OMP)-\d+$/, "captured item key");
	assert.ok(String(out.captured).includes("Captured →"), "/capture filed the item");
	assert.ok(String(out.nowAfterSelect).includes(key), "/now selected the item");
	assert.equal(out.plan, "stamped", "plan stamp landed");
	assert.equal(out.spawnBlocked, false, "auditor spawn not blocked");
	assert.ok(String(out.verification).includes("verification receipt recorded"), "verification receipt");
	assert.ok(String(out.audit).includes("audit receipt recorded"), "audit receipt");
	assert.ok(String(out.closeout).includes("closeout receipt recorded"), "closeout receipt");
	assert.ok(String(out.requestCloseout).includes("close"), "closeout intent requested");
	const notices = (out.doneUi as string[]).join("\n");
	assert.ok(notices.includes("pushed"), "/done pushed the candidate");
	assert.ok(notices.includes("done"), "/done completed the work");
	assert.ok(String(out.now).includes("NOW unset"), "focus cleared after /done");

	// the freeze actually committed the dirty file as a new candidate commit
	const headSha = git(probe, ["rev-parse", "HEAD"]);
	assert.notEqual(headSha, initialSha, "freeze created a new commit");
	assert.equal(git(probe, ["show", "HEAD:smoke.txt"]), "candidate payload", "candidate commit carries the work");

	// the bare remote carries the exact candidate commit
	const remoteSha = git(probe, ["ls-remote", "origin", "refs/heads/main"]).split(/\s+/)[0];
	assert.equal(remoteSha, headSha, "remote ref resolves to the candidate commit");

	// network guard: the backend never crossed the loopback boundary
	const urls = out.fetchUrls as string[];
	assert.ok(urls.length > 0, "backend made requests");
	assert.ok(urls.every(url => ["127.0.0.1", "::1", "localhost", "[::1]"].includes(new URL(url).hostname)), `loopback only, saw: ${urls.join(", ")}`);

	// service-side read-back: closed done with the receipts bound
	const token = (JSON.parse(fs.readFileSync(path.join(xdg, "omp/work-ledger/capabilities/owner.json"), "utf8")) as { token: string }).token;
	const headers = { authorization: `Bearer ${token}`, "X-OMP-Workspace-ID": WORKSPACE };
	const view = (await (await fetch(`${baseUrl}/v1/work-items/${key}/workflow`, { headers })).json()) as {
		item: { state: string; candidate: { kind: string; commit_sha: string; candidate_id: string } | null };
		receipts: { kind: string; candidate_id: string; remote_commit: string | null }[];
	};
	assert.equal(view.item.state, "DONE", "item state");
	assert.equal(view.item.candidate?.kind, "final", "final candidate");
	assert.equal(view.item.candidate?.commit_sha, headSha, "candidate binds the exact commit");
	const finalCandidateId = view.item.candidate?.candidate_id;
	// Completion binds receipts to the FINAL candidate — the planned candidate's
	// plan receipt is historical; assert the bound set, not the global one.
	const bound = view.receipts.filter(r => r.candidate_id === finalCandidateId);
	assert.deepEqual(bound.map(r => r.kind).sort(), ["audit", "closeout", "plan", "push", "verification"], "receipt set bound to final candidate");
	assert.equal(bound.find(r => r.kind === "push")?.remote_commit, headSha, "push receipt binds the remote commit");

	console.log(`work-service-candidate-smoke: PASS (${key} done, candidate ${headSha.slice(0, 12)} pushed, ${urls.length} loopback requests)`);
} finally {
	cleanup();
}
