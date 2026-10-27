import { expect, test } from "bun:test";

const postmortemUrl = new URL("../src/postmortem.ts", import.meta.url).href;
const loaderUrl = new URL("../../natives/native/loader-state.js", import.meta.url).href;

async function probe(fail: boolean, code: number): Promise<{ code: number; stderr: string }> {
	const child = Bun.spawn([process.execPath, "--eval", `
		import * as fs from "node:fs";
		import { exitProcess } from ${JSON.stringify(postmortemUrl)};
		import { registerNativeTokioShutdown } from ${JSON.stringify(loaderUrl)};
		registerNativeTokioShutdown({ __ompShutdownTokioRuntime() {
			fs.writeSync(2, "shutdown-called\\n");
			if (${fail}) throw new Error("tid 123 still alive");
		} });
		exitProcess(${code});
	`], { stdin: "ignore", stdout: "ignore", stderr: "pipe" });
	const [exitCode, stderr] = await Promise.all([child.exited, new Response(child.stderr).text()]);
	return { code: exitCode, stderr };
}

test("exitProcess calls native shutdown exactly once before successful exit", async () => {
	const result = await probe(false, 0);
	expect(result.code).toBe(0);
	expect(result.stderr).toBe("shutdown-called\n");
});

test("native shutdown failure turns successful exit into failure and prints live tids", async () => {
	const result = await probe(true, 0);
	expect(result.code).toBe(1);
	expect(result.stderr).toBe("shutdown-called\nNative Tokio shutdown failed: tid 123 still alive\n");
});

test("native shutdown failure preserves an existing non-zero exit code", async () => {
	const result = await probe(true, 130);
	expect(result.code).toBe(130);
	expect(result.stderr).toContain("Native Tokio shutdown failed: tid 123 still alive");
});
