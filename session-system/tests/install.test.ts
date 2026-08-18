import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";

const repoRoot = path.resolve(import.meta.dir, "../..");
const ss = path.join(repoRoot, "session-system");
const home = fs.mkdtempSync(path.join(os.tmpdir(), "ss-install-"));
afterAll(() => fs.rmSync(home, { recursive: true, force: true }));

const SHARED_LINKS: Array<[string, string]> = [
	// workflow/ is support code: work-now.ts imports it
	[".omp/agent/extensions/workflow", "extensions/workflow"],
	[".omp/agent/extensions/model-bookends.ts", "extensions/model-bookends.ts"],
	[".omp/agent/extensions/model-bookends-audit.md", "extensions/model-bookends-audit.md"],
	[".omp/agent/extensions/model-bookends-refused.md", "extensions/model-bookends-refused.md"],
	[".omp/agent/extensions/model-bookends-schema-refused.md", "extensions/model-bookends-schema-refused.md"],
	[".omp/agent/extensions/model-bookends-stop-no-audit.md", "extensions/model-bookends-stop-no-audit.md"],
	[".omp/agent/extensions/model-bookends-stop-not-forwarded.md", "extensions/model-bookends-stop-not-forwarded.md"],
	[".omp/agent/extensions/model-bookends-stop-refused.md", "extensions/model-bookends-stop-refused.md"],
	[".omp/agent/agents/auditor.md", "agents/auditor.md"],
	[".omp/agent/rules/work-plan.md", "rules/work-plan.md"],
	["AGENTS.md", "agents/AGENTS.md"],
	[".omp/agent/AGENTS.md", "agents/omp-AGENTS.md"],
	[".agents/skills/summary", "skills/summary"],
	[".agents/skills/questionyourself", "skills/questionyourself"],
	[".agents/skills/whatsmissing", "skills/whatsmissing"],
	[".omp/agent/skills/intake", "skills/intake"],
];
const LINEAR_ENTRY = ".omp/agent/extensions/linear-now.ts";
const WORK_ENTRY = ".omp/agent/extensions/work-now.ts";

function run(args: string[] = []) {
	return Bun.spawnSync(["bash", path.join(ss, "install.sh"), ...args], {
		env: { ...process.env, HOME: home },
	});
}
const live = (p: string) => path.join(home, p);

describe("install.sh", () => {
	test("default installation links work-now and all shared links", () => {
		expect(run().exitCode).toBe(0);
		for (const [dst, src] of SHARED_LINKS) {
			expect(fs.realpathSync(live(dst))).toBe(fs.realpathSync(path.join(ss, src)));
		}
		expect(fs.realpathSync(live(WORK_ENTRY))).toBe(fs.realpathSync(path.join(ss, "extensions/work-now.ts")));
		expect(fs.existsSync(live(LINEAR_ENTRY))).toBe(false);
	});
	test("re-run is idempotent: staged set is retired and work-now is live", () => {
		expect(run().exitCode).toBe(0);
		expect(fs.lstatSync(live(".omp/agent/extensions")).isDirectory()).toBe(true);
		// staged sets from every run (including crashed ones) are retired
		const leftovers = fs.readdirSync(live(".omp/agent")).filter((n) => n.startsWith(".extensions-set.") || n.startsWith(".extensions-legacy."));
		expect(leftovers).toHaveLength(0);
		expect(fs.existsSync(live(WORK_ENTRY))).toBe(true);
		expect(fs.existsSync(live(LINEAR_ENTRY))).toBe(false);
	});
	test("--expect-backend work verifies installed backend, non-work fails", () => {
		expect(run(["--expect-backend", "work"]).exitCode).toBe(0);
		expect(run(["--expect-backend", "linear"]).exitCode).not.toBe(0);
		expect(run(["--expect-backend", "other"]).exitCode).not.toBe(0);
	});
	test("preserves unmanaged extensions across install runs and copy-mode flips, and removes stale linear-now.ts", () => {
		const extensions = live(".omp/agent/extensions");
		const regular = Buffer.from([0, 1, 2, 255]);
		const hidden = Buffer.from("hidden\n");
		const nested = Buffer.from([255, 0, 127]);
		fs.writeFileSync(path.join(extensions, "custom.bin"), regular);
		fs.writeFileSync(path.join(extensions, ".custom-hidden"), hidden);
		fs.mkdirSync(path.join(extensions, "custom-dir"));
		fs.writeFileSync(path.join(extensions, "custom-dir/nested.bin"), nested);
		fs.symlinkSync("custom.bin", path.join(extensions, "custom-link"));
		fs.symlinkSync("missing-target", path.join(extensions, "broken-link"));
		// Simulate a stale linear-now.ts left over in the live directory
		fs.writeFileSync(path.join(extensions, "linear-now.ts"), "// stale\n");
		const rootMode = 0o700;
		const rootMtimeSeconds = 978307200;
		fs.chmodSync(extensions, rootMode);
		fs.utimesSync(extensions, rootMtimeSeconds, rootMtimeSeconds);

		const assertPreserved = () => {
			expect(fs.readFileSync(path.join(extensions, "custom.bin"))).toEqual(regular);
			expect(fs.readFileSync(path.join(extensions, ".custom-hidden"))).toEqual(hidden);
			expect(fs.readFileSync(path.join(extensions, "custom-dir/nested.bin"))).toEqual(nested);
			expect(fs.readlinkSync(path.join(extensions, "custom-link"))).toBe("custom.bin");
			expect(fs.readlinkSync(path.join(extensions, "broken-link"))).toBe("missing-target");
			expect(fs.existsSync(live(WORK_ENTRY))).toBe(true);
			expect(fs.existsSync(live(LINEAR_ENTRY))).toBe(false);
			const root = fs.statSync(extensions);
			expect(root.mode & 0o777).toBe(rootMode);
			expect(Math.floor(root.mtimeMs / 1000)).toBe(rootMtimeSeconds);
		};

		expect(run().exitCode).toBe(0);
		assertPreserved();
		expect(run(["--copy"]).exitCode).toBe(0);
		assertPreserved();
	});
	test("refuses an unreadable extension root before exchange", () => {
		expect(run().exitCode).toBe(0);
		const extensions = live(".omp/agent/extensions");
		const sentinel = path.join(extensions, "keep.txt");
		fs.writeFileSync(sentinel, "keep");
		const mode = fs.statSync(extensions).mode & 0o777;
		const result = (() => {
			fs.chmodSync(extensions, 0);
			try {
				return run();
			} finally {
				fs.chmodSync(extensions, mode);
			}
		})();

		expect(result.exitCode).not.toBe(0);
		expect(result.stderr.toString()).toContain("extension root unreadable");
		expect(fs.readFileSync(sentinel, "utf8")).toBe("keep");
		expect(fs.existsSync(live(WORK_ENTRY))).toBe(true);
		expect(fs.existsSync(live(LINEAR_ENTRY))).toBe(false);
	});
	test("rejects legacy --backend selector", () => {
		expect(run(["--backend", "linear"]).exitCode).toBe(2);
		expect(run(["--backend", "work"]).exitCode).toBe(2);
		expect(run(["--backend", "notion"]).exitCode).toBe(2);
	});
});
