import { describe, expect, it } from "bun:test";
import type { Subprocess } from "bun";
import * as fs from "node:fs/promises";
import * as path from "node:path";

const repoRoot = path.resolve(import.meta.dir, "../../..");
const cliEntry = path.join(repoRoot, "packages/coding-agent/src/cli.ts");
const candidate = "renewed-candidate";
const model = "pinned-worker-model";
const token = "pinned-model-token";
const cap = 73;

interface CapturedRequest {
	url: string;
	headers: Headers;
	body: string;
}
interface RunOptions {
	api?: "google-generative-ai" | "ollama-chat";
	lease?: string;
	deadline?: unknown;
	legacy?: boolean;
	endpointMismatch?: boolean;
	respond?: (index: number, request: CapturedRequest, child: Subprocess) => Response | Promise<Response>;
}
function sse(parts: unknown[]): Response {
	return new Response(
		`data: ${JSON.stringify({ candidates: [{ content: { role: "model", parts }, finishReason: "STOP" }] })}\n\n`,
		{ headers: { "content-type": "text/event-stream" } },
	);
}
function write(file: string, content: string): unknown {
	return { functionCall: { name: "write", args: { path: file, content } } };
}
async function runWorker(options: RunOptions = {}) {
	// Writable mounts belong to this test; /work never touches the host.
	const root = await fs.mkdtemp(path.join(repoRoot, ".renewed-worker-"));
	const requests: CapturedRequest[] = [];
	let child: Subprocess<"ignore", "pipe", "pipe"> | undefined;
	const server = Bun.serve({
		hostname: "127.0.0.1",
		port: 0,
		async fetch(request) {
			const captured = { url: request.url, headers: request.headers, body: await request.text() };
			requests.push(captured);
			if (!child) throw new Error("worker not started");
			return options.respond?.(requests.length - 1, captured, child) ?? sse([{ text: "done" }]);
		},
	});
	const proxyUrl = `http://127.0.0.1:${server.port}/pinned`;
	let timer: Timer | undefined;
	try {
		await fs.mkdir(path.join(root, "work/src"), { recursive: true });
		await fs.mkdir(path.join(root, "tmp"));
		await Bun.write(path.join(root, "work/src/allowed.txt"), "old\n");
		await Bun.write(
			path.join(root, "work/manifest.json"),
			JSON.stringify({
				version: 1,
				instruction: "Update allowed file, then finish.",
				approach: ["Stay inside granted paths."],
				allowed_paths: ["src/allowed.txt"],
				candidate_id: candidate,
				lease_expires_at: options.lease ?? "2000-01-01T00:00:00.000Z",
				...(options.legacy
					? {}
					: { task_deadline: Object.hasOwn(options, "deadline") ? options.deadline : "2099-01-01T00:00:00.000Z" }),
				model: {
					api: options.api ?? "google-generative-ai",
					id: model,
					limits: { max_output_tokens: cap, context_window: 8192 },
					proxy: { baseUrl: options.endpointMismatch ? `${proxyUrl}/different` : proxyUrl },
				},
			}),
		);
		const env: Record<string, string> = {
			NO_COLOR: "1",
			HOME: "/tmp",
			TMPDIR: "/tmp",
			OMP_WORKER_MANIFEST: "/work/manifest.json",
			OMP_MODEL_PROXY_URL: proxyUrl,
			OMP_MODEL_TOKEN: token,
		};
		for (const key of ["PATH", "LANG", "LD_LIBRARY_PATH"]) {
			const value = process.env[key];
			if (value !== undefined) env[key] = value;
		}
		child = Bun.spawn(
			[
				"bwrap",
				"--die-with-parent",
				"--tmpfs",
				"/",
				"--ro-bind",
				"/usr",
				"/usr",
				"--ro-bind",
				"/lib",
				"/lib",
				"--ro-bind",
				"/lib64",
				"/lib64",
				"--ro-bind",
				repoRoot,
				repoRoot,
				"--ro-bind",
				process.execPath,
				process.execPath,
				"--dev-bind",
				"/dev",
				"/dev",
				"--proc",
				"/proc",
				"--bind",
				path.join(root, "work"),
				"/work",
				"--bind",
				path.join(root, "tmp"),
				"/tmp",
				"--chdir",
				"/work",
				process.execPath,
				"--no-env-file",
				cliEntry,
				"orchestrator-worker",
			],
			{ cwd: root, env, stdin: "ignore", stdout: "pipe", stderr: "pipe" },
		);
		let timedOut = false;
		timer = setTimeout(() => {
			timedOut = true;
			child?.kill("SIGKILL");
		}, 15_000);
		const [exitCode, stdout, stderr] = await Promise.all([
			child.exited,
			new Response(child.stdout).text(),
			new Response(child.stderr).text(),
		]);
		expect(timedOut, `${stderr}\n${stdout}`).toBe(false);
		return {
			exitCode,
			stdout,
			stderr,
			requests,
			proxyUrl,
			signalCode: child.signalCode,
			file: await Bun.file(path.join(root, "work/src/allowed.txt")).text(),
			forbiddenExists: await Bun.file(path.join(root, "work/src/forbidden.txt")).exists(),
		};
	} finally {
		clearTimeout(timer);
		if (child && child.exitCode === null) child.kill("SIGKILL");
		if (child) await child.exited;
		server.stop(true);
		await fs.rm(root, { recursive: true, force: true });
	}
}
function assertPinned(requests: CapturedRequest[], proxyUrl: string): void {
	for (const request of requests) {
		expect(request.url).toBe(`${proxyUrl}/models/${model}:streamGenerateContent?alt=sse`);
		expect(request.headers.get("x-goog-api-key")).toBe(token);
		const body = JSON.parse(request.body) as {
			generationConfig: { maxOutputTokens: number };
			tools: { functionDeclarations: { name: string }[] }[];
		};
		expect(body.generationConfig.maxOutputTokens).toBe(cap);
		expect(body.tools.flatMap(tool => tool.functionDeclarations.map(declaration => declaration.name)).sort()).toEqual(
			["read", "write"],
		);
		expect(request.body).toContain(candidate);
		expect(request.body).toContain("src/allowed.txt");
	}
}

