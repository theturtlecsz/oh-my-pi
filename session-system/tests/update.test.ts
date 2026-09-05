import { afterEach, describe, expect, test } from "bun:test";
import { chmodSync, cpSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// update.sh contract (OMP-156, standing guardrail OMP-229): pinned non-pushing gate runner.
//   * exactly one full 40-hex upstream commit; `main`/short/moving refs refused
//   * dirty tracked worktree refused
//   * refuses (before fetch) while any live same-owner process maps code from the checkout
//   * non-ancestor target -> requires a matching, passing standing-guardrail review record
//     (docs/upstream/reviews/<sha12>/review.json pinning this target with fork == HEAD),
//     then --no-ff merge only (never on branch `main`), then stop
//   * conflicted merge exits immediately; no gate runs
//   * ancestor target -> frozen install + natives + gates 1-12 verbatim, in order
//   * success refused if tracked state ends dirty; never pushes or installs live links

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
	logLines: () => string[];
	runUpdate: (arg: string, env?: Record<string, string | undefined>) => RunResult;
}

function makeFixture(options: { conflict?: boolean } = {}): Fixture {
	const root = mkdtempSync(join(tmpdir(), "omp-update-test-"));
	tempDirs.push(root);
	const repo = join(root, "repo");
	const upstream = join(root, "upstream");
	const binDir = join(root, "bin");
	const gateLog = join(root, "gates.log");
	mkdirSync(repo, { recursive: true });
	mkdirSync(binDir, { recursive: true });

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
	git(repo, "add", "-A");
	git(repo, "add", "-f", "node_modules/.bin/tsc");
	git(repo, "commit", "-m", "base");

	// --- upstream diverges ----------------------------------------------------
	git(root, "clone", repo, upstream);
	git(upstream, "config", "user.email", "up@example.invalid");
	git(upstream, "config", "user.name", "upstream");
	if (options.conflict) {
		writeFileSync(join(upstream, "file.txt"), "upstream-side\n");
	} else {
		writeFileSync(join(upstream, "upstream-only.txt"), "new upstream file\n");
	}
	git(upstream, "add", "-A");
	git(upstream, "commit", "-m", "upstream change");
	const upstreamSha = git(upstream, "rev-parse", "HEAD");
	git(repo, "remote", "add", "upstream", upstream);
	if (options.conflict) {
		writeFileSync(join(repo, "file.txt"), "fork-side\n");
		git(repo, "add", "file.txt");
		git(repo, "commit", "-m", "fork conflicting change");
	}

	// --- PATH stubs for every gate binary ------------------------------------
	const bunStub = join(binDir, "bun");
	writeFileSync(
		bunStub,
		`#!/bin/sh
line="bun $*"
if [ "$1 $2" = "run test:session:smoke" ]; then line="$line pg=\${OMP_WORK_POSTGRES_INTEGRATION:-unset}"; fi
echo "$line" >> "$GATE_LOG"
if [ -n "\${DIRTY_ON:-}" ] && [ "$*" = "\$DIRTY_ON" ]; then echo dirty >> "\$DIRTY_FILE"; fi
if [ "$1 $2" = "run test:session:smoke" ]; then echo "\${SMOKE_OUTPUT:-PASS}"; fi
exit "\${BUN_EXIT:-0}"
`,
	);
	chmodSync(bunStub, 0o755);
	const cargoStub = join(binDir, "cargo");
	writeFileSync(cargoStub, `#!/bin/sh\necho "cargo $*" >> "$GATE_LOG"\nexit "\${CARGO_EXIT:-0}"\n`);
	chmodSync(cargoStub, 0o755);

	const runUpdate = (arg: string, env: Record<string, string | undefined> = {}) =>
		sh(repo, ["bash", "session-system/update.sh", arg], {
			PATH: `${binDir}:${process.env.PATH ?? ""}`,
			GATE_LOG: gateLog,
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
	return { repo, upstreamSha, binDir, gateLog, logLines, runUpdate };
}

function writeReviewRecord(fx: Fixture, overrides: { target?: string; fork?: string } = {}): void {
	const target = overrides.target ?? fx.upstreamSha;
	const fork = overrides.fork ?? git(fx.repo, "rev-parse", "HEAD");
	const dir = join(fx.repo, "docs", "upstream", "reviews", fx.upstreamSha.slice(0, 12));
	mkdirSync(dir, { recursive: true });
	writeFileSync(
		join(dir, "review.json"),
		`${JSON.stringify({ target, fork }, null, "\t")}\n`,
	);
}

const FAKE_SHA = "0123456789abcdef0123456789abcdef01234567";

describe("argument validation", () => {
	test("refuses a missing argument before any git call", () => {
		const fx = makeFixture();
		const result = sh(fx.repo, ["bash", "session-system/update.sh"], { GATE_LOG: fx.gateLog });
		expect(result.exitCode).toBe(2);
		expect(result.stderr).toContain("exactly one argument");
		expect(fx.logLines()).toEqual([]);
	});
	test("refuses the moving ref `main`", () => {
		const fx = makeFixture();
		const result = fx.runUpdate("main");
		expect(result.exitCode).toBe(2);
		expect(result.stderr).toContain("moving refs are refused");
	});
	test("refuses an abbreviated commit id", () => {
		const fx = makeFixture();
		const result = fx.runUpdate("b4e8e856ad");
		expect(result.exitCode).toBe(2);
		expect(result.stderr).toContain("not a full 40-hex commit");
	});
	test("refuses a dirty tracked worktree", () => {
		const fx = makeFixture();
		writeFileSync(join(fx.repo, "file.txt"), "dirty\n");
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("uncommitted tracked changes");
		expect(fx.logLines()).toEqual([]);
	});
	test("refuses a well-formed commit id that does not resolve", () => {
		const fx = makeFixture();
		const result = fx.runUpdate(FAKE_SHA);
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("does not resolve to a commit");
	});
});

describe("merge path", () => {
	test("refuses to merge while on branch main", () => {
		const fx = makeFixture();
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("refusing to merge on 'main'");
		expect(git(fx.repo, "rev-list", "--count", "HEAD")).toBe("1");
	});
	test("refuses to merge without a standing-guardrail review record", () => {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("no review record");
		expect(git(fx.repo, "rev-list", "--count", "HEAD")).toBe("1");
		expect(fx.logLines()).toEqual([]);
	});
	test("refuses a review record pinned to a different candidate", () => {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx, { target: FAKE_SHA });
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain(`review record targets '${FAKE_SHA}', not ${fx.upstreamSha}`);
		expect(git(fx.repo, "rev-list", "--count", "HEAD")).toBe("1");
	});
	test("refuses a review record pinning an unknown fork commit", () => {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx, { fork: FAKE_SHA });
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("pins unknown fork commit");
		expect(result.stderr).toContain("re-run the review against the current fork state");
		expect(git(fx.repo, "rev-list", "--count", "HEAD")).toBe("1");
	});
	test("accepts a committed review record — fork-pin drift confined to docs/upstream/", () => {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx); // pins fork == pre-commit HEAD
		git(fx.repo, "add", "docs/upstream");
		git(fx.repo, "commit", "-m", "record upstream review");
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode, result.stderr).toBe(0);
		expect(git(fx.repo, "rev-parse", "HEAD^2")).toBe(fx.upstreamSha);
	});
	test("refuses fork-pin drift outside docs/upstream/", () => {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx); // pins fork == pre-commit HEAD
		writeFileSync(join(fx.repo, "shared.txt"), "fork drift\n");
		git(fx.repo, "add", "docs/upstream", "shared.txt");
		git(fx.repo, "commit", "-m", "record review plus unrelated drift");
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("outside docs/upstream/");
		expect(result.stderr).toContain("diverging path: shared.txt");
	});
	test("refuses to merge when the guardrail review fails", () => {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx);
		const result = fx.runUpdate(fx.upstreamSha, { BUN_EXIT: "1" });
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("standing guardrail review failed");
		expect(git(fx.repo, "rev-list", "--count", "HEAD")).toBe("1");
	});
	test("merges --no-ff on an integration branch and stops before any gate", () => {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx);
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode, result.stderr).toBe(0);
		expect(result.stdout).toContain("re-run to execute the gates");
		// true merge commit with two parents, second parent the pinned target
		expect(git(fx.repo, "rev-parse", "HEAD^2")).toBe(fx.upstreamSha);
		// only the pre-merge guardrail review runs; no install/natives/gates
		expect(fx.logLines()).toEqual([
			`bun scripts/verify-upstream-handoff.ts --record docs/upstream/reviews/${fx.upstreamSha.slice(0, 12)}/review.json`,
		]);
	});
	test("a conflicted merge exits immediately with no install or gates", () => {
		const fx = makeFixture({ conflict: true });
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx);
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode).not.toBe(0);
		// only the pre-merge guardrail review reached the log
		expect(fx.logLines()).toEqual([
			`bun scripts/verify-upstream-handoff.ts --record docs/upstream/reviews/${fx.upstreamSha.slice(0, 12)}/review.json`,
		]);
		// merge stopped in conflict state for hand resolution
		expect(git(fx.repo, "ls-files", "-u")).not.toBe("");
	});
});

