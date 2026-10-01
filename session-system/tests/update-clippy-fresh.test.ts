import { afterEach, describe, expect, test } from "bun:test";
import { chmodSync, cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

// OMP-510: gate 7 clippy cache freshness regression test.
// Verifies that update.sh refreshes every tracked .rs under crates/
// before invoking cargo clippy, so a warm cache cannot hide lints.

const realUpdateSh = join(import.meta.dir, "..", "update.sh");
const tempDirs: string[] = [];

afterEach(() => {
	for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

interface RunResult {
	exitCode: number;
	stdout: string;
	stderr: string;
}

const RUST_TRACKED_PATHS = [
	"crates/demo/src/lib.rs",
	"crates/demo/build.rs",
	"crates/vendor/gen/bindings/rust/lib.rs",
	"crates/vendor/gen/bindings/rust/build.rs",
] as const;

function sh(cwd: string, command: string[], env: Record<string, string | undefined> = {}): RunResult {
	const proc = Bun.spawnSync(command, {
		cwd,
		env: { ...process.env, GIT_MERGE_AUTOEDIT: "no", ...env } as Record<string, string>,
		stdout: "pipe",
		stderr: "pipe",
	});
	return { exitCode: proc.exitCode, stdout: proc.stdout.toString(), stderr: proc.stderr.toString() };
}

function git(cwd: string, ...args: string[]): string {
	const result = sh(cwd, ["git", ...args]);
	if (result.exitCode !== 0) throw new Error(`git ${args.join(" ")}: ${result.stderr}`);
	return result.stdout.trim();
}

interface Fixture {
	repo: string;
	upstreamSha: string;
	binDir: string;
	gateLog: string;
	markerFile: string;
	logLines: () => string[];
	runUpdate: (arg: string, env?: Record<string, string | undefined>) => RunResult;
}

function makeFixture(): Fixture {
	const root = mkdtempSync(join(tmpdir(), "omp-update-clippy-fresh-"));
	tempDirs.push(root);
	const repo = join(root, "repo");
	const upstream = join(root, "upstream");
	const binDir = join(root, "bin");
	const gateLog = join(root, "gates.log");
	const markerFile = join(root, "marker");
	mkdirSync(repo, { recursive: true });
	mkdirSync(binDir, { recursive: true });
	writeFileSync(markerFile, "");

	// --- fork repo skeleton -------------------------------------------------
	git(repo, "init", "-b", "main");
	git(repo, "config", "user.email", "test@example.invalid");
	git(repo, "config", "user.name", "update-test");
	mkdirSync(join(repo, "session-system"), { recursive: true });
	mkdirSync(join(repo, "node_modules", ".bin"), { recursive: true });
	cpSync(realUpdateSh, join(repo, "session-system", "update.sh"));
	writeFileSync(
		join(repo, "session-system", "refresh-natives.sh"),
		`#!/bin/sh\necho "refresh-natives" >> "$GATE_LOG"\n`,
	);
	const tscStub = join(repo, "node_modules", ".bin", "tsc");
	writeFileSync(tscStub, `#!/bin/sh\necho "tsc $*" >> "$GATE_LOG"\n`);
	chmodSync(tscStub, 0o755);
	writeFileSync(join(repo, "file.txt"), "base\n");
	writeFileSync(join(repo, "shared.txt"), "shared\n");
	writeFileSync(join(repo, ".gitignore"), "node_modules/\n");

	for (const relPath of RUST_TRACKED_PATHS) {
		const fullPath = join(repo, relPath);
		mkdirSync(dirname(fullPath), { recursive: true });
		writeFileSync(fullPath, "// tracked rust file\n");
	}

	git(repo, "add", "-A");
	git(repo, "add", "-f", "node_modules/.bin/tsc");
	git(repo, "commit", "-m", "base");

	// --- upstream diverges ----------------------------------------------------
	git(root, "clone", repo, upstream);
	git(upstream, "config", "user.email", "up@example.invalid");
	git(upstream, "config", "user.name", "upstream");
	writeFileSync(join(upstream, "upstream-only.txt"), "new upstream file\n");
	git(upstream, "add", "-A");
	git(upstream, "commit", "-m", "upstream change");
	const upstreamSha = git(upstream, "rev-parse", "HEAD");
	git(repo, "remote", "add", "upstream", upstream);

	// --- PATH stubs for every gate binary ------------------------------------
	const bunStub = join(binDir, "bun");
	writeFileSync(
		bunStub,
		`#!/bin/sh
line="bun $*"
if [ "$1 $2" = "run test:session:smoke" ]; then line="$line pg=\${OMP_WORK_POSTGRES_INTEGRATION:-unset}"; fi
echo "$line" >> "$GATE_LOG"
if [ "$1 $2" = "run test:session:smoke" ]; then echo "\${SMOKE_OUTPUT:-PASS}"; fi
exit "\${BUN_EXIT:-0}"
`,
	);
	chmodSync(bunStub, 0o755);

	const cargoStub = join(binDir, "cargo");
	writeFileSync(
		cargoStub,
		`#!/bin/sh
if [ "$1" = "clippy" ]; then
	for f in crates/demo/src/lib.rs crates/demo/build.rs crates/vendor/gen/bindings/rust/lib.rs crates/vendor/gen/bindings/rust/build.rs; do
		if [ "$f" -nt "$MARKER_FILE" ]; then
			echo "fresh $f" >> "$GATE_LOG"
		else
			echo "stale $f" >> "$GATE_LOG"
		fi
	done
fi
echo "cargo $*" >> "$GATE_LOG"
exit "\${CARGO_EXIT:-0}"
`,
	);
	chmodSync(cargoStub, 0o755);

	const runUpdate = (arg: string, env: Record<string, string | undefined> = {}) =>
		sh(repo, ["bash", "session-system/update.sh", arg], {
			PATH: `${binDir}:${process.env.PATH ?? ""}`,
			GATE_LOG: gateLog,
			MARKER_FILE: markerFile,
			DIRTY_FILE: join(repo, "file.txt"),
			...env,
		});

	const logLines = () => {
		try {
			return readFileSync(gateLog, "utf8").split("\n").filter(Boolean);
		} catch {
			return [];
		}
	};

	return { repo, upstreamSha, binDir, gateLog, markerFile, logLines, runUpdate };
}

function writeReviewRecord(fx: Fixture, overrides: { target?: string; fork?: string } = {}): void {
	const target = overrides.target ?? fx.upstreamSha;
	const fork = overrides.fork ?? git(fx.repo, "rev-parse", "HEAD");
	mkdirSync(join(fx.repo, "docs"), { recursive: true });
	writeFileSync(
		join(fx.repo, "docs", `upstream-review-${fx.upstreamSha.slice(0, 12)}.json`),
		`${JSON.stringify({ target, fork }, null, "\t")}\n`,
	);
}

describe("update.sh clippy cache freshness (OMP-510)", () => {
	test("refreshes mtimes on all tracked crates/*.rs so cargo clippy re-lints every member", () => {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx);
		git(fx.repo, "add", "docs");
		git(fx.repo, "commit", "-m", "record upstream review");
		const merge = fx.runUpdate(fx.upstreamSha);
		expect(merge.exitCode, merge.stderr).toBe(0);
		rmSync(fx.gateLog, { force: true });

		// Set the 4 tracked rust file mtimes to 2000, marker to 2010
		for (const relPath of RUST_TRACKED_PATHS) {
			utimesSync(join(fx.repo, relPath), 2000, 2000);
		}
		utimesSync(fx.markerFile, 2010, 2010);

		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode, result.stderr).toBe(0);
		expect(result.stdout).toContain("all gates passed");

		const clippyFreshnessLines = fx.logLines().filter(l => l.startsWith("fresh ") || l.startsWith("stale "));
		expect(clippyFreshnessLines).toEqual([
			"fresh crates/demo/src/lib.rs",
			"fresh crates/demo/build.rs",
			"fresh crates/vendor/gen/bindings/rust/lib.rs",
			"fresh crates/vendor/gen/bindings/rust/build.rs",
		]);

		expect(git(fx.repo, "status", "--porcelain")).toBe("");
	});
});
