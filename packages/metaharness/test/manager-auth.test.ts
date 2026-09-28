import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { ManagerServer } from "../src/server";

/**
 * Contracts under test (E0131/V07):
 *  - start() binds loopback only, never all interfaces.
 *  - With a token, `/api/*` requires `Authorization: Bearer` (or `?token=` on
 *    `/api/events`, which EventSource cannot header) and answers 401 JSON
 *    without it; the static dashboard route stays open.
 *  - The CLI prints a loopback URL carrying the token that unlocks the API,
 *    and `--token` overrides the environment.
 */

const cleanups: Array<() => void> = [];
afterEach(() => {
	while (cleanups.length) cleanups.pop()?.();
});

function makeJobsDir(): string {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "metaharness-auth-"));
	cleanups.push(() => fs.rmSync(dir, { recursive: true, force: true }));
	return dir;
}

const PID_ALIVE = (pid: number): boolean => {
	try {
		process.kill(pid, 0);
		return true;
	} catch {
		return false;
	}
};

describe("ManagerServer API token", () => {
	it("binds loopback and gates /api/* behind a bearer token", async () => {
		const jobsDir = makeJobsDir();
		const manager = new ManagerServer(jobsDir);
		const server = manager.start(0, { token: "s3cret" });
		cleanups.push(() => {
			void manager.stop();
		});
		const base = `http://127.0.0.1:${server.port}`;

		expect(server.hostname).toBe("127.0.0.1");

		// No token / wrong token → 401 JSON; correct bearer → 200.
		const noToken = await fetch(`${base}/api/runs`);
		expect(noToken.status).toBe(401);
		expect(noToken.headers.get("content-type")).toContain("application/json");
		expect(await noToken.json()).toEqual({ error: "unauthorized" });
		expect((await fetch(`${base}/api/runs`, { headers: { authorization: "Bearer wrong" } })).status).toBe(401);
		const authorized = await fetch(`${base}/api/runs`, {
			headers: { authorization: "Bearer s3cret" },
		});
		expect(authorized.status).toBe(200);
		expect(await authorized.json()).toEqual([]);

		// The static dashboard route stays open (no token needed to load it).
		const page = await fetch(`${base}/`);
		expect(page.status).toBe(200);

		// SSE: EventSource cannot set headers, so ?token= is accepted; a bare
		// request is 401.
		const sse = await fetch(`${base}/api/events?token=s3cret`);
		expect(sse.status).toBe(200);
		expect(sse.headers.get("content-type")).toBe("text/event-stream");
		expect((await fetch(`${base}/api/events`)).status).toBe(401);
		await sse.body?.cancel();
	});

	it("leaves the API open when no token is configured", async () => {
		const jobsDir = makeJobsDir();
		const manager = new ManagerServer(jobsDir);
		const server = manager.start(0);
		cleanups.push(() => {
			void manager.stop();
		});
		expect(server.hostname).toBe("127.0.0.1");
		expect((await fetch(`http://127.0.0.1:${server.port}/api/runs`)).status).toBe(200);
	});
});

describe("metaharness server CLI", () => {
	it("prints a loopback URL whose token unlocks /api/runs", async () => {
		const jobsDir = makeJobsDir();
		// Port 0 is not accepted by parseServerArgs; pick a free one without
		// holding it (Bun.serve in the child rebinds it).
		const probe = Bun.serve({ port: 0, hostname: "127.0.0.1", fetch: () => new Response("") });
		const port = probe.port;
		probe.stop(true);

		const proc = Bun.spawn(
			["bun", "src/server.ts", "--port", String(port), "--jobs-dir", jobsDir, "--token", "cli-token"],
			{
				cwd: path.resolve(import.meta.dir, ".."),
				stdout: "pipe",
				stderr: "pipe",
			},
		);
		cleanups.push(() => {
			try {
				proc.kill(9);
			} catch {}
		});

		const reader = (proc.stdout as ReadableStream<Uint8Array>).getReader();
		let line = "";
		while (!line.includes("listening on")) {
			const { value, done } = await reader.read();
			if (done) throw new Error(`server exited before printing its URL: ${line}`);
			line += new TextDecoder().decode(value);
		}
		await reader.cancel();

		const match = line.match(/http:\/\/127\.0\.0\.1:(\d+)\/\?token=(\S+)/);
		expect(match).not.toBeNull();
		const [, printedPort, printedToken] = match!;
		expect(Number(printedPort)).toBe(port);
		expect(printedToken).toBe("cli-token");

		const url = `http://127.0.0.1:${printedPort}`;
		expect((await fetch(`${url}/api/runs`)).status).toBe(401);
		const ok = await fetch(`${url}/api/runs`, { headers: { authorization: `Bearer ${printedToken}` } });
		expect(ok.status).toBe(200);

		proc.kill(9);
		await proc.exited;
		expect(PID_ALIVE(proc.pid)).toBe(false);
	});
});
