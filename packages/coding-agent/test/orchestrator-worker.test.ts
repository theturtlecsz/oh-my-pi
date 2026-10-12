import { describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

// Headless `orchestrator-worker` turns. The child pivots into a private root so
// the scoped tools' hardcoded `/work` is the fixture. A planted candidate `.env`
// sits on that worktree and on the HOME the process starts with.

const repoRoot = path.resolve(import.meta.dir, "../../..");
const cliEntry = path.join(repoRoot, "packages/coding-agent/src/cli.ts");
const compileScript = path.join(repoRoot, "packages/coding-agent/scripts/compile-binary.ts");
const MODEL_TOKEN = "model-token-real";
const TASK_TOKEN = "task-token-must-not-leak";
const PLANTED_PROXY = "http://127.0.0.1:9/planted";
const PLANTED_SECRETS = [
	"planted-gemini",
	"planted-ollama",
	"planted-openai",
	"planted-token",
	"planted-task-token",
	TASK_TOKEN,
	PLANTED_PROXY,
];
const APPROACH = "touch only the allowed file";
const CANDIDATE_ID = "cand-1";
const MODEL_ID = "worker-granted";
const MAX_OUTPUT_TOKENS = 64;

const PLANTED_ENV = [
	"GEMINI_API_KEY=planted-gemini",
	"OLLAMA_API_KEY=planted-ollama",
	"OPENAI_API_KEY=planted-openai",
	`OMP_MODEL_PROXY_URL=${PLANTED_PROXY}`,
	"OMP_MODEL_TOKEN=planted-token",
	"OMP_TASK_TOKEN=planted-task-token",
	"",
].join("\n");

interface CapturedRequest {
	url: string;
	headers: Headers;
	body: string;
}

interface WorkerRun {
	exitCode: number;
	stdout: string;
	stderr: string;
	requests: CapturedRequest[];
	fileText: string;
	forbiddenExists: boolean;
	plantedEnv: string;
}

type WorkerApi = "google-generative-ai" | "ollama-chat";

function childEnv(extra: Record<string, string>): Record<string, string> {
	const env: Record<string, string> = { NO_COLOR: "1" };
	for (const key of ["PATH", "LANG", "LC_ALL", "TZ", "TERM", "USER", "LOGNAME", "LD_LIBRARY_PATH"]) {
		const value = process.env[key];
		if (value !== undefined) env[key] = value;
	}
	return { ...env, ...extra };
}

function geminiSse(parts: unknown[]): string {
	return `data: ${JSON.stringify({
		candidates: [{ content: { role: "model", parts }, finishReason: "STOP" }],
	})}\n\n`;
}

function ollamaLine(message: unknown, doneReason: string): string {
	return `${JSON.stringify({ message, done: true, done_reason: doneReason })}\n`;
}

function declaredToolNames(body: string): string[] {
	const json = JSON.parse(body) as { tools?: unknown };
	if (!Array.isArray(json.tools)) return [];
	const names: string[] = [];
	for (const tool of json.tools) {
		if (!tool || typeof tool !== "object") continue;
		const record = tool as {
			name?: unknown;
			function?: { name?: unknown };
			functionDeclarations?: unknown;
		};
		if (typeof record.name === "string") names.push(record.name);
		if (typeof record.function?.name === "string") names.push(record.function.name);
		if (Array.isArray(record.functionDeclarations)) {
			for (const decl of record.functionDeclarations) {
				if (decl && typeof decl === "object" && typeof (decl as { name?: unknown }).name === "string") {
					names.push((decl as { name: string }).name);
				}
			}
		}
	}
	return names;
}

function outputCap(body: string, api: WorkerApi): number | undefined {
	const json = JSON.parse(body) as {
		generationConfig?: { maxOutputTokens?: number };
		options?: { num_predict?: number };
	};
	return api === "google-generative-ai" ? json.generationConfig?.maxOutputTokens : json.options?.num_predict;
}

function assertPinned(requests: CapturedRequest[], api: WorkerApi, proxyUrl: string): void {
	expect(requests.length).toBeGreaterThan(0);
	for (const request of requests) {
		expect(request.url.startsWith(`${proxyUrl}/`)).toBe(true);
		const headerBlob = [...request.headers.entries()].map(([name, value]) => `${name}: ${value}`).join("\n");
		const wire = `${request.url}\n${headerBlob}\n${request.body}`;
		for (const secret of PLANTED_SECRETS) {
			expect(wire.includes(secret)).toBe(false);
		}
		if (api === "google-generative-ai") {
			expect(request.headers.get("x-goog-api-key")).toBe(MODEL_TOKEN);
			expect(request.url).toContain(`/models/${MODEL_ID}:streamGenerateContent`);
		} else {
			expect(request.headers.get("authorization")).toBe(`Bearer ${MODEL_TOKEN}`);
			expect(new URL(request.url).pathname).toBe("/api/chat");
			expect((JSON.parse(request.body) as { model?: string }).model).toBe(MODEL_ID);
		}
		expect(declaredToolNames(request.body).sort()).toEqual(["read", "write"]);
		expect(outputCap(request.body, api)).toBe(MAX_OUTPUT_TOKENS);
		expect(request.body).toContain(CANDIDATE_ID);
		expect(request.body).toContain(APPROACH);
	}
}

const SYSTEM_ENTRY_NAMES = ["bin", "lib", "lib64", "sbin", "lib32", "libx32"] as const;
type SystemEntryName = (typeof SYSTEM_ENTRY_NAMES)[number];

interface HostSystemLayout {
	symlinks: Array<{ name: SystemEntryName; target: string }>;
	realDirs: SystemEntryName[];
}

function resolveHostSystemLayout(): HostSystemLayout {
	const symlinks: Array<{ name: SystemEntryName; target: string }> = [];
	const realDirs: SystemEntryName[] = [];
	for (const name of SYSTEM_ENTRY_NAMES) {
		const hostPath = `/${name}`;
		try {
			const stat = fs.lstatSync(hostPath);
			if (stat.isSymbolicLink()) {
				symlinks.push({ name, target: fs.readlinkSync(hostPath) });
			} else if (stat.isDirectory()) {
				realDirs.push(name);
			}
		} catch {
			// absent on this host
		}
	}
	return { symlinks, realDirs };
}

function populateHostSystemLayout(root: string, layout: HostSystemLayout = resolveHostSystemLayout()): void {
	for (const { name, target } of layout.symlinks) {
		fs.symlinkSync(target, path.join(root, name));
	}
	for (const name of layout.realDirs) {
		fs.mkdirSync(path.join(root, name));
	}
}

function getBinaryInterpreter(binaryPath: string): string | null {
	try {
		const proc = Bun.spawnSync(["readelf", "-l", binaryPath]);
		if (proc.exitCode === 0) {
			const match = proc.stdout.toString().match(/Requesting program interpreter:\s*([^\]]+)\]/);
			if (match?.[1]) return match[1].trim();
		}
	} catch {}
	try {
		const fd = fs.openSync(binaryPath, "r");
		try {
			const buf = Buffer.alloc(64);
			fs.readSync(fd, buf, 0, 64, 0);
			if (buf.subarray(0, 4).toString("latin1") === "\x7fELF" && buf[4] === 2) {
				const phoff = Number(buf.readBigUInt64LE(32));
				const phentsize = buf.readUInt16LE(54);
				const phnum = buf.readUInt16LE(56);
				const phBuf = Buffer.alloc(phentsize * phnum);
				fs.readSync(fd, phBuf, 0, phBuf.length, phoff);
				for (let i = 0; i < phnum; i++) {
					const entry = phBuf.subarray(i * phentsize, (i + 1) * phentsize);
					if (entry.readUInt32LE(0) === 3) {
						const offset = Number(entry.readBigUInt64LE(8));
						const size = Number(entry.readBigUInt64LE(32));
						const interpBuf = Buffer.alloc(size);
						fs.readSync(fd, interpBuf, 0, size, offset);
						return interpBuf.toString("utf8").replace(/\0+$/, "");
					}
				}
			}
		} finally {
			fs.closeSync(fd);
		}
	} catch {}
	return null;
}

