import { afterEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import { startDaemonBrokerFromEnvironment } from "../../src/launch/broker";
import * as brokerClients from "../../src/launch/client";
import {
	DAEMON_BROKER_WORKER_ARG,
	DAEMON_IDLE_GRACE_ENV,
	DAEMON_PROJECT_DIR_ENV,
	DAEMON_RUNTIME_DIR_ENV,
} from "../../src/launch/protocol";

interface EmbeddedBroker {
	/** Settles once this in-process broker has shut down and flushed its metadata. */
	finished: Promise<void>;
}

/**
 * Start the in-process broker and wait until it accepts connections. A client
 * that connects earlier spawns a broker process, which can claim the scope in a
 * broker-restart handoff; `finished` would then not track the broker that
 * flushes the metadata these tests read.
 */
async function startBroker(projectDir: string, runtimeDir: string): Promise<EmbeddedBroker> {
	const previousProjectDir = process.env[DAEMON_PROJECT_DIR_ENV];
	const previousRuntimeDir = process.env[DAEMON_RUNTIME_DIR_ENV];
	const previousGrace = process.env[DAEMON_IDLE_GRACE_ENV];
	process.env[DAEMON_PROJECT_DIR_ENV] = projectDir;
	process.env[DAEMON_RUNTIME_DIR_ENV] = runtimeDir;
	process.env[DAEMON_IDLE_GRACE_ENV] = "5000";
	const listening = Promise.withResolvers<boolean>();
	const finished = startDaemonBrokerFromEnvironment({ onListening: () => listening.resolve(true) });
	if (previousProjectDir === undefined) delete process.env[DAEMON_PROJECT_DIR_ENV];
	else process.env[DAEMON_PROJECT_DIR_ENV] = previousProjectDir;
	if (previousRuntimeDir === undefined) delete process.env[DAEMON_RUNTIME_DIR_ENV];
	else process.env[DAEMON_RUNTIME_DIR_ENV] = previousRuntimeDir;
	if (previousGrace === undefined) delete process.env[DAEMON_IDLE_GRACE_ENV];
	else process.env[DAEMON_IDLE_GRACE_ENV] = previousGrace;
	const claimed = await Promise.race([listening.promise, finished.then(() => false)]);
	if (!claimed) throw new Error("In-process daemon broker did not claim its scope");
	return { finished };
}

describe("broker client own shutdown", () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("does not spawn a broker after shutdown ack and second shutdown resolves under 1 s", async () => {
		using tempDir = TempDir.createSync("@omp-broker-shutdown-");
		const projectDir = path.join(tempDir.path(), "project");
		const runtimeDir = path.join(tempDir.path(), "runtime");
		await fs.mkdir(projectDir);
		const previousTitle = process.title;
		const client = await brokerClients.createDaemonBrokerClient(projectDir, { runtimeDir, idleGraceMs: 5_000 });
		const broker = await startBroker(projectDir, runtimeDir);
		const finished: Array<Promise<void>> = [broker.finished];
		client.onCompletion("test-session", () => {});

		const recordedSpawns: string[][] = [];
		const originalSpawn = Bun.spawn;
		vi.spyOn(Bun, "spawn").mockImplementation(((...args: unknown[]) => {
			const first = args[0];
			const cmd = Array.isArray(first) ? (first as string[]) : ((first as { cmd?: string[] })?.cmd ?? []);
			const isBrokerCmd = cmd.some(arg => typeof arg === "string" && arg.includes(DAEMON_BROKER_WORKER_ARG));
			if (isBrokerCmd) {
				recordedSpawns.push(cmd);
				return { unref() {} };
			}
			return Reflect.apply(originalSpawn, Bun, args);
		}) as never);

		try {
			const shutdownResult = await client.request({ op: "shutdown" });
			expect(shutdownResult).toEqual({ op: "shutdown" });
			await broker.finished;
			await Bun.sleep(500);
			expect(recordedSpawns).toHaveLength(0);

			const start = performance.now();
			const secondShutdownResult = await client.request({ op: "shutdown" });
			const durationMs = performance.now() - start;
			expect(secondShutdownResult).toEqual({ op: "shutdown" });
			expect(durationMs).toBeLessThan(1_000);
			expect(recordedSpawns).toHaveLength(0);
		} finally {
			await client.request({ op: "shutdown" }).catch(() => undefined);
			client.close();
			await Promise.all(finished);
			process.title = previousTitle;
		}
	}, 15_000);

	it("records one broker spawn and resolves when ping follows shutdown", async () => {
		using tempDir = TempDir.createSync("@omp-broker-ping-");
		const projectDir = path.join(tempDir.path(), "project");
		const runtimeDir = path.join(tempDir.path(), "runtime");
		await fs.mkdir(projectDir);
		const previousTitle = process.title;
		const client = await brokerClients.createDaemonBrokerClient(projectDir, { runtimeDir, idleGraceMs: 5_000 });
		const broker = await startBroker(projectDir, runtimeDir);
		const finished: Array<Promise<void>> = [broker.finished];
		client.onCompletion("test-session", () => {});

		const recordedSpawns: string[][] = [];
		const originalSpawn = Bun.spawn;
		vi.spyOn(Bun, "spawn").mockImplementation(((...args: unknown[]) => {
			const first = args[0];
			const cmd = Array.isArray(first) ? (first as string[]) : ((first as { cmd?: string[] })?.cmd ?? []);
			const isBrokerCmd = cmd.some(arg => typeof arg === "string" && arg.includes(DAEMON_BROKER_WORKER_ARG));
			if (isBrokerCmd) {
				recordedSpawns.push(cmd);
				const nextBroker = startBroker(projectDir, runtimeDir);
				finished.push(nextBroker.then(b => b.finished).catch(() => undefined));
				return { unref() {} };
			}
			return Reflect.apply(originalSpawn, Bun, args);
		}) as never);

		try {
			const shutdownResult = await client.request({ op: "shutdown" });
			expect(shutdownResult).toEqual({ op: "shutdown" });
			await broker.finished;

			const pingResult = await client.request({ op: "ping" });
			expect(pingResult.op).toBe("ping");
			expect(recordedSpawns).toHaveLength(1);
		} finally {
			await client.request({ op: "shutdown" }).catch(() => undefined);
			client.close();
			await Promise.all(finished);
			process.title = previousTitle;
		}
	}, 15_000);
});
