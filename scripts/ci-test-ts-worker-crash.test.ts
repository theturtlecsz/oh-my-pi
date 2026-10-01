import { describe, expect, test } from "bun:test";
import { ptree, TempDir } from "@oh-my-pi/pi-utils";
import { extractFailingTests } from "./ci-test-ts.ts";

describe("worker crash attribution", () => {
	// A `bun test --parallel` worker that dies reports no `(fail)` marker, so
	// without worker-crash parsing the chunk's progress line and failure report
	// are unnamed and the gate has no file to rerun.
	test("names the file and keeps the buffered watchdog lines as detail", () => {
		const output = [
			"thing.test.ts:",
			"[stall-watchdog] chunk exceeded 600s; killed with SIGKILL",
			"\u2717 thing.test.ts (worker crashed: SIGKILL)",
			"",
		].join("\n");
		expect(extractFailingTests(output)).toEqual([
			{
				name: "thing.test.ts > worker crashed: SIGKILL",
				detail: "[stall-watchdog] chunk exceeded 600s; killed with SIGKILL",
			},
		]);
	});

	test("reports a worker crash as a failure naming the file", async () => {
		using dir = TempDir.createSync("omp-test-runner-worker-crash-");
		await Bun.write(
			dir.join("crash.test.ts"),
			'import { test } from "bun:test";\ntest("crashes", () => {\n\tprocess.kill(process.pid, "SIGKILL");\n});\n',
		);
		await Bun.write(
			dir.join("ok.test.ts"),
			'import { test } from "bun:test";\ntest("passes", () => {\n\texpect(1).toBe(1);\n});\n',
		);
		const commands = [
			{
				label: "crash chunk",
				cwd: ".",
				command: ["/bin/sh", "-c", `cd '${dir.path()}' && exec '${process.execPath}' test --parallel=2`],
			},
		];
		const result = await ptree.exec(
			[
				process.execPath,
				"-e",
				`import { runTestCommandsInParallel } from ${JSON.stringify(import.meta.resolve("./ci-test-ts.ts"))}; await runTestCommandsInParallel(${JSON.stringify(commands)}, 1);`,
			],
			{
				env: { ...Bun.env, NO_COLOR: "1" },
				timeout: 30_000,
				detached: true,
				allowNonZero: true,
			},
		);

		expect(result.exitCode).toBe(1);
		expect(result.stdout).toContain("\u2717 crash chunk");
		expect(result.stdout).toContain(" \u2014 crash.test.ts > worker crashed: SIGKILL");
	}, 40_000);
});
