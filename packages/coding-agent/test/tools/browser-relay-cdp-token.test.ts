import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { findFreeCdpPort } from "@oh-my-pi/pi-coding-agent/tools/browser/attach";
import { loadRelayCdpToken } from "@oh-my-pi/pi-coding-agent/tools/browser/relay/cdp-token";
import { probeRelayServer } from "@oh-my-pi/pi-coding-agent/tools/browser/relay/daemon";
import { type RelayServer, startRelayServer } from "@oh-my-pi/pi-coding-agent/tools/browser/relay/server";

const TOKEN = "relay-cdp-test-token";
const EXTENSION_HELLO = {
	t: "hello",
	userAgent: "test",
	browserVersion: "Chrome/151.0.0.0",
	tabs: [],
	attachedTabIds: [],
} as const;

function versionUrl(port: number, token?: string): string {
	const url = new URL(`http://127.0.0.1:${port}/json/version`);
	if (token !== undefined) url.searchParams.set("token", token);
	return url.toString();
}

async function cdpSocket(port: number, token?: string): Promise<"open" | "fail"> {
	const url = new URL(`ws://127.0.0.1:${port}/cdp`);
	if (token !== undefined) url.searchParams.set("token", token);
	const ws = new WebSocket(url);
	return await new Promise(resolve => {
		let settled = false;
		const finish = (result: "open" | "fail") => {
			if (settled) return;
			settled = true;
			clearTimeout(timer);
			ws.close();
			resolve(result);
		};
		const timer = setTimeout(() => finish("fail"), 2_000);
		ws.addEventListener("open", () => finish("open"), { once: true });
		ws.addEventListener("error", () => finish("fail"), { once: true });
		ws.addEventListener("close", () => finish("fail"), { once: true });
	});
}

describe("loadRelayCdpToken", () => {
	it("creates a mode-0600 token once and returns the same value next time", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-cdp-token-"));
		try {
			const first = await loadRelayCdpToken(dir);
			expect(first).toMatch(/^[0-9a-f]{64}$/);
			const file = path.join(dir, "cdp-token");
			const stat = await fs.stat(file);
			expect(stat.mode & 0o777).toBe(0o600);
			expect(await fs.readFile(file, "utf8")).toBe(first);
			expect(await loadRelayCdpToken(dir)).toBe(first);
			expect(await fs.readdir(dir)).toEqual(["cdp-token"]);
		} finally {
			await fs.rm(dir, { recursive: true, force: true });
		}
	});

	it("shares one token when two callers create the file together", async () => {
		const dir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-cdp-token-race-"));
		try {
			const [a, b] = await Promise.all([loadRelayCdpToken(dir), loadRelayCdpToken(dir)]);
			expect(a).toBe(b);
			expect(a).toMatch(/^[0-9a-f]{64}$/);
			expect(await fs.readdir(dir)).toEqual(["cdp-token"]);
			expect((await fs.stat(path.join(dir, "cdp-token"))).mode & 0o777).toBe(0o600);
		} finally {
			await fs.rm(dir, { recursive: true, force: true });
		}
	});
});

describe("relay CDP token gate", () => {
	let relay: RelayServer | undefined;
	let extension: WebSocket | undefined;

	afterEach(() => {
		extension?.close();
		relay?.stop();
		extension = undefined;
		relay = undefined;
	});

	it("rejects /json* without the token and advertises the token once the extension connects", async () => {
		const port = await findFreeCdpPort();
		relay = startRelayServer({ port, cdpToken: TOKEN });
		const sameLengthWrong = `${TOKEN.slice(0, -1)}${TOKEN.endsWith("a") ? "b" : "a"}`;

		expect((await fetch(versionUrl(port))).status).toBe(401);
		expect((await fetch(versionUrl(port, "nope"))).status).toBe(401);
		expect((await fetch(versionUrl(port, sameLengthWrong))).status).toBe(401);
		expect((await fetch(versionUrl(port, TOKEN))).status).toBe(503);
		expect((await fetch(`http://127.0.0.1:${port}/json`)).status).toBe(401);
		expect((await fetch(`http://127.0.0.1:${port}/json/list`)).status).toBe(401);
		expect((await fetch(`http://127.0.0.1:${port}/json?token=${TOKEN}`)).status).toBe(200);
		expect((await fetch(`http://127.0.0.1:${port}/json/list?token=${TOKEN}`)).status).toBe(200);

		extension = await new Promise<WebSocket>((resolve, reject) => {
			const ws = new WebSocket(`ws://127.0.0.1:${port}/ext`);
			ws.addEventListener(
				"open",
				() => {
					ws.send(JSON.stringify(EXTENSION_HELLO));
					resolve(ws);
				},
				{ once: true },
			);
			ws.addEventListener("error", () => reject(new Error("extension socket failed")), { once: true });
		});

		const deadline = Date.now() + 1_000;
		let body: Record<string, string> | undefined;
		while (Date.now() < deadline) {
			const response = await fetch(versionUrl(port, TOKEN));
			if (response.status === 200) {
				body = (await response.json()) as Record<string, string>;
				break;
			}
		}
		expect(body).toBeDefined();
		const advertised = new URL(body!.webSocketDebuggerUrl!);
		expect(advertised.protocol).toBe("ws:");
		expect(advertised.host).toBe(`127.0.0.1:${port}`);
		expect(advertised.pathname).toBe("/cdp");
		expect(advertised.searchParams.get("token")).toBe(TOKEN);
	});

	it("fails a /cdp websocket without the token and opens one with it", async () => {
		const port = await findFreeCdpPort();
		relay = startRelayServer({ port, cdpToken: TOKEN });
		expect(await cdpSocket(port)).toBe("fail");
		expect(await cdpSocket(port, "nope")).toBe("fail");
		expect(await cdpSocket(port, TOKEN)).toBe("open");
		const forbidden = await fetch(`http://127.0.0.1:${port}/cdp?token=${TOKEN}`, {
			headers: { Origin: "https://evil.example" },
		});
		expect(forbidden.status).toBe(403);
	});

	it("probeRelayServer treats HTTP 401 as a live relay", async () => {
		const server = Bun.serve({
			hostname: "127.0.0.1",
			port: 0,
			fetch() {
				return new Response("Unauthorized", { status: 401 });
			},
		});
		try {
			expect(await probeRelayServer(`http://127.0.0.1:${server.port}`)).toBe(true);
		} finally {
			server.stop(true);
		}
	});
});
