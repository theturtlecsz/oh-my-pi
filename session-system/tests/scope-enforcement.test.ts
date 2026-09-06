import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";

interface HarnessOutput {
	statuses: string[];
	refusalText: string;
	explicitText: string;
}

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-scope-"));
const home = path.join(tempRoot, "home");
const probe = path.join(tempRoot, "repo");
const harness = path.join(import.meta.dir, "fixtures/scope-enforcement-harness.ts");
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
fs.writeFileSync(
	path.join(home, ".omp", "agent", "work-now.json"),
	JSON.stringify({
		backend: "work",
		issueId: "old-now-id",
		identifier: "HOME-1",
		title: "Old global NOW",
		project: "Old Global Project",
		setAt: Date.now(),
	}),
);
Bun.spawnSync(["git", "init", "-q"], { cwd: probe });

afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

describe("unscoped repo write enforcement", () => {
	test("refuses restored global NOW project but accepts an explicit project", () => {
		const child = Bun.spawnSync([process.execPath, harness, probe], {
			cwd: probe,
			env: { ...process.env, HOME: home, XDG_CONFIG_HOME: path.join(home, ".config"), OMP_WORK_BEARER: "test-token", PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent") },
		});
		expect(child.exitCode, child.stderr.toString()).toBe(0);
		const output = JSON.parse(child.stdout.toString()) as HarnessOutput;
		expect(output.statuses.join("\n")).toContain("Old Global Project"); // state.project really restored
		expect(output.refusalText).toContain("refused: unscoped git repo");
		expect(output.refusalText).not.toContain("Old Global Project");
		expect(output.explicitText).toContain("CONFIRM REQUIRED");
		expect(output.explicitText).toContain("project Chosen Project");
		expect(output.explicitText).not.toContain("refused: unscoped git repo");
	});
});
