/**
 * HOME-45 digest obligation machine — the 2026-08-14 re-arm loop fix.
 * Contract (through the REAL extension + ExtensionRunner, stubbed Linear API):
 *  - a *-plan.md arms the digest obligation via the session_stop backstop;
 *  - once its digest comment is posted, the SAME plan (same session local dir,
 *    same name, unchanged mtime) never re-arms — this was the loop: every
 *    session_stop re-armed an already-settled plan;
 *  - a rewrite of that plan after discharge re-arms;
 *  - a same-named plan in a DIFFERENT session's local dir re-arms (no
 *    cross-session masking through the shared cache).
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
fs.mkdirSync(path.join(home, ".config"), { recursive: true });
fs.mkdirSync(probe, { recursive: true });
fs.writeFileSync(path.join(home, ".config", "linear.env"), "LINEAR_API_KEY=fake\n");
Bun.spawnSync(["git", "init", "-q"], { cwd: probe });

afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

test("digest obligation: settled plan never re-arms; rewrite and foreign session do", () => {
	const child = Bun.spawnSync([process.execPath, harness, probe], {
		cwd: probe,
		env: { ...process.env, HOME: home },
	});
	expect(child.exitCode, child.stderr.toString()).toBe(0);
	const out = JSON.parse(child.stdout.toString()) as Record<string, string>;
	expect(out.armed, "plan file must arm the digest obligation").toContain("digest comment is owed");
	expect(out.settled, "posted digest must settle the plan — this re-arm WAS the loop").toBe("none");
	expect(out.rewritten, "plan rewritten after discharge owes a fresh digest").toContain("digest comment is owed");
	expect(out.resettled, "digested rewrite must settle again — foreign-session probe starts from a quiet state").toBe("none");
	expect(out.otherSession, "same-named plan in another session must not be masked").toContain("digest comment is owed");
});
