/**
 * `omp read` of an MCP resource goes through `McpProtocolHandler` after
 * `connectServers` has already returned from its 250ms startup race. These
 * tests pin the two outcomes that race must not blur:
 * - a server whose `initialize` lands after the race still yields its resource
 * - a server that never answers still produces the missing-resource error,
 *   and the wait is capped by `OMP_MCP_TIMEOUT_MS` rather than hanging
 */
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { InternalUrlRouter } from "@oh-my-pi/pi-coding-agent/internal-urls";
import { MCPManager } from "@oh-my-pi/pi-coding-agent/mcp/manager";
import type { MCPStdioServerConfig } from "@oh-my-pi/pi-coding-agent/mcp/types";
import { removeSyncWithRetries } from "@oh-my-pi/pi-utils";
import { RESOURCE_URI } from "./fixtures/slow-initialize-resource-mcp";

const SLOW_FIXTURE = path.join(import.meta.dir, "fixtures", "slow-initialize-resource-mcp.ts");
const HANG_FIXTURE = path.join(import.meta.dir, "fixtures", "hang-during-init-mcp.ts");

function stdioConfig(fixture: string): MCPStdioServerConfig {
	return { type: "stdio", command: process.execPath, args: [fixture] };
}

describe("MCP resource read readiness", () => {
	let workDir: string;
	let manager: MCPManager;
	let previousTimeout: string | undefined;

	beforeEach(() => {
		previousTimeout = process.env.OMP_MCP_TIMEOUT_MS;
		workDir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-mcp-resource-ready-"));
		manager = new MCPManager(workDir);
		MCPManager.setInstance(manager);
		InternalUrlRouter.resetForTests();
	});

	afterEach(async () => {
		if (previousTimeout === undefined) delete process.env.OMP_MCP_TIMEOUT_MS;
		else process.env.OMP_MCP_TIMEOUT_MS = previousTimeout;
		await manager.disconnectAll();
		MCPManager.resetForTests();
		InternalUrlRouter.resetForTests();
		removeSyncWithRetries(workDir);
	});

	it("reads a resource whose server finishes initialize after the startup race", async () => {
		process.env.OMP_MCP_TIMEOUT_MS = "15000";
		const gate = path.join(workDir, "init-gate");
		await manager.connectServers({ fixture: { ...stdioConfig(SLOW_FIXTURE), env: { SLOW_INIT_GATE: gate } } }, {});
		// The child cannot answer `initialize` until the gate exists, so the
		// startup race must have returned with the server still connecting.
		expect(manager.getConnectionStatus("fixture")).toBe("connecting");
		await fs.promises.writeFile(gate, "open");

		const router = InternalUrlRouter.instance();
		const resource = await router.resolve(RESOURCE_URI);
		expect(resource.content).toContain("fixture content for test://alpha");
		expect(resource.content).not.toContain("test://missing");

		await expect(router.resolve("test://missing")).rejects.toThrow('No MCP server has resource "test://missing"');
		await expect(router.resolve("test://missing")).rejects.toThrow("test://alpha");
	}, 20_000);

	it("reports a missing resource when the server never answers, within the MCP timeout", async () => {
		process.env.OMP_MCP_TIMEOUT_MS = "1000";
		await manager.connectServers({ hang: stdioConfig(HANG_FIXTURE) }, {});

		const started = performance.now();
		let error: unknown;
		try {
			await InternalUrlRouter.instance().resolve("test://missing");
		} catch (caught) {
			error = caught;
		}
		const elapsed = performance.now() - started;

		expect(error).toBeInstanceOf(Error);
		const message = error instanceof Error ? error.message : "";
		expect(message).toContain('No MCP server has resource "test://missing"');
		expect(message).toContain("(none)");
		// `OMP_MCP_TIMEOUT_MS` is 1s. Crossing 8s means the read ignored that
		// bound and fell through to the 30s default.
		expect(elapsed).toBeLessThan(8_000);
	}, 20_000);
});
