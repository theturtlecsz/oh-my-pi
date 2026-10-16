/**
 * Bounded synchronous spawns for test fixtures. An unbounded `Bun.spawnSync`
 * (e.g. `git init/add/commit`) can hang a test until the suite timeout.
 */
export function spawnFixtureSync(argv: string[], cwd: string, timeoutMs = 10_000): void {
	const result = Bun.spawnSync(argv, {
		cwd,
		stdin: "ignore",
		stdout: "ignore",
		stderr: "pipe",
		timeout: timeoutMs,
		killSignal: "SIGKILL",
	});
	if (result.exitCode === 0) return;
	const stderr = result.stderr.toString().trim();
	const outcome = result.signalCode ? `signal ${result.signalCode}` : `exit code ${result.exitCode}`;
	throw new Error(`Fixture command failed (${outcome}): ${argv.join(" ")}${stderr ? `\n${stderr}` : ""}`);
}

export function runGitFixture(cwd: string, args: string[], timeoutMs?: number): void {
	spawnFixtureSync(["git", ...args], cwd, timeoutMs);
}
