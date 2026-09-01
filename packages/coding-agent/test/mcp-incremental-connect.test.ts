/**
 * Incremental `connectServers` must keep tools for already-owned connections.
 *
 * `/mcp enable` and `/extensions` enable one server by calling
 * `connectServers({ [name]: config })` while others are already live. The
 * startup race used to assign `this.#tools = allTools` from only this call's
 * tasks, dropping every other server's tools even though those connections
 * stayed open.
 */
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { MCPManager } from "@oh-my-pi/pi-coding-agent/mcp/manager";
import type { MCPStdioServerConfig } from "@oh-my-pi/pi-coding-agent/mcp/types";
import { applyMcpToggleRuntime } from "@oh-my-pi/pi-coding-agent/modes/components/extensions/mcp-runtime";
import { removeSyncWithRetries } from "@oh-my-pi/pi-utils";
import { MANY_TOOL_COUNT, manyToolName } from "./fixtures/many-tools-mcp";

const FIXTURE_PATH = path.join(import.meta.dir, "fixtures", "many-tools-mcp.ts");

const SERVER_A = "alpha";
const SERVER_B = "bravo";
const TOOL_A = `mcp__${SERVER_A}_${manyToolName(0)}`;
const TOOL_B = `mcp__${SERVER_B}_${manyToolName(0)}`;

function fixtureConfig(): MCPStdioServerConfig {
	return { type: "stdio", command: process.execPath, args: [FIXTURE_PATH] };
}

/**
 * `connectServers` returns after `STARTUP_TIMEOUT_MS` (250 ms) and lets slow
 * cold-starting servers register tools via the background `#onToolsChanged`
 * chain (issue #2100 anti-blocking design). Await that signal before
 * asserting on the tool registry; the per-test timeout bounds the wait.
 */
function waitForToolCount(manager: MCPManager, count: number): Promise<void> {
	if (manager.getTools().length >= count) return Promise.resolve();
	const { promise, resolve } = Promise.withResolvers<void>();
	manager.setOnToolsChanged(tools => {
		if (tools.length >= count) resolve();
	});
	return promise;
}

describe("MCP incremental connectServers", () => {
	let workDir: string;
	let manager: MCPManager;

	beforeEach(() => {
		workDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-mcp-incremental-"));
		manager = new MCPManager(workDir);
	});

	afterEach(async () => {
		await manager.disconnectAll();
		removeSyncWithRetries(workDir);
	});

	it("keeps server A tools after incrementally connecting server B", async () => {
		await manager.connectServers({ [SERVER_A]: fixtureConfig() }, {});
		await waitForToolCount(manager, MANY_TOOL_COUNT);
		expect(manager.getConnectionStatus(SERVER_A)).toBe("connected");
		const afterA = manager.getTools();
		expect(afterA.map(t => t.name)).toContain(TOOL_A);
		expect(afterA).toHaveLength(MANY_TOOL_COUNT);
		expect(afterA.every(t => t.mcpServerName === SERVER_A)).toBe(true);

		const result = await manager.connectServers({ [SERVER_B]: fixtureConfig() }, {});
		await waitForToolCount(manager, MANY_TOOL_COUNT * 2);
		expect(manager.getConnectionStatus(SERVER_A)).toBe("connected");
		expect(manager.getConnectionStatus(SERVER_B)).toBe("connected");

		const tools = manager.getTools();
		expect(tools.map(t => t.name)).toContain(TOOL_A);
		expect(tools.map(t => t.name)).toContain(TOOL_B);
		expect(tools).toHaveLength(MANY_TOOL_COUNT * 2);
		expect(tools.filter(t => t.mcpServerName === SERVER_A)).toHaveLength(MANY_TOOL_COUNT);
		expect(tools.filter(t => t.mcpServerName === SERVER_B)).toHaveLength(MANY_TOOL_COUNT);
		// `result.tools` is the registry snapshot at return time; late-arriving
		// tools may only appear in the polled registry, never the reverse.
		expect(tools.map(t => t.name)).toEqual(expect.arrayContaining(result.tools.map(t => t.name)));
		expect(result.connectedServers).toContain(SERVER_B);
	}, 20_000);

	it("applyMcpToggleRuntime enable of B refreshes the A+B union", async () => {
		await manager.connectServers({ [SERVER_A]: fixtureConfig() }, {});
		await waitForToolCount(manager, MANY_TOOL_COUNT);
		expect(manager.getTools()).toHaveLength(MANY_TOOL_COUNT);

		const refreshed: string[][] = [];
		await applyMcpToggleRuntime({
			name: SERVER_B,
			enabled: true,
			cwd: workDir,
			manager,
			session: {
				refreshMCPTools: next => {
					refreshed.push(next.map(t => t.name));
				},
			},
			loadConfigs: async () => ({
				configs: { [SERVER_B]: fixtureConfig() },
				sources: {},
				exaApiKeys: [],
			}),
		});
		await waitForToolCount(manager, MANY_TOOL_COUNT * 2);

		expect(manager.getConnectionStatus(SERVER_A)).toBe("connected");
		expect(manager.getConnectionStatus(SERVER_B)).toBe("connected");
		const names = manager.getTools().map(t => t.name);
		expect(names).toContain(TOOL_A);
		expect(names).toContain(TOOL_B);
		expect(manager.getTools()).toHaveLength(MANY_TOOL_COUNT * 2);
		// The refresh callback receives the post-connect snapshot; server B's
		// tools may still be in flight on a cold start. The guarded regression
		// is that enabling B must never drop A's already-registered tools.
		const lastRefresh = refreshed.at(-1) ?? [];
		expect(lastRefresh.filter(name => name.startsWith(`mcp__${SERVER_A}_`))).toHaveLength(MANY_TOOL_COUNT);
	}, 20_000);

	it("notifies connection-status listeners on connect and transport loss", async () => {
		const events: Array<{ type: string; name?: string }> = [];
		const stop = manager.addConnectionStatusListener(event => {
			events.push({
				type: event.type,
				name: event.type === "connecting" ? event.serverNames[0] : event.serverName,
			});
		});
		await manager.connectServers({ [SERVER_A]: fixtureConfig() }, {});
		expect(events.some(event => event.type === "connecting" && event.name === SERVER_A)).toBe(true);
		expect(events.some(event => event.type === "connected" && event.name === SERVER_A)).toBe(true);

		const connection = manager.getConnection(SERVER_A);
		expect(connection).toBeDefined();
		connection?.transport.onClose?.();
		expect(events.some(event => event.type === "connecting" && event.name === SERVER_A)).toBe(true);
		stop();
	}, 20_000);
});
