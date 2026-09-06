/**
 * HOME-122 execution obligation contract through the real extension:
 * plan files are inert; plan_approved arms one checkpoint; a typed handoff
 * settles it; later file rewrites cannot resurrect it.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, expect, test } from "bun:test";

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-obligation-"));
const home = path.join(tempRoot, "home");
const probe = path.join(tempRoot, "repo");
const harness = path.join(import.meta.dir, "fixtures/obligation-loop-harness.ts");
fs.mkdirSync(path.join(home, ".omp", "agent"), { recursive: true });
fs.mkdirSync(path.join(home, ".config", "omp-work"), { recursive: true });
fs.mkdirSync(probe, { recursive: true });
fs.writeFileSync(
	path.join(home, ".config", "omp-work", "client.json"),
	JSON.stringify({
		base_url: "http://127.0.0.1:54322",
		workspace_id: "00000000-0000-7000-8000-000000000001",
		owner_id: "00000000-0000-7000-8000-000000000002",
	}),
);
Bun.spawnSync(["git", "init", "-q"], { cwd: probe });

test("only approval arms execution and only typed handoff settles it", () => {
	const child = Bun.spawnSync([process.execPath, harness, probe], {
		cwd: probe,
		// Pin XDG_CONFIG_HOME to the temp HOME: hosted CI images export it globally,
		// and ompWorkConfigDir() prefers it over $HOME/.config (OMP-254).
		env: { ...process.env, HOME: home, XDG_CONFIG_HOME: path.join(home, ".config"), OMP_WORK_BEARER: "test-token", PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent") },
	});
	expect(child.exitCode, child.stderr.toString()).toBe(0);
	const out = JSON.parse(child.stdout.toString()) as Record<string, string | number>;
	expect(out.fileOnly, "creating a plan file must not imply approval or execution").toBe("none");
	expect(out.approved).toContain("Post one silent workflow checkpoint");
	expect(out.handoff).toContain("handoff receipt recorded");
	expect(out.settled).toBe("none");
	expect(out.rewrittenFile, "file watchers must not resurrect settled workflow debt").toBe("none");
	expect(out.planComments).toBe(1);
});
