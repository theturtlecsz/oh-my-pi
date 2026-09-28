#!/usr/bin/env bun
/**
 * Stdio MCP server that withholds `initialize` until `SLOW_INIT_GATE` exists
 * on disk, then serves a concrete resource catalog. The gate is the
 * readiness signal: `connectServers` returns from its 250ms startup race
 * while this process is still blocked, so a read can be forced to observe
 * an in-flight connect on an idle machine.
 */
import * as fs from "node:fs";
import * as readline from "node:readline";

export const RESOURCE_URI = "test://alpha";

const GATE = process.env.SLOW_INIT_GATE ?? "";

type JsonRpcRequest = {
	jsonrpc: "2.0";
	id?: string | number;
	method: string;
	params?: Record<string, unknown>;
};

async function waitForGate(): Promise<void> {
	if (!GATE) return;
	const deadline = Date.now() + 20_000;
	while (!fs.existsSync(GATE)) {
		if (Date.now() > deadline) throw new Error("init gate timed out");
		await Bun.sleep(15);
	}
}

function buildResult(method: string, params?: Record<string, unknown>): Record<string, unknown> {
	switch (method) {
		case "initialize":
			return {
				protocolVersion: "2025-03-26",
				serverInfo: { name: "slow-initialize-resource-fixture", version: "1.0.0" },
				capabilities: { resources: {} },
			};
		case "resources/list":
			return { resources: [{ uri: RESOURCE_URI, name: "alpha" }] };
		case "resources/templates/list":
			return { resourceTemplates: [] };
		case "resources/read": {
			const uri = String(params?.uri ?? "");
			return { contents: [{ uri, text: `fixture content for ${uri}` }] };
		}
		default:
			return {};
	}
}

function startServer(): void {
	const rl = readline.createInterface({ input: process.stdin });
	rl.on("line", line => {
		void (async () => {
			const trimmed = line.trim();
			if (trimmed.length === 0) return;
			let msg: JsonRpcRequest;
			try {
				msg = JSON.parse(trimmed) as JsonRpcRequest;
			} catch {
				return;
			}
			if (msg.id === undefined || msg.id === null) return;
			if (msg.method === "initialize") await waitForGate();
			const response = { jsonrpc: "2.0" as const, id: msg.id, result: buildResult(msg.method, msg.params) };
			process.stdout.write(`${JSON.stringify(response)}\n`);
		})();
	});
	rl.on("close", () => process.exit(0));
}

if (import.meta.main) {
	startServer();
}
