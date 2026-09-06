/**
 * HOME-114 mechanical enforcement (WEB-2 sequence + advisor findings): the
 * work-now extension refuses wrap-up writes (update_health, propose_close,
 * archive_issue) unless the HOST observed the owner literally entering
 * /summary or /done. Contracts proven here, each through a REAL
 * ExtensionRunner (real createContext → ctx.taskDepth wiring):
 *  - owner-typed "/summary" (input event) unlocks; a session switch re-locks
 *    (authorization never crosses transcripts);
 *  - the host-composed user-attributed skill-prompt message unlocks; the same
 *    marker TEXT pasted as an ordinary user message does not (prompt bytes
 *    are not provenance);
 *  - a depth-1 (subagent) runner never unlocks on either path;
 *  - a legacy host whose ctx lacks taskDepth fails CLOSED (nothing unlocks).
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-closeout-"));
const home = path.join(tempRoot, "home");
const probe = path.join(tempRoot, "repo");
const harness = path.join(import.meta.dir, "fixtures/closeout-lock-harness.ts");
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

afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

const LOCK = "closeout lock (HOME-114)";
const run = (mode: "input" | "skill" | "done" | "forged-subagent" | "legacy-host" | "body-refused"): Record<string, string | string[]> => {
	const child = Bun.spawnSync([process.execPath, harness, probe, mode], {
		cwd: probe,
		// Pin XDG_CONFIG_HOME to the temp HOME: hosted CI images export it globally,
		// and ompWorkConfigDir() prefers it over $HOME/.config (OMP-254).
		env: { ...process.env, HOME: home, XDG_CONFIG_HOME: path.join(home, ".config"), PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent") },
	});
	expect(child.exitCode, child.stderr.toString()).toBe(0);
	return JSON.parse(child.stdout.toString()) as Record<string, string | string[]>;
};

describe("HOME-114 closeout lock (host-enforced)", () => {
	test("owner-typed /summary unlocks; session switch re-locks", () => {
		const out = run("input");
		expect(out.before).toContain(LOCK);
		expect(out.before_cancel_work, "cancel_work must be lock-refused").toContain(LOCK);
		expect(out.afterUnlock).not.toContain(LOCK);
		expect(out.afterUnlock).toContain("CONFIRM REQUIRED");
		expect(out.afterSwitch, "session switch must re-lock").toContain(LOCK);
	});

	test("owner-typed /skill:summary unlocks; structured and pasted messages do not", () => {
		const out = run("skill");
		expect(out.before).toContain(LOCK);
		expect(out.afterPaste).toContain(LOCK);
		expect(out.afterStructured).toContain(LOCK);
		expect(out.afterUnlock).not.toContain(LOCK);
		expect(out.afterUnlock).toContain("CONFIRM REQUIRED");
	});

	test("owner /done unlocks at depth 0 (no NOW: flips auth, then bails)", () => {
		const out = run("done");
		expect(out.before).toContain(LOCK);
		expect(out.afterAttempts).not.toContain(LOCK);
		expect(out.afterAttempts).toContain("CONFIRM REQUIRED");
		expect(out.uiCalls).toContain("notify:No NOW set");
		expect(out.uiCalls).not.toContain("select");
		expect(out.uiCalls).not.toContain("confirm");
	});

	test("subagent (depth 1) never unlocks; /done refuses before any NOW/UI/write flow", () => {
		const out = run("forged-subagent");
		expect(out.before).toContain(LOCK);
		expect(out.afterAttempts).toContain(LOCK);
		// NOW is restored in this mode — an unguarded /done would open select/confirm.
		expect(out.uiCalls).not.toContain("select");
		expect(out.uiCalls).not.toContain("confirm");
		expect(out.uiCalls).toContain("notify:/done is owner-only — refused outside the owner's main session (HOME-114)");
		// Guard fired before NOW logic: neither the no-NOW bail nor the close flow ran.
		expect(out.uiCalls).not.toContain("notify:No NOW set");
	});

	test("legacy host without ctx.taskDepth fails closed", () => {
		const out = run("legacy-host");
		expect(out.before).toContain(LOCK);
		expect(out.afterAttempts).toContain(LOCK);
	});

	test("record_health rejects body after owner authorization", () => {
		const out = run("body-refused");
		expect(out.bodyRefused).toBe("REFUSED — record_health stores only project health and updated_at; omit body.");
	});
});
