import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as net from "node:net";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import * as brokerClients from "../../src/launch/client";
import { daemonBrokerEndpoint } from "../../src/launch/paths";
import { DAEMON_BROKER_WORKER_ARG } from "../../src/launch/protocol";

interface FakeConnection {
	socket: net.Socket;
	lines: string[];
	closed: Promise<void>;
}

interface FakeBroker {
	server: net.Server;
	connections: FakeConnection[];
	listen: (endpoint: string) => Promise<void>;
	close: () => Promise<void>;
}

function createFakeBroker(options: {
	projectDir: string;
	onPing?: (reply: () => void, line: string, socket: net.Socket) => void;
}): FakeBroker {
	const connections: FakeConnection[] = [];
	const server = net.createServer({ allowHalfOpen: true });

	server.on("connection", socket => {
		const lines: string[] = [];
		const { promise: closed, resolve: resolveClosed } = Promise.withResolvers<void>();
		socket.on("close", () => resolveClosed());
		connections.push({ socket, lines, closed });

		let buffer = "";
		socket.setEncoding("utf8");
		socket.on("data", chunk => {
			buffer += typeof chunk === "string" ? chunk : chunk.toString("utf8");
			for (;;) {
				const newline = buffer.indexOf("\n");
				if (newline < 0) return;
				const line = buffer.slice(0, newline);
				buffer = buffer.slice(newline + 1);
				if (line.length === 0) continue;
				lines.push(line);
				try {
					const msg = JSON.parse(line) as { id: string; operation?: { op: string } };
					if (msg.operation?.op === "shutdown") {
						socket.write(`${JSON.stringify({ id: msg.id, ok: true, result: { op: "shutdown" } })}\n`);
					} else if (msg.operation?.op === "ping") {
						const reply = (): void => {
							socket.write(
								`${JSON.stringify({
									id: msg.id,
									ok: true,
									result: { op: "ping", projectDir: options.projectDir },
								})}\n`,
							);
						};
						if (options.onPing) {
							options.onPing(reply, line, socket);
						} else {
							reply();
						}
					}
				} catch {
					// Ignore malformed lines in fake broker.
				}
			}
		});
	});

	const listen = async (endpoint: string): Promise<void> => {
		const { promise, resolve, reject } = Promise.withResolvers<void>();
		server.once("listening", resolve);
		server.once("error", reject);
		server.listen(endpoint);
		await promise;
	};

	const close = async (): Promise<void> => {
		if (!server.listening) return;
		const { promise, resolve, reject } = Promise.withResolvers<void>();
		server.close(err => (err ? reject(err) : resolve()));
		await promise;
	};

	return { server, connections, listen, close };
}

describe("broker client post shutdown close", () => {
	beforeEach(() => {
		const originalSpawn = Bun.spawn;
		vi.spyOn(Bun, "spawn").mockImplementation(((...args: unknown[]) => {
			const first = args[0];
			const cmd = Array.isArray(first) ? (first as string[]) : ((first as { cmd?: string[] })?.cmd ?? []);
			if (cmd.some(arg => typeof arg === "string" && arg.includes(DAEMON_BROKER_WORKER_ARG))) {
				throw new Error(`Unexpected broker spawn: ${cmd.join(" ")}`);
			}
			return Reflect.apply(originalSpawn, Bun, args);
		}) as never);
	});

	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("Case A: server A acks shutdown, keeps that connection, stops listening, removes the endpoint (non-win32); server B listens. ping resolves op:ping; A's connection got only the shutdown line", async () => {
		using tempDir = TempDir.createSync("@omp-post-shutdown-case-a-");
		const projectDir = path.join(tempDir.path(), "project");
		const runtimeDir = path.join(tempDir.path(), "runtime");
		await fs.mkdir(projectDir, { recursive: true });

		const client = await brokerClients.createDaemonBrokerClient(projectDir, { runtimeDir });
		const endpoint = daemonBrokerEndpoint(client.projectDir, runtimeDir);

		const serverA = createFakeBroker({ projectDir: client.projectDir });
		await serverA.listen(endpoint);

		const serverB = createFakeBroker({ projectDir: client.projectDir });

		try {
			const shutdownResult = await client.request({ op: "shutdown" });
			expect(shutdownResult).toEqual({ op: "shutdown" });
			expect(serverA.connections).toHaveLength(1);

			serverA.server.close();
			if (process.platform !== "win32") {
				await fs.rm(endpoint, { force: true });
			}

			await serverB.listen(endpoint);

			const pingResult = await client.request({ op: "ping" });
			expect(pingResult.op).toBe("ping");

			expect(serverA.connections[0].lines).toHaveLength(1);
			const lineObj = JSON.parse(serverA.connections[0].lines[0]) as { operation?: { op?: string } };
			expect(lineObj.operation?.op).toBe("shutdown");
		} finally {
			client.close();
			for (const conn of serverA.connections) conn.socket.destroy();
			for (const conn of serverB.connections) conn.socket.destroy();
			await serverA.close();
			await serverB.close();
		}
	});

	it("Case B: as A, but B holds the ping reply until it has the ping line, the test destroys A's connection, awaits its server-side close and Bun.sleep(50). ping then resolves", async () => {
		using tempDir = TempDir.createSync("@omp-post-shutdown-case-b-");
		const projectDir = path.join(tempDir.path(), "project");
		const runtimeDir = path.join(tempDir.path(), "runtime");
		await fs.mkdir(projectDir, { recursive: true });

		const client = await brokerClients.createDaemonBrokerClient(projectDir, { runtimeDir });
		const endpoint = daemonBrokerEndpoint(client.projectDir, runtimeDir);

		const serverA = createFakeBroker({ projectDir: client.projectDir });
		await serverA.listen(endpoint);

		const pingReceived = Promise.withResolvers<() => void>();
		const serverB = createFakeBroker({
			projectDir: client.projectDir,
			onPing: reply => pingReceived.resolve(reply),
		});

		try {
			const shutdownResult = await client.request({ op: "shutdown" });
			expect(shutdownResult).toEqual({ op: "shutdown" });
			expect(serverA.connections).toHaveLength(1);
			const connA = serverA.connections[0];

			serverA.server.close();
			if (process.platform !== "win32") {
				await fs.rm(endpoint, { force: true });
			}

			await serverB.listen(endpoint);

			const pingPromise = client.request({ op: "ping" });
			const sendPingReply = await pingReceived.promise;

			connA.socket.destroy();
			await connA.closed;
			await Bun.sleep(50);

			sendPingReply();
			const pingResult = await pingPromise;
			expect(pingResult.op).toBe("ping");
		} finally {
			client.close();
			for (const conn of serverA.connections) conn.socket.destroy();
			for (const conn of serverB.connections) conn.socket.destroy();
			await serverA.close();
			await serverB.close();
		}
	});
});
