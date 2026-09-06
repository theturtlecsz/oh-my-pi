import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { loadExtensions } from "@oh-my-pi/pi-coding-agent";

import * as fs from "node:fs";
import * as os from "node:os";

const repoRoot = path.resolve(import.meta.dir, "../..");
const extPath = path.join(repoRoot, "session-system/extensions/work-now.ts");

describe("work-now extension vs current omp source", () => {
	test("loads with no errors and registers its full surface when configured", async () => {
		const tempConfig = fs.mkdtempSync(path.join(os.tmpdir(), "work-ext-load-"));
		try {
			const workDir = path.join(tempConfig, "omp-work");
			fs.mkdirSync(workDir, { recursive: true });
			fs.writeFileSync(
				path.join(workDir, "client.json"),
				JSON.stringify({
					base_url: "http://127.0.0.1:54322",
					workspace_id: "00000000-0000-7000-8000-000000000001",
					owner_id: "00000000-0000-7000-8000-000000000002",
				}),
			);
			const oldXdg = process.env.XDG_CONFIG_HOME;
			process.env.XDG_CONFIG_HOME = tempConfig;
			try {
				const result = await loadExtensions([extPath], repoRoot);
				expect(result.errors).toEqual([]);
				expect(result.extensions).toHaveLength(1);
				const ext = result.extensions[0]!;
				for (const cmd of ["now", "done", "capture", "work", "center"]) {
					expect(ext.commands.has(cmd)).toBe(true);
				}
				expect(ext.tools.has("work")).toBe(true);
			} finally {
				if (oldXdg === undefined) delete process.env.XDG_CONFIG_HOME;
				else process.env.XDG_CONFIG_HOME = oldXdg;
			}
		} finally {
			fs.rmSync(tempConfig, { recursive: true, force: true });
		}
	});

	test("stays dormant with no errors when client.json is missing", async () => {
		const tempConfig = fs.mkdtempSync(path.join(os.tmpdir(), "work-ext-dormant-"));
		const oldXdg = process.env.XDG_CONFIG_HOME;
		process.env.XDG_CONFIG_HOME = tempConfig;
		try {
			const result = await loadExtensions([extPath], repoRoot);
			expect(result.errors).toEqual([]);
			expect(result.extensions).toHaveLength(1);
			expect(result.extensions[0]!.tools.has("work")).toBe(false);
		} finally {
			if (oldXdg === undefined) delete process.env.XDG_CONFIG_HOME;
			else process.env.XDG_CONFIG_HOME = oldXdg;
			fs.rmSync(tempConfig, { recursive: true, force: true });
		}
	});

	test("stays dormant with no errors when client.json is malformed", async () => {
		const tempConfig = fs.mkdtempSync(path.join(os.tmpdir(), "work-ext-malformed-"));
		const oldXdg = process.env.XDG_CONFIG_HOME;
		process.env.XDG_CONFIG_HOME = tempConfig;
		try {
			const workDir = path.join(tempConfig, "omp-work");
			fs.mkdirSync(workDir, { recursive: true });
			fs.writeFileSync(
				path.join(workDir, "client.json"),
				JSON.stringify({ base_url: "http://127.0.0.1:54322", workspace_id: "not-a-uuid", owner_id: "also-not-a-uuid" }),
			);
			const result = await loadExtensions([extPath], repoRoot);
			expect(result.errors).toEqual([]);
			expect(result.extensions).toHaveLength(1);
			expect(result.extensions[0]!.tools.has("work")).toBe(false);
		} finally {
			if (oldXdg === undefined) delete process.env.XDG_CONFIG_HOME;
			else process.env.XDG_CONFIG_HOME = oldXdg;
			fs.rmSync(tempConfig, { recursive: true, force: true });
		}
	});
});
