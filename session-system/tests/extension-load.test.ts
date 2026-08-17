import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { loadExtensions } from "@oh-my-pi/pi-coding-agent";

const repoRoot = path.resolve(import.meta.dir, "../..");
const extPath = path.join(repoRoot, "session-system/extensions/linear-now.ts");

describe("linear-now extension vs current omp source", () => {
	test("loads with no errors and registers its full surface", async () => {
		const result = await loadExtensions([extPath], repoRoot);
		expect(result.errors).toEqual([]);
		expect(result.extensions).toHaveLength(1);
		const ext = result.extensions[0]!;
		for (const cmd of ["now", "done", "capture", "work"]) {
			expect(ext.commands.has(cmd)).toBe(true);
		}
		expect(ext.tools.has("work")).toBe(true);
	});
});