/**
 * User and mount namespace whose `/work` is `root/work`. Host `/usr` is bound
 * in so the dynamic linker survives `pivot_root`; host system layout is mirrored
 * so the dynamic linker loader path exists; the resolved Bun binary and
 * checkout are bound under `/run` regardless of their host paths.
 */
function pivotScript(root: string, layout: HostSystemLayout = resolveHostSystemLayout()): string {
	const realDirBinds = layout.realDirs.map(name => `mount --bind /${name} ${root}/${name}\n`).join("");
	return `
set -euo pipefail
mount --make-rprivate /
mount --bind /usr ${root}/usr
mount --rbind /proc ${root}/proc
mount --rbind /dev ${root}/dev
mount --rbind /sys ${root}/sys
${realDirBinds}touch ${root}/run/bun
mount --bind ${fs.realpathSync(process.execPath)} ${root}/run/bun
mkdir ${root}/run/checkout
mount --bind ${repoRoot} ${root}/run/checkout
mount --rbind ${root} ${root}
cd ${root}
pivot_root . old
cd /work
exec /run/bun --no-env-file /run/checkout/packages/coding-agent/src/cli.ts orchestrator-worker
`;
}

async function runWorker(
	api: WorkerApi,
	respond: (index: number, request: CapturedRequest) => Response,
): Promise<WorkerRun> {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "omp-worker-test-"));
	const layout = resolveHostSystemLayout();
	const requests: CapturedRequest[] = [];
	const server = Bun.serve({
		hostname: "127.0.0.1",
		port: 0,
		async fetch(request) {
			const captured = { url: request.url, headers: request.headers, body: await request.text() };
			requests.push(captured);
			return respond(requests.length - 1, captured);
		},
	});
	const proxyUrl = `http://127.0.0.1:${server.port}`;
	try {
		for (const dir of ["old", "work", "tmp", "proc", "dev", "sys", "usr", "run"]) {
			fs.mkdirSync(path.join(root, dir));
		}
		populateHostSystemLayout(root, layout);
		fs.symlinkSync("old/home", path.join(root, "home"));
		const work = path.join(root, "work");
		fs.mkdirSync(path.join(work, "src"));
		fs.mkdirSync(path.join(work, "planted-home"));
		fs.writeFileSync(path.join(work, "src/a.py"), "old\n");
		fs.writeFileSync(path.join(work, ".env"), PLANTED_ENV);
		fs.writeFileSync(path.join(work, "planted-home/.env"), PLANTED_ENV);
		fs.writeFileSync(
			path.join(work, ".omp-worker-manifest.json"),
			JSON.stringify({
				version: 1,
				instruction: "Update the allowed fixture.",
				approach: [APPROACH],
				allowed_paths: ["src/*.py"],
				candidate_id: CANDIDATE_ID,
				lease_expires_at: "2099-01-01T00:00:00.000Z",
				model: {
					api,
					id: MODEL_ID,
					limits: { max_output_tokens: MAX_OUTPUT_TOKENS, context_window: 8192 },
					proxy: { baseUrl: proxyUrl },
				},
			}),
		);
		const proc = Bun.spawn(
			["unshare", "--user", "--map-root-user", "--mount", "bash", "-c", pivotScript(root, layout)],
			{
				cwd: root,
				env: childEnv({
					HOME: "/work/planted-home",
					USERPROFILE: "/work/planted-home",
					TMPDIR: "/tmp",
					OMP_MODEL_PROXY_URL: proxyUrl,
					OMP_MODEL_TOKEN: MODEL_TOKEN,
					OMP_TASK_TOKEN: TASK_TOKEN,
					OMP_WORKER_MANIFEST: "/work/.omp-worker-manifest.json",
					PI_NO_THINKING_LOOP_GUARD: "1",
				}),
				stdin: "ignore",
				stdout: "pipe",
				stderr: "pipe",
			},
		);
		const timer = setTimeout(() => proc.kill(), 150_000);
		let exitCode: number;
		let stdout: string;
		let stderr: string;
		try {
			[exitCode, stdout, stderr] = await Promise.all([
				proc.exited,
				new Response(proc.stdout).text(),
				new Response(proc.stderr).text(),
			]);
		} finally {
			clearTimeout(timer);
			if (proc.exitCode === null) proc.kill();
		}
		return {
			exitCode,
			stdout,
			stderr,
			requests,
			fileText: fs.readFileSync(path.join(work, "src/a.py"), "utf8"),
			forbiddenExists: fs.existsSync(path.join(work, "notes.txt")),
			plantedEnv: fs.readFileSync(path.join(work, ".env"), "utf8"),
		};
	} finally {
		server.stop(true);
		fs.rmSync(root, { recursive: true, force: true });
	}
}

