import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { Settings } from "../src/config/settings";
import { nativePlainReadProvenance, ReadTool } from "../src/tools/read";

let directory: string;
let tool: ReadTool;

beforeEach(async () => {
	directory = await fs.mkdtemp(path.join(os.tmpdir(), "native-read-provenance-"));
	tool = new ReadTool({
		cwd: directory,
		hasUI: false,
		getSessionFile: () => null,
		getSessionSpawns: () => "*",
		settings: Settings.isolated(),
	});
});

afterEach(async () => {
	await fs.rm(directory, { recursive: true, force: true });
});

describe("native read recovery provenance", () => {
	test("only original plain-file result can authorize its resolved file, not an equal object copy", async () => {
		const file = path.join(directory, "plain.txt");
		await Bun.write(file, "naïve\n");
		const result = await tool.execute("original-read", { path: "plain.txt" });
		expect(result.isError).not.toBe(true);
		expect(
			result.content
				.filter(part => part.type === "text")
				.map(part => part.text)
				.join("\n"),
		).toContain("naïve");
		expect(nativePlainReadProvenance(result)).toEqual({ kind: "local-file-text", resolvedPath: file, fileSize: 7 });
		const copied = { ...result, content: [...result.content], details: result.details };
		expect(nativePlainReadProvenance(copied)).toBeUndefined();
	});

	test.skipIf(process.platform === "win32")(
		"successful all-text device read cannot authorize regular-file recovery",
		async () => {
			expect((await fs.stat("/dev/null")).isCharacterDevice()).toBe(true);
			const result = await tool.execute("device-read", { path: "/dev/null" });
			expect(result.isError).not.toBe(true);
			expect(result.content).toEqual([{ type: "text", text: expect.any(String) }]);
			expect(nativePlainReadProvenance(result)).toBeUndefined();
		},
	);
});