describe.serial("orchestrator renewed authority", () => {
	it("completes multiple calls after an obsolete lease while keeping grants pinned", async () => {
		const result = await runWorker({
			respond: index => (index === 0 ? sse([write("src/allowed.txt", "renewed\n")]) : sse([{ text: "done" }])),
		});
		expect(result.exitCode, result.stderr).toBe(0);
		expect(result.requests).toHaveLength(2);
		expect(result.file).toBe("renewed\n");
		expect(result.stdout).toContain(`"candidate_id":"${candidate}"`);
		expect(result.stdout).toContain('"result":"completed"');
		assertPinned(result.requests, result.proxyUrl);
	}, 20_000);
	it("accepts a delayed response spanning the obsolete lease", async () => {
		const oldLease = Date.now() + 2000;
		let started = 0;
		let finished = 0;
		const result = await runWorker({
			lease: new Date(oldLease).toISOString(),
			deadline: new Date(oldLease + 10_000).toISOString(),
			respond: async () => {
				started = Date.now();
				await Bun.sleep(Math.max(0, oldLease + 150 - Date.now()));
				finished = Date.now();
				return sse([{ text: "done" }]);
			},
		});
		expect(started).toBeLessThan(oldLease);
		expect(finished).toBeGreaterThan(oldLease);
		expect(result.exitCode, result.stderr).toBe(0);
		expect(result.requests).toHaveLength(1);
	}, 20_000);
	it("hard deadline interrupts an in-flight HTTP response", async () => {
		const pending = Promise.withResolvers<Response>();
		const deadline = Date.now() + 3000;
		try {
			const result = await runWorker({ deadline: new Date(deadline).toISOString(), respond: () => pending.promise });
			expect(result.requests).toHaveLength(1);
			expect(result.exitCode, result.stderr).not.toBe(0);
			expect(result.stdout).toContain('"stopReason":"aborted"');
			expect(Date.now()).toBeGreaterThanOrEqual(deadline - 50);
			expect(Date.now()).toBeLessThan(deadline + 3000);
		} finally {
			pending.resolve(sse([{ text: "too late" }]));
		}
	}, 20_000);
	it("parent cancellation terminates its child during an in-flight response", async () => {
		const pending = Promise.withResolvers<Response>();
		try {
			const result = await runWorker({
				respond: (_index, _request, child) => {
					child.kill("SIGTERM");
					return pending.promise;
				},
			});
			expect(result.requests).toHaveLength(1);
			expect(result.signalCode).toBe("SIGTERM");
			expect(result.stdout).not.toContain('"result":"completed"');
		} finally {
			pending.resolve(sse([{ text: "too late" }]));
		}
	}, 20_000);
	it("hard deadline interrupts a response body after headers arrive", async () => {
		const deadline = Date.now() + 3000;
		let body: ReadableStreamDefaultController<Uint8Array> | undefined;
		try {
			const result = await runWorker({
				deadline: new Date(deadline).toISOString(),
				respond: () =>
					new Response(
						new ReadableStream<Uint8Array>({
							start(controller) {
								body = controller;
								controller.enqueue(
									new TextEncoder().encode(
										'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"pending"}]}}]}\n\n',
									),
								);
							},
						}),
						{ headers: { "content-type": "text/event-stream" } },
					),
			});
			expect(result.requests).toHaveLength(1);
			expect(result.exitCode).not.toBe(0);
			expect(result.stdout).toContain('"stopReason":"aborted"');
			expect(Date.now()).toBeLessThan(deadline + 3000);
		} finally {
			// Error is harmless even if the disconnected response was already cancelled.
			body?.error(new Error("test response closed"));
		}
	}, 20_000);
	it("legacy expired lease refuses before HTTP", async () => {
		const result = await runWorker({ legacy: true });
		expect(result.exitCode).not.toBe(0);
		expect(result.stderr).toContain("deadline exceeded");
		expect(result.requests).toHaveLength(0);
	}, 20_000);
	it("legacy malformed lease retains timestamp validation", async () => {
		const result = await runWorker({ legacy: true, lease: "not-a-date" });
		expect(result.exitCode).not.toBe(0);
		expect(result.stderr).toContain("lease_expires_at is not a timestamp");
		expect(result.requests).toHaveLength(0);
	}, 20_000);
	it("expired task deadline refuses despite a future lease", async () => {
		const result = await runWorker({ lease: "2099-01-01T00:00:00.000Z", deadline: "2000-01-01T00:00:00.000Z" });
		expect(result.exitCode).not.toBe(0);
		expect(result.stderr).toContain("deadline exceeded");
		expect(result.requests).toHaveLength(0);
	}, 20_000);
	for (const deadline of ["not-a-date", "", null]) {
		it(`malformed task_deadline ${JSON.stringify(deadline)} never falls back to a valid lease`, async () => {
			const result = await runWorker({ lease: "2099-01-01T00:00:00.000Z", deadline });
			expect(result.exitCode).not.toBe(0);
			expect(result.stderr).toContain("task_deadline");
			expect(result.requests).toHaveLength(0);
		}, 20_000);
	}
	it("HTTP refusal stays sticky with no later request", async () => {
		const result = await runWorker({
			api: "ollama-chat",
			respond: index => (index === 0 ? new Response("refused", { status: 503 }) : sse([{ text: "done" }])),
		});
		expect(result.exitCode).not.toBe(0);
		expect(result.stderr).toContain("proxy refusal HTTP 503");
		expect(result.requests).toHaveLength(1);
		const request = result.requests[0]!;
		expect(request.url).toBe(`${result.proxyUrl}/api/chat`);
		expect(request.headers.get("authorization")).toBe(`Bearer ${token}`);
	}, 20_000);
	it("endpoint grant mismatch refuses with zero requests", async () => {
		const result = await runWorker({ endpointMismatch: true });
		expect(result.exitCode).not.toBe(0);
		expect(result.stderr).toContain("proxy baseUrl does not match");
		expect(result.requests).toHaveLength(0);
	}, 20_000);
	it("undeclared tool refusal stops without a subsequent model request", async () => {
		const result = await runWorker({
			respond: index =>
				index === 0
					? sse([{ functionCall: { name: "bash", args: { command: "touch src/forbidden.txt" } } }])
					: sse([{ text: "done" }]),
		});
		expect(result.exitCode).not.toBe(0);
		expect(result.requests).toHaveLength(1);
		expect(result.forbiddenExists).toBe(false);
		expect(result.file).toBe("old\n");
		expect(result.stdout).toContain('"result":"failed"');
		assertPinned(result.requests, result.proxyUrl);
	}, 20_000);
	it("forbidden path refusal cannot recover into an allowed write or another model call", async () => {
		const result = await runWorker({
			respond: index =>
				index === 0
					? sse([write("src/forbidden.txt", "refused\n"), write("src/allowed.txt", "must not run\n")])
					: sse([{ text: "done" }]),
		});
		expect(result.exitCode, result.stderr).not.toBe(0);
		expect(result.requests).toHaveLength(1);
		expect(result.file).toBe("old\n");
		expect(result.forbiddenExists).toBe(false);
		expect(result.stdout).toContain('"result":"failed"');
		assertPinned(result.requests, result.proxyUrl);
	}, 20_000);
});