describe("ancestor gate path", () => {
	function mergedFixture(): Fixture {
		const fx = makeFixture();
		git(fx.repo, "checkout", "-b", "integration");
		writeReviewRecord(fx);
		const merge = fx.runUpdate(fx.upstreamSha);
		expect(merge.exitCode, merge.stderr).toBe(0);
		rmSync(fx.gateLog, { force: true }); // drop the merge-path review line
		return fx;
	}

	test("runs frozen install, natives, then gates 1-12 verbatim and in order", () => {
		const fx = mergedFixture();
		const result = fx.runUpdate(fx.upstreamSha);
		expect(result.exitCode, result.stderr).toBe(0);
		expect(result.stdout).toContain("all gates passed");
		expect(fx.logLines()).toEqual([
			"bun install --frozen-lockfile",
			"refresh-natives",
			"bun scripts/verify-upstream-handoff.ts --record docs/upstream/baseline.json",
			"bun scripts/upstream-inventory.ts",
			"bun test session-system/tests packages/work-client/test scripts/verify-upstream-handoff.test.ts",
			"tsc --noEmit -p session-system",
			"bun run check:ts",
			"cargo fmt --all -- --check",
			"cargo clippy --workspace --exclude brush-core --no-deps -- -D warnings",
			"bun run test:ts",
			"bun run test:scripts",
			"bun run test:py",
			"cargo nextest run --workspace --exclude brush-core --status-level=fail --final-status-level=fail",
			"bun run test:session:smoke pg=1",
		]);
	});
	test("a failing gate stops the chain immediately", () => {
		const fx = mergedFixture();
		const result = fx.runUpdate(fx.upstreamSha, { CARGO_EXIT: "3" });
		expect(result.exitCode).not.toBe(0);
		const lines = fx.logLines();
		expect(lines[lines.length - 1]).toBe("cargo fmt --all -- --check");
		expect(lines).not.toContain("bun run test:ts");
	});
	test("refuses success when gate 12 does not print PASS", () => {
		const fx = mergedFixture();
		const result = fx.runUpdate(fx.upstreamSha, { SMOKE_OUTPUT: "smoke did not converge" });
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("did not print PASS");
	});
	test("refuses success when a gate dirties tracked state", () => {
		const fx = mergedFixture();
		const result = fx.runUpdate(fx.upstreamSha, { DIRTY_ON: "run test:py" });
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("dirtied tracked state");
	});
});

