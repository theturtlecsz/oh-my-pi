import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import { isTestSource, scan } from "./check-test-external-reads.ts";

const SCRIPT = path.join(import.meta.dir, "check-test-external-reads.ts");

// Line numbers are load-bearing: the test asserts hits carry the call's line.
const TS_SOURCE = [
	`import * as fs from "node:fs";`, // 1
	`const HOME_CFG = "/home/thetu/.omp/config.json";`, // 2
	``, // 3
	`fs.readFileSync("/home/thetu/.config/AGENTS.md", "utf8");`, // 4
	`fs.existsSync("/home/thetu/.omp");`, // 5
	`Bun.file("/home/thetu/.omp/agent.json");`, // 6
	`fs.readFileSync(HOME_CFG, "utf8");`, // 7
	`const nested = fs.readFileSync(path.join("/home/thetu", "nested.json"));`, // 8
	`const rootRead = fs.readFileSync("/root/only-root.ts");`, // 9
	`fs.readFileSync("/repo/local.ts");`, // 10
	`fs.readFileSync(process.env.HOME + "/x");`, // 11
	`expect(actual).toBe("/home/thetu/assert-only.json");`, // 12
	`const fixture = { path: "/home/thetu/fixture.json", home: "/home/thetu" };`, // 13
	`// fs.readFileSync("/home/thetu/commented.ts");`, // 14
].join("\n");

const PY_SOURCE = [
	`import os`, // 1
	`from pathlib import Path`, // 2
	``, // 3
	`ROOT_CFG = "/root/.config/app.json"`, // 4
	``, // 5
	`def load():`, // 6
	`    open("/root/.cache/data.txt", "r")`, // 7
	`    Path("/root/.omp/state.json").read_text()`, // 8
	`    os.path.exists("/root/.omp")`, // 9
	`    with open(ROOT_CFG) as fh:`, // 10
	`        pass`, // 11
	`    Path(ROOT_CFG).read_bytes()`, // 12
	`    open("./local.txt")`, // 13
].join("\n");

const tempDirs: string[] = [];

afterEach(async () => {
	await Promise.all(tempDirs.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

async function makeRepo(files: Record<string, string>): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-check-test-external-reads-"));
	tempDirs.push(dir);
	const init = await $`git -c init.defaultBranch=main init`.cwd(dir).quiet().nothrow();
	expect(init.exitCode).toBe(0);
	for (const [rel, content] of Object.entries(files)) {
		const target = path.join(dir, rel);
		await fs.mkdir(path.dirname(target), { recursive: true });
		await Bun.write(target, content);
	}
	const add = await $`git add -f ${Object.keys(files)}`.cwd(dir).quiet().nothrow();
	expect(add.exitCode).toBe(0);
	return dir;
}

describe("scan", () => {
	test("reports direct, const, path.join, and /root reads at their call lines", () => {
		expect(scan(TS_SOURCE, "tests/sample.test.ts")).toEqual([
			{ line: 4, call: "fs.readFileSync", path: "/home/thetu/.config/AGENTS.md" },
			{ line: 5, call: "fs.existsSync", path: "/home/thetu/.omp" },
			{ line: 6, call: "Bun.file", path: "/home/thetu/.omp/agent.json" },
			{ line: 7, call: "fs.readFileSync", path: "/home/thetu/.omp/config.json" },
			{ line: 8, call: "fs.readFileSync", path: "/home/thetu" },
			{ line: 9, call: "fs.readFileSync", path: "/root/only-root.ts" },
		]);
	});

	test("ignores home strings in assertions, fixture objects, env paths, and comments", () => {
		const lines = scan(TS_SOURCE, "tests/sample.test.ts").map(hit => hit.line);
		expect(lines).not.toContain(11);
		expect(lines).not.toContain(12);
		expect(lines).not.toContain(13);
		expect(lines).not.toContain(14);
	});

	test("reports Python open and Path().read_text()/read_bytes() /root reads at their call lines", () => {
		expect(scan(PY_SOURCE, "python/pkg/tests/test_sample.py")).toEqual([
			{ line: 7, call: "open", path: "/root/.cache/data.txt" },
			{ line: 8, call: "read_text", path: "/root/.omp/state.json" },
			{ line: 9, call: "os.path.exists", path: "/root/.omp" },
			{ line: 10, call: "open", path: "/root/.config/app.json" },
			{ line: 12, call: "read_bytes", path: "/root/.config/app.json" },
		]);
	});
});

describe("isTestSource", () => {
	test("accepts test files and tests/ trees, rejects sources and data files", () => {
		expect(isTestSource("scripts/foo.test.ts")).toBe(true);
		expect(isTestSource("packages/ai/test/foo.test.tsx")).toBe(true);
		expect(isTestSource("packages/ai/tests/helper.ts")).toBe(true);
		expect(isTestSource("python/omp-work/tests/test_runtime.py")).toBe(true);
		expect(isTestSource("python/omp-work/tests/runtime_test.py")).toBe(true);
		expect(isTestSource("packages/ai/src/index.ts")).toBe(false);
		expect(isTestSource("docs/upstream-fork-inventory.tsv")).toBe(false);
		expect(isTestSource("packages/ai/src/fixtures/sample.json")).toBe(false);
	});
});

describe("CLI", () => {
	test("exits 1 and prints file:line for a tracked test reading a home path", async () => {
		const planted = [
			`import * as fs from "node:fs";`,
			`const p = "/home/thetu/.omp/state.json";`,
			`fs.readFileSync(p, "utf8");`,
			``,
		].join("\n");
		const dir = await makeRepo({ "tests/planted.test.ts": planted });
		const proc = await $`bun ${SCRIPT}`.cwd(dir).quiet().nothrow();
		expect(proc.exitCode).toBe(1);
		expect(proc.stdout.toString()).toContain("tests/planted.test.ts:3: fs.readFileSync /home/thetu/.omp/state.json");
	});

	test("exits 0 and prints PASS when tracked tests read only in-repo paths", async () => {
		const clean = [
			`import * as fs from "node:fs";`,
			`const p = path.join(__dirname, "fixtures", "data.json");`,
			`fs.readFileSync(p, "utf8");`,
			``,
		].join("\n");
		const dir = await makeRepo({ "tests/clean.test.ts": clean });
		const proc = await $`bun ${SCRIPT}`.cwd(dir).quiet().nothrow();
		expect(proc.exitCode).toBe(0);
		expect(proc.stdout.toString()).toContain("PASS");
	});
});