describe.serial("orchestrator-worker", () => {
	it("keeps dotenv autoload off in the compiled entry", () => {
		const source = fs.readFileSync(compileScript, "utf8");
		expect(source).toContain("autoloadDotenv: false");
	});

	it("enters the worker branch instead of launch when the manifest is missing", async () => {
		const home = fs.mkdtempSync(path.join(os.tmpdir(), "omp-worker-cli-"));
		try {
			const proc = Bun.spawn([process.execPath, "--no-env-file", cliEntry, "orchestrator-worker"], {
				cwd: home,
				env: childEnv({ HOME: home, USERPROFILE: home }),
				stdin: "ignore",
				stdout: "pipe",
				stderr: "pipe",
			});
			const timer = setTimeout(() => proc.kill(), 120_000);
			let exitCode: number;
			let stdout: string;
			let stderr: string;
			try {
				[exitCode, stdout, stderr] = await Promise.all([
					proc.exited,
					new Response(proc.stdout).text(),
					new Response(proc.stderr).text(),
				]);
			} finally {
				clearTimeout(timer);
			}
			const output = `${stdout}\n${stderr}`;
			expect(exitCode, output).not.toBe(0);
			expect(stderr).toContain("orchestrator-worker:");
			expect(output).not.toContain("No models available");
			expect(output).not.toContain("interactive mode requires a terminal");
		} finally {
			fs.rmSync(home, { recursive: true, force: true });
		}
	}, 150_000);

	it("mirrors the host /lib64 layout so the dynamic loader is present in the pivoted root", () => {
		const root = fs.mkdtempSync(path.join(os.tmpdir(), "omp-layout-test-"));
		try {
			const layout = resolveHostSystemLayout();
			populateHostSystemLayout(root, layout);

			const hostLib64 = "/lib64";
			if (fs.existsSync(hostLib64)) {
				const hostStat = fs.lstatSync(hostLib64);
				const rootLib64 = path.join(root, "lib64");
				const rootStat = fs.lstatSync(rootLib64);

				if (hostStat.isSymbolicLink()) {
					expect(rootStat.isSymbolicLink()).toBe(true);
					expect(fs.readlinkSync(rootLib64)).toBe(fs.readlinkSync(hostLib64));
				} else if (hostStat.isDirectory()) {
					expect(rootStat.isDirectory()).toBe(true);
					expect(layout.realDirs).toContain("lib64");
				}
			}

			const interp = getBinaryInterpreter(fs.realpathSync(process.execPath));
			expect(interp).not.toBeNull();
			if (interp) {
				// Inside the pivoted root host /usr is mounted at /usr and real directories are bound.
				// For this layout resolution check, symlink /usr to host /usr and point any realDirs to host.
				fs.symlinkSync("/usr", path.join(root, "usr"));
				for (const dir of layout.realDirs) {
					const target = path.join(root, dir);
					if (fs.existsSync(target) && fs.lstatSync(target).isDirectory()) {
						fs.rmdirSync(target);
						fs.symlinkSync(`/${dir}`, target);
					}
				}
				const loaderInRoot = path.join(root, interp);
				expect(fs.existsSync(loaderInRoot)).toBe(true);
				expect(fs.realpathSync(loaderInRoot)).toBe(fs.realpathSync(interp));
			}
		} finally {
			fs.rmSync(root, { recursive: true, force: true });
		}
	});

	it("applies a fake Gemini write inside the allowed fixture", async () => {
		const result = await runWorker("google-generative-ai", index => {
			const body =
				index === 0
					? geminiSse([{ functionCall: { name: "write", args: { path: "src/a.py", content: "changed\n" } } }])
					: geminiSse([{ text: "done" }]);
			return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
		});
		expect(result.exitCode, `${result.stderr}\n${result.stdout}`).toBe(0);
		expect(result.fileText).toBe("changed\n");
		expect(result.requests).toHaveLength(2);
		expect(result.stdout).toContain('"result":"completed"');
		expect(result.plantedEnv).toContain("planted-gemini");
		assertPinned(result.requests, "google-generative-ai", new URL(result.requests[0]!.url).origin);
	}, 180_000);

	it("applies a fake Ollama write inside the allowed fixture", async () => {
		const result = await runWorker("ollama-chat", index => {
			const body =
				index === 0
					? ollamaLine(
							{
								role: "assistant",
								content: "",
								tool_calls: [
									{ function: { name: "write", arguments: { path: "src/a.py", content: "changed\n" } } },
								],
							},
							"tool_calls",
						)
					: ollamaLine({ role: "assistant", content: "done" }, "stop");
			return new Response(body, { status: 200, headers: { "content-type": "application/x-ndjson" } });
		});
		expect(result.exitCode, `${result.stderr}\n${result.stdout}`).toBe(0);
		expect(result.fileText).toBe("changed\n");
		expect(result.requests).toHaveLength(2);
		expect(result.stdout).toContain('"result":"completed"');
		expect(result.plantedEnv).toContain("planted-ollama");
		assertPinned(result.requests, "ollama-chat", new URL(result.requests[0]!.url).origin);
	}, 180_000);

	it("exits nonzero when the model writes outside allowed_paths", async () => {
		const result = await runWorker("google-generative-ai", () => {
			const body = geminiSse([{ functionCall: { name: "write", args: { path: "notes.txt", content: "nope\n" } } }]);
			return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
		});
		expect(result.exitCode, `${result.stderr}\n${result.stdout}`).not.toBe(0);
		expect(result.fileText).toBe("old\n");
		expect(result.forbiddenExists).toBe(false);
		expect(result.requests).toHaveLength(1);
		expect(result.stdout).toContain('"result":"failed"');
		assertPinned(result.requests, "google-generative-ai", new URL(result.requests[0]!.url).origin);
	}, 180_000);

	it("exits nonzero on a proxy refusal and does not retry", async () => {
		const result = await runWorker("ollama-chat", () => {
			return new Response('{"error":"refused"}', { status: 500, headers: { "content-type": "application/json" } });
		});
		expect(result.exitCode, `${result.stderr}\n${result.stdout}`).not.toBe(0);
		expect(result.fileText).toBe("old\n");
		expect(result.requests).toHaveLength(1);
		expect(result.stderr).toContain("orchestrator-worker:");
		assertPinned(result.requests, "ollama-chat", new URL(result.requests[0]!.url).origin);
	}, 180_000);
});
