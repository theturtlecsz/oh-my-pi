import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";

const repoRoot = path.resolve(import.meta.dir, "../..");
const ss = path.join(repoRoot, "session-system");
const home = fs.mkdtempSync(path.join(os.tmpdir(), "ss-install-"));
afterAll(() => fs.rmSync(home, { recursive: true, force: true }));

const LINKS: Array<[string, string]> = [
	[".omp/agent/extensions/linear-now.ts", "extensions/linear-now.ts"],
	[".omp/agent/extensions/model-bookends.ts", "extensions/model-bookends.ts"],
	[".omp/agent/extensions/model-bookends-audit.md", "extensions/model-bookends-audit.md"],
	[".omp/agent/extensions/model-bookends-refused.md", "extensions/model-bookends-refused.md"],
	[".omp/agent/extensions/model-bookends-schema-refused.md", "extensions/model-bookends-schema-refused.md"],
	[".omp/agent/extensions/model-bookends-stop-no-audit.md", "extensions/model-bookends-stop-no-audit.md"],
	[".omp/agent/extensions/model-bookends-stop-not-forwarded.md", "extensions/model-bookends-stop-not-forwarded.md"],
	[".omp/agent/extensions/model-bookends-stop-refused.md", "extensions/model-bookends-stop-refused.md"],
	[".omp/agent/agents/auditor.md", "agents/auditor.md"],
	[".omp/agent/rules/linear-plan.md", "rules/linear-plan.md"],
	["AGENTS.md", "agents/AGENTS.md"],
	[".omp/agent/AGENTS.md", "agents/omp-AGENTS.md"],
	[".agents/skills/summary", "skills/summary"],
	[".agents/skills/questionyourself", "skills/questionyourself"],
	[".agents/skills/whatsmissing", "skills/whatsmissing"],
	[".omp/agent/skills/intake", "skills/intake"],
];

function run() {
	return Bun.spawnSync(["bash", path.join(ss, "install.sh")], {
		env: { ...process.env, HOME: home },
	});
}

describe("install.sh", () => {
	test("links every artifact into HOME", () => {
		expect(run().exitCode).toBe(0);
		for (const [live, src] of LINKS) {
			expect(fs.realpathSync(path.join(home, live))).toBe(fs.realpathSync(path.join(ss, src)));
		}
	});
	test("is idempotent on re-run", () => {
		const second = run();
		expect(second.exitCode).toBe(0);
		expect(second.stdout.toString()).not.toContain("linked"); // all "ok"
	});
});
