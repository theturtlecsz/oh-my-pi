// Integration test — real timers are required (ts-no-test-timers exception): this drives the actual
// cross-process daemon broker running a real child process, and the bug is a missing idle-shutdown
// rearm in #settle. Fake timers cannot control the OS process-exit promise or the unix-socket RPC,
// and shutdown is observed by awaiting the broker's own run() promise — its resolution IS the signal
// (no shutdown polling). A regression leaves the broker alive, so the test's own timeout
// surfaces the failure.
import { describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import { startDaemonBrokerFromEnvironment } from "../../src/launch/broker";
import { createDaemonBrokerClient } from "../../src/launch/client";
import { registerDaemonProjectPresence } from "../../src/launch/presence";
import { DAEMON_IDLE_GRACE_ENV, DAEMON_PROJECT_DIR_ENV, DAEMON_RUNTIME_DIR_ENV } from "../../src/launch/protocol";

function restoreEnv(name: string, value: string | undefined): void {
	if (value === undefined) delete process.env[name];
	else process.env[name] = value;
}

function startBroker(projectDir: string, runtimeDir: string, idleGraceMs: number): Promise<void> {
	const previousProjectDir = process.env[DAEMON_PROJECT_DIR_ENV];
	const previousRuntimeDir = process.env[DAEMON_RUNTIME_DIR_ENV];
	const previousGrace = process.env[DAEMON_IDLE_GRACE_ENV];
	process.env[DAEMON_PROJECT_DIR_ENV] = projectDir;
	process.env[DAEMON_RUNTIME_DIR_ENV] = runtimeDir;
	process.env[DAEMON_IDLE_GRACE_ENV] = String(idleGraceMs);
	const broker = startDaemonBrokerFromEnvironment();
	restoreEnv(DAEMON_PROJECT_DIR_ENV, previousProjectDir);
	restoreEnv(DAEMON_RUNTIME_DIR_ENV, previousRuntimeDir);
	restoreEnv(DAEMON_IDLE_GRACE_ENV, previousGrace);
	return broker;
}

describe("daemon broker idle shutdown", () => {
	it("shuts down after its last persistent daemon exits with no clients", async () => {
		using tempDir = TempDir.createSync("@omp-launch-idle-");
		const projectDir = path.join(tempDir.path(), "project");
		const runtimeDir = path.join(tempDir.path(), "runtime");
		await fs.mkdir(projectDir);

		const previousTitle = process.title;
		const idleGraceMs = 100;
		// Startup is not the idle-exit contract: keep the scope present until a real
		// authenticated client has started the child, even if CI delays connecting.
		const presence = await registerDaemonProjectPresence(projectDir, runtimeDir);
		// Create the client (writes broker.token) before starting the broker, which reads that token.
		const client = await createDaemonBrokerClient(projectDir, { runtimeDir, idleGraceMs });
		const broker = startBroker(projectDir, runtimeDir, idleGraceMs);
		const childReady = Promise.withResolvers<void>();
		const releaseChild = Promise.withResolvers<Response>();
		const exitGate = Bun.serve({
			hostname: "127.0.0.1",
			port: 0,
			fetch() {
				childReady.resolve();
				return releaseChild.promise;
			},
		});
		try {
			// The real child cannot exit until released, so slow start/response persistence
			// cannot consume its lifetime before the final client disconnects.
			const started = await client.request({
				op: "start",
				spec: {
					name: "persistent-temp",
					application: process.execPath,
					args: ["-e", "await fetch(process.env.OMP_TEST_EXIT_GATE)"],
					env: { OMP_TEST_EXIT_GATE: exitGate.url.href },
					cwd: projectDir,
					pty: false,
					restart: "no",
					persist: true,
					detached: false,
				},
			});
			expect(started.op).toBe("start");
			await childReady.promise;
			await presence.close();

			// Disconnect the final client. The broker keeps the persistent daemon alive, so the
			// idle timer this arms fires while the daemon is still live and returns without rearming.
			client.close();
			// With no client or project presence, only the live persistent child may
			// keep the broker alive beyond its idle grace.
			expect(
				await Promise.race([broker.then(() => "shutdown"), Bun.sleep(idleGraceMs * 2).then(() => "alive")]),
			).toBe("alive");
			releaseChild.resolve(new Response("exit"));

			// When the daemon self-exits, terminal settlement must rearm idle shutdown; the broker
			// then releases its lease and run() resolves. Awaiting the broker promise IS the shutdown
			// signal. Before the fix nothing rearmed, so this await never resolved and the test timed
			// out — the regression this guards.
			await broker;
		} finally {
			releaseChild.resolve(new Response("cleanup"));
			exitGate.stop(true);
			client.close();
			await presence.close();
			process.title = previousTitle;
		}
	}, 30_000);
});
