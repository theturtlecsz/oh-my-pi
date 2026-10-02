import { describe, expect, test } from "bun:test";
import { ptree, TempDir } from "@oh-my-pi/pi-utils";

// The runner wraps every spawned chunk in `coreLimitedCommand` (ulimit -c 0) so a
// bun JSC GC crash cannot write core dumps. These tests drive the real spawn path
// in a child bun process — the same shape as the watchdog test in
// scripts/ci-test-ts.test.ts — because the wrapper only takes effect inside
// runTestCommandsInParallel/runTestCommand, not when imported.
describe.skipIf(process.platform === "win32")("ci-test-ts core dump limit", () => {
	test("runs each chunk with core dumps disabled", async () => {
		using dir = TempDir.createSync("omp-test-core-limit-");
		const softPath = dir.join("soft");
		const hardPath = dir.join("hard");
		const chunk = [
			"/bin/sh",
			"-c",
			`ulimit -c > ${JSON.stringify(softPath)}; ulimit -H -c > ${JSON.stringify(hardPath)}`,
		];
		const result = await ptree.exec(
			[
				process.execPath,
				"-e",
				`import { runTestCommandsInParallel } from ${JSON.stringify(import.meta.resolve("./ci-test-ts.ts"))}; await runTestCommandsInParallel([{ label: "core-limit", cwd: ".", command: ${JSON.stringify(chunk)} }], 1);`,
			],
			{ env: { ...Bun.env, NO_COLOR: "1" }, timeout: 10_000, allowNonZero: true },
		);

		expect(result.exitCode).toBe(0);
		expect((await Bun.file(softPath).text()).trim()).toBe("0");
		expect((await Bun.file(hardPath).text()).trim()).toBe("0");
	}, 15_000);

	test("still reports a chunk's failing exit code through the wrapper", async () => {
		using dir = TempDir.createSync("omp-test-core-limit-exit-");
		const outcomesPath = dir.join("outcomes.json");
		const chunk = ["/bin/sh", "-c", "exit 3"];
		const result = await ptree.exec(
			[
				process.execPath,
				"-e",
				`import { runTestCommandsInParallel } from ${JSON.stringify(import.meta.resolve("./ci-test-ts.ts"))}; const outcomes = await runTestCommandsInParallel([{ label: "failing", cwd: ".", command: ${JSON.stringify(chunk)} }], 1); await Bun.write(${JSON.stringify(outcomesPath)}, JSON.stringify(outcomes));`,
			],
			{ env: { ...Bun.env, NO_COLOR: "1" }, timeout: 10_000, allowNonZero: true },
		);

		expect(result.exitCode).toBe(1);
		const outcomes = (await Bun.file(outcomesPath).json()) as Array<{ exitCode: number }>;
		expect(outcomes).toHaveLength(1);
		expect(outcomes[0].exitCode).toBe(3);
	}, 15_000);
});
