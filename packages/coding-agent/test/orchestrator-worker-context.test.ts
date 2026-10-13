import { describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import { pivotScript, populateHostSystemLayout, resolveHostSystemLayout } from "./helpers/orchestrator-worker-jail";

// Manifest-to-prompt context transport for the headless `orchestrator-worker`.
// A local proxy stands in for the provider and captures the request body, so a
// supplied manifest `context` is observable in the systemInstruction and an
// absent one contributes no context text. An empty or non-string context is
// rejected by manifest validation, before any provider request is opened.

const repoRoot = path.resolve(import.meta.dir, "../../..");
const MODEL_TOKEN = "model-token-real";
const CANDIDATE_ID = "cand-1";
const MODEL_ID = "worker-granted";
const MAX_OUTPUT_TOKENS = 64;
const APPROACH = "touch only the allowed file";
const CONTEXT_SENTINEL = "manifest-transport-context-9f3a";

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
}

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

/** The concatenated text of every `systemInstruction.parts[]` entry. */
function systemInstructionText(body: string): string {
	const json = JSON.parse(body) as { systemInstruction?: { parts?: Array<{ text?: unknown }> } };
	const parts = json.systemInstruction?.parts;
	if (!Array.isArray(parts)) return "";
	return parts.map(part => (typeof part?.text === "string" ? part.text : "")).join("\n");
}

async function runWorker(manifest: Record<string, unknown>): Promise<WorkerRun> {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "omp-worker-context-"));
	const layout = resolveHostSystemLayout();
	const requests: CapturedRequest[] = [];
	const server = Bun.serve({
		hostname: "127.0.0.1",
		port: 0,
		async fetch(request) {
			requests.push({ url: request.url, headers: request.headers, body: await request.text() });
			return new Response(geminiSse([{ text: "done" }]), {
				status: 200,
				headers: { "content-type": "text/event-stream" },
			});
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
		fs.writeFileSync(path.join(work, "src/a.py"), "old\n");
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
					api: "google-generative-ai",
					id: MODEL_ID,
					limits: { max_output_tokens: MAX_OUTPUT_TOKENS, context_window: 8192 },
					proxy: { baseUrl: proxyUrl },
				},
				...manifest,
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
		return { exitCode, stdout, stderr, requests };
	} finally {
		server.stop(true);
		fs.rmSync(root, { recursive: true, force: true });
	}
}

describe.serial("orchestrator-worker context transport", () => {
	it("carries the manifest context into the provider system prompt", async () => {
		const result = await runWorker({ context: CONTEXT_SENTINEL });
		expect(result.exitCode, `${result.stderr}\n${result.stdout}`).toBe(0);
		expect(result.requests).toHaveLength(1);
		const system = systemInstructionText(result.requests[0]!.body);
		expect(system).toContain(CONTEXT_SENTINEL);
		expect(system).toContain("\nContext:\n");
	}, 180_000);

	it("renders no context text when the manifest omits context", async () => {
		const result = await runWorker({});
		expect(result.exitCode, `${result.stderr}\n${result.stdout}`).toBe(0);
		expect(result.requests).toHaveLength(1);
		const system = systemInstructionText(result.requests[0]!.body);
		expect(system).not.toContain("Context:");
		expect(system).not.toContain(CONTEXT_SENTINEL);
	}, 180_000);

	it("rejects an empty or non-string context before any provider request", async () => {
		for (const context of ["", 7]) {
			const result = await runWorker({ context });
			expect(result.exitCode, `context=${JSON.stringify(context)} ${result.stderr}\n${result.stdout}`).not.toBe(0);
			expect(result.requests).toHaveLength(0);
			expect(result.stderr).toContain("orchestrator-worker:");
		}
	}, 180_000);
});
