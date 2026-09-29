import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test } from "bun:test";
import { materializeExecutionRuntime } from "../extensions/workflow/git";

describe("execution runtime dependency install (OMP-472)", () => {
	const roots: string[] = [];

	afterEach(() => {
		for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true });
	});

	const makeWorktree = (): { primary: string; worktree: string } => {
		const primary = fs.mkdtempSync(path.join(os.tmpdir(), "ss-exec-install-"));
		roots.push(primary);
		const worktree = path.join(primary, "wt");
		fs.mkdirSync(worktree);
		fs.writeFileSync(path.join(worktree, "package.json"), JSON.stringify({ name: "exec-install-probe" }));
		return { primary, worktree };
	};

	test("a stalled install yields the event loop, times out, and kills the child", async () => {
		const { primary, worktree } = makeWorktree();
		const pidFile = path.join(worktree, "child.pid");
		const script = [
			`require("node:fs").writeFileSync(${JSON.stringify(pidFile)}, String(process.pid));`,
			"await Bun.sleep(30000);",
		].join(" ");
		// This timer can run before the call settles only when the install does not block the event loop.
		let firedWhilePending = false;
		const pending = materializeExecutionRuntime(primary, worktree, {
			command: [process.execPath, "-e", script],
			timeoutMs: 1_500,
		});
		setTimeout(() => {
			firedWhilePending = true;
		}, 100);
		await expect(pending).rejects.toThrow(/timed out after/);
		expect(firedWhilePending).toBe(true);
		const pid = Number(fs.readFileSync(pidFile, "utf8"));
		expect(Number.isInteger(pid)).toBe(true);
		expect(pid).toBeGreaterThan(0);
		expect(() => process.kill(pid, 0)).toThrow();
	});

	test("a non-zero install surfaces the first stderr line", async () => {
		const { primary, worktree } = makeWorktree();
		const script = `process.stderr.write("boom\\n"); process.exit(1);`;
		await expect(
			materializeExecutionRuntime(primary, worktree, {
				command: [process.execPath, "-e", script],
				timeoutMs: 1_500,
			}),
		).rejects.toThrow(/dependency install failed: boom/);
	});
});
