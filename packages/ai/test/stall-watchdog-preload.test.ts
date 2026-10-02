import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";

const DEADLINE_MS = 10_000;

function stripAnsi(text: string): string {
	return text.replace(/\u001b\[[0-9;]*[a-zA-Z]/g, "");
}

describe("stall-watchdog preload (packages/ai bunfig)", () => {
	it("kills a fixture blocked past its --timeout and names the per-test timeout", async () => {
		using temp = await TempDir.create("@pi-ai-stall-preload-");
		const fixture = path.join(temp.path(), "blocks.test.ts");
		await Bun.write(
			fixture,
			`import { test } from "bun:test";
test("blocks the event loop", () => {
	Bun.sleepSync(120_000);
});
`,
		);

		const proc = Bun.spawn(["bun", "test", fixture, "--timeout=2000"], {
			cwd: path.resolve(import.meta.dir, ".."),
			stdout: "pipe",
			stderr: "pipe",
			env: { ...process.env, NO_COLOR: "1" },
		});
		const pid = proc.pid;
		const stdoutPromise = new Response(proc.stdout).text();
		const stderrPromise = new Response(proc.stderr).text();

		const outcome = await Promise.race([
			proc.exited.then(() => "exited" as const),
			Bun.sleep(DEADLINE_MS).then(() => "deadline" as const),
		]);
		if (outcome === "deadline") {
			try {
				process.kill(pid, "SIGKILL");
			} catch {}
		}
		await proc.exited;
		const [, stderr] = await Promise.all([stdoutPromise, stderrPromise]);
		const cleanStderr = stripAnsi(stderr);

		expect(outcome).toBe("exited");
		expect(proc.exitCode !== 0 || proc.signalCode !== null).toBe(true);
		expect(cleanStderr).toContain("[stall-watchdog]");
		expect(cleanStderr).toContain("per-test timeout 2000 ms");
	}, 20_000);
});