describe("forbidden behavior", () => {
	test("never pushes, installs live links, or merges a moving ref", () => {
		const script = readFileSync(realUpdateSh, "utf8");
		const code = script
			.split("\n")
			.filter(line => !line.trimStart().startsWith("#"))
			.join("\n");
		expect(code).not.toMatch(/git push/);
		expect(code).not.toMatch(/install\.sh/);
		expect(code).not.toMatch(/link-omp/);
		// the only merge in the script targets the validated pinned argument
		expect(code).toMatch(/git merge --no-ff "\$TARGET"/);
		expect(code.match(/git merge(?!-base)/g)).toHaveLength(1);
	});
});

describe("live-mapping fence (OMP-157)", () => {
	// The mapper prints "ready" only after mmap succeeds, then sleeps until the
	// test terminates it — awaiting that line awaits the real mapping event.
	// With a "shield" argument it also sets PR_SET_DUMPABLE=0, making its maps
	// kernel-unreadable to the fence (the owner-accepted carve-out class).
	const MAPPER_SCRIPT = `import ctypes, mmap, sys, time
f = open(sys.argv[1], "r+b")
m = mmap.mmap(f.fileno(), 0)
if len(sys.argv) > 2 and sys.argv[2] == "shield":
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)
print("ready", flush=True)
time.sleep(600)
`;

	async function awaitReady(stdout: ReadableStream<Uint8Array>): Promise<void> {
		const reader = stdout.getReader();
		const decoder = new TextDecoder();
		let seen = "";
		while (!seen.includes("ready")) {
			const { done, value } = await reader.read();
			if (done) throw new Error(`mapper exited before signaling ready: ${JSON.stringify(seen)}`);
			seen += decoder.decode(value, { stream: true });
		}
		reader.releaseLock();
	}

	test.skipIf(process.platform !== "linux")(
		"refuses the ancestor path while a live process maps the checkout, then succeeds once unmapped",
		async () => {
			const fx = makeFixture();
			git(fx.repo, "checkout", "-b", "integration");
			writeReviewRecord(fx);
			const merge = fx.runUpdate(fx.upstreamSha);
			expect(merge.exitCode, merge.stderr).toBe(0);
			rmSync(fx.gateLog, { force: true }); // drop the merge-path review line

			const root = realpathSync(fx.repo);
			const mappedFile = join(fx.repo, "mapped.bin"); // untracked: dirty check ignores it
			writeFileSync(mappedFile, Buffer.alloc(4096, 1));
			const mapperPy = join(fx.binDir, "mapper.py");
			writeFileSync(mapperPy, MAPPER_SCRIPT);
			const mappedReal = realpathSync(mappedFile);

			const mapper = Bun.spawn(["python3", mapperPy, mappedReal], {
				stdout: "pipe",
				stderr: "ignore",
			});
			try {
				await awaitReady(mapper.stdout);
				const refused = fx.runUpdate(fx.upstreamSha);
				expect(refused.exitCode).toBe(1);
				expect(refused.stderr).toContain(
					`update.sh: refusing to mutate ${root} — live process ${mapper.pid} maps code from this checkout: python3 ${mapperPy} ${mappedReal}`,
				);
				expect(refused.stderr).toContain(
					"update.sh: run the upgrade from a session already on the stable build, then retry",
				);
				expect(fx.logLines()).toEqual([]); // bun install and native refresh never started
			} finally {
				mapper.kill();
				await mapper.exited;
			}

			const second = fx.runUpdate(fx.upstreamSha);
			expect(second.exitCode, second.stderr).toBe(0);
			expect(second.stdout).toContain("all gates passed");
			const lines = fx.logLines();
			expect(lines[0]).toBe("bun install --frozen-lockfile");
			expect(lines[1]).toBe("refresh-natives");
			expect(lines[lines.length - 1]).toBe("bun run test:session:smoke pg=1");
		},
		20000,
	);

	test.skipIf(process.platform !== "linux")(
		"skips a kernel-shielded same-owner mapper with a warning (owner-accepted carve-out)",
		async () => {
			const fx = makeFixture();
			git(fx.repo, "checkout", "-b", "integration");
			writeReviewRecord(fx);
			const merge = fx.runUpdate(fx.upstreamSha);
			expect(merge.exitCode, merge.stderr).toBe(0);
			rmSync(fx.gateLog, { force: true }); // drop the merge-path review line

			const mappedFile = join(fx.repo, "mapped.bin");
			writeFileSync(mappedFile, Buffer.alloc(4096, 1));
			const mapperPy = join(fx.binDir, "mapper.py");
			writeFileSync(mapperPy, MAPPER_SCRIPT);
			const mapper = Bun.spawn(["python3", mapperPy, realpathSync(mappedFile), "shield"], {
				stdout: "pipe",
				stderr: "ignore",
			});
			try {
				await awaitReady(mapper.stdout);
				// The shielded mapping is invisible by kernel policy: the fence
				// warns, skips, and the full gate path runs — the accepted blind spot.
				const result = fx.runUpdate(fx.upstreamSha);
				expect(result.exitCode, result.stderr).toBe(0);
				expect(result.stdout).toContain("all gates passed");
				expect(result.stderr).toContain(
					`update.sh: warning: skipping kernel-shielded same-owner process ${mapper.pid} — mappings not inspectable`,
				);
				const lines = fx.logLines();
				expect(lines[0]).toBe("bun install --frozen-lockfile");
				expect(lines[lines.length - 1]).toBe("bun run test:session:smoke pg=1");
			} finally {
				mapper.kill();
				await mapper.exited;
			}
		},
		20000,
	);
});
