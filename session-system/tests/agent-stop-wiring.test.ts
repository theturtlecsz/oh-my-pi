// OMP-405: wire the agent-stop gate into work-now and verify held session while stopped.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test, vi } from "bun:test";
import { type ExtensionContext, loadExtensions } from "@oh-my-pi/pi-coding-agent";

const repoRoot = path.resolve(import.meta.dir, "../..");
const extPath = path.join(repoRoot, "session-system/extensions/work-now.ts");

describe("work-now agent-stop gate wiring (OMP-405)", () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	test("wires agent-stop gate when client.json is configured", async () => {
		const tempConfig = fs.mkdtempSync(path.join(os.tmpdir(), "work-stop-wiring-"));
		const oldXdg = process.env.XDG_CONFIG_HOME;
		const oldBearer = process.env.OMP_WORK_BEARER;
		process.env.XDG_CONFIG_HOME = tempConfig;
		process.env.OMP_WORK_BEARER = "test-token";
		try {
			const workDir = path.join(tempConfig, "omp-work");
			fs.mkdirSync(workDir, { recursive: true });
			const workspaceId = "00000000-0000-7000-8000-000000000001";
			fs.writeFileSync(
				path.join(workDir, "client.json"),
				JSON.stringify({
					base_url: "http://127.0.0.1:54322",
					workspace_id: workspaceId,
					owner_id: "00000000-0000-7000-8000-000000000002",
				}),
			);

			vi.spyOn(globalThis, "fetch").mockResolvedValue(
				Response.json({
					workspace_id: workspaceId,
					stopped: true,
					reason: "owner halt",
					changed_at: null,
					changed_by_actor_kind: "owner",
				}),
			);

			const result = await loadExtensions([extPath], repoRoot);
			expect(result.errors).toEqual([]);
			expect(result.extensions).toHaveLength(1);
			const ext = result.extensions[0]!;

			let capturedCallback: (() => Promise<void>) | null = null;
			const notifies: Array<{ message: string; level?: string }> = [];
			let abortCount = 0;
			const fakeCtx = {
				ui: {
					notify: (message: string, level?: "info" | "warning" | "error") => {
						notifies.push({ message, level });
					},
				},
				isIdle: () => true,
				abort: () => {
					abortCount += 1;
				},
				setInterval: (callback: () => Promise<void>) => {
					capturedCallback = callback;
					return {};
				},
				clearTimer: () => {},
			};

			const sessionStartHandlers = ext.handlers.get("session_start");
			expect(sessionStartHandlers).toBeDefined();
			expect(sessionStartHandlers!.length).toBeGreaterThan(0);
			const sessionStart = sessionStartHandlers![0]!;
			await sessionStart({}, fakeCtx as unknown as ExtensionContext);

			expect(capturedCallback).toBeTypeOf("function");
			await capturedCallback!();

			const inputHandlers = ext.handlers.get("input");
			expect(inputHandlers).toBeDefined();
			expect(inputHandlers!.length).toBeGreaterThan(0);
			const inputResult = inputHandlers![0]!({}, fakeCtx as unknown as ExtensionContext);
			expect(inputResult).toEqual({ handled: true });

			const toolCallHandlers = ext.handlers.get("tool_call");
			expect(toolCallHandlers).toBeDefined();
			expect(toolCallHandlers!.length).toBeGreaterThan(0);
			const toolResult = toolCallHandlers![0]!({}, fakeCtx as unknown as ExtensionContext);
			expect((toolResult as { block?: boolean })?.block).toBe(true);
		} finally {
			if (oldBearer === undefined) delete process.env.OMP_WORK_BEARER;
			else process.env.OMP_WORK_BEARER = oldBearer;
			if (oldXdg === undefined) delete process.env.XDG_CONFIG_HOME;
			else process.env.XDG_CONFIG_HOME = oldXdg;
			fs.rmSync(tempConfig, { recursive: true, force: true });
		}
	});

	test("stays dormant with no gate input handler and no fetch when client.json is missing", async () => {
		const tempConfig = fs.mkdtempSync(path.join(os.tmpdir(), "work-stop-missing-"));
		const oldXdg = process.env.XDG_CONFIG_HOME;
		const oldBearer = process.env.OMP_WORK_BEARER;
		process.env.XDG_CONFIG_HOME = tempConfig;
		process.env.OMP_WORK_BEARER = "test-token";
		try {
			const fetchSpy = vi.spyOn(globalThis, "fetch");

			const result = await loadExtensions([extPath], repoRoot);
			expect(result.errors).toEqual([]);
			expect(result.extensions).toHaveLength(1);
			const ext = result.extensions[0]!;

			const inputHandlers = ext.handlers.get("input");
			expect(!inputHandlers || inputHandlers.length === 0).toBe(true);
			expect(fetchSpy).not.toHaveBeenCalled();
		} finally {
			if (oldBearer === undefined) delete process.env.OMP_WORK_BEARER;
			else process.env.OMP_WORK_BEARER = oldBearer;
			if (oldXdg === undefined) delete process.env.XDG_CONFIG_HOME;
			else process.env.XDG_CONFIG_HOME = oldXdg;
			fs.rmSync(tempConfig, { recursive: true, force: true });
		}
	});
});
