import type { CloseAttempt, WorkflowView } from "@oh-my-pi/pi-work-client";

export interface StageContextInput {
	key: string;
	cwd: string;
}

export type StageContextRunner = (
	argv: string[],
	stdin: string,
	timeoutMs: number,
) => Promise<{ exitCode: number; stdout: string; stderr: string }>;

export interface StageContextDeps {
	workflow: (key: string) => Promise<WorkflowView>;
	liveAttempt: (view: WorkflowView) => CloseAttempt | undefined;
	env: Record<string, string | undefined>;
	run?: StageContextRunner | ((...args: never[]) => unknown);
}

const DEFAULT_TIMEOUT_MS = 60_000;
const ENV_VAR_NAME = "OMP_KNOWLEDGE_CONTEXT_CMD";

export async function defaultRun(
	argv: string[],
	stdin: string,
	timeoutMs: number,
): Promise<{ exitCode: number; stdout: string; stderr: string }> {
	const proc = Bun.spawn(argv, {
		stdin: "pipe",
		stdout: "pipe",
		stderr: "pipe",
	});

	if (proc.stdin) {
		try {
			proc.stdin.write(stdin);
			await proc.stdin.end();
		} catch {
			// Subprocess may exit before stdin can be fully written
		}
	}

	let timer: Timer | undefined;
	const { promise: timeoutPromise, reject } = Promise.withResolvers<never>();
	timer = setTimeout(() => {
		try {
			proc.kill(9);
		} catch {
			// Subprocess might already be dead
		}
		reject(new Error("timeout"));
	}, timeoutMs);

	try {
		const [stdoutText, stderrText, exitCode] = await Promise.race([
			Promise.all([
				new Response(proc.stdout).text(),
				new Response(proc.stderr).text(),
				proc.exited,
			]),
			timeoutPromise,
		]);
		return { exitCode, stdout: stdoutText, stderr: stderrText };
	} finally {
		clearTimeout(timer);
	}
}

export async function stageContextLines(
	input: StageContextInput,
	deps: StageContextDeps,
): Promise<string[]> {
	try {
		const envVar = deps.env?.[ENV_VAR_NAME];
		if (envVar === undefined) {
			return [];
		}

		let cmd: unknown;
		try {
			cmd = JSON.parse(envVar);
		} catch {
			return ["STAGE CONTEXT: unavailable (bad JSON)"];
		}

		if (!Array.isArray(cmd) || cmd.length === 0 || !cmd.every(item => typeof item === "string")) {
			return ["STAGE CONTEXT: unavailable (bad JSON)"];
		}

		const argv = [...cmd, "compile", "--json"];

		const view = await deps.workflow(input.key);
		const attempt = deps.liveAttempt(view);
		let stage: "plan" | "implement" | "audit";
		let attemptId: string;

		if (attempt) {
			stage = "audit";
			attemptId = attempt.attempt_id;
		} else {
			const closeAttemptsCount = Array.isArray(view.close_attempts) ? view.close_attempts.length : 0;
			attemptId = `pre-close-${closeAttemptsCount}`;
			const currentRevId = view.item?.revision?.revision_id;
			const currentCandId = view.item?.candidate?.candidate_id;
			const hasPlan = Boolean(
				currentRevId &&
				currentCandId &&
				Array.isArray(view.receipts) &&
				view.receipts.some(
					r => r.kind === "plan" && r.revision_id === currentRevId && r.candidate_id === currentCandId,
				),
			);
			stage = hasPlan ? "implement" : "plan";
		}

		const stdin = JSON.stringify({
			stage,
			attempt_id: attemptId,
			cwd: input.cwd,
			workflow: view,
		});

		const isWorkBackendRun =
			typeof deps.run === "function" &&
			deps.run.name === "run" &&
			deps.run.toString().includes("intentFingerprint");
		const runner: StageContextRunner =
			deps.run && !isWorkBackendRun ? (deps.run as StageContextRunner) : defaultRun;

		let result: { exitCode: number; stdout: string; stderr: string };
		try {
			result = await runner(argv, stdin, DEFAULT_TIMEOUT_MS);
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			const isTimeout = msg.toLowerCase().includes("timeout");
			return [`STAGE CONTEXT: unavailable (${isTimeout ? "timeout" : msg})`];
		}

		if (result.exitCode !== 0) {
			return [`STAGE CONTEXT: unavailable (exit ${result.exitCode})`];
		}

		let payload: unknown;
		try {
			payload = JSON.parse(result.stdout);
		} catch {
			return ["STAGE CONTEXT: unavailable (bad JSON)"];
		}

		if (
			!payload ||
			typeof payload !== "object" ||
			typeof (payload as { text?: unknown }).text !== "string" ||
			typeof (payload as { bundle_sha256?: unknown }).bundle_sha256 !== "string" ||
			typeof (payload as { bundle_id?: unknown }).bundle_id !== "string" ||
			typeof (payload as { stage?: unknown }).stage !== "string" ||
			typeof (payload as { tokens?: unknown }).tokens !== "number" ||
			typeof (payload as { token_budget?: unknown }).token_budget !== "number"
		) {
			return ["STAGE CONTEXT: unavailable (bad JSON)"];
		}

		const p = payload as {
			bundle_id: string;
			bundle_sha256: string;
			stage: string;
			tokens: number;
			token_budget: number;
			exclusions?: unknown[];
			text: string;
		};

		const actualSha = new Bun.CryptoHasher("sha256").update(p.text, "utf8").digest("hex");
		if (actualSha !== p.bundle_sha256) {
			return ["STAGE CONTEXT: unavailable (sha mismatch)"];
		}

		const excludedCount = Array.isArray(p.exclusions) ? p.exclusions.length : 0;
		const header = `STAGE CONTEXT ${p.stage} bundle=${p.bundle_id} sha256=${p.bundle_sha256} tokens=${p.tokens}/${p.token_budget} excluded=${excludedCount}`;
		return [header, ...p.text.split("\n")];
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		const isTimeout = msg.toLowerCase().includes("timeout");
		return [`STAGE CONTEXT: unavailable (${isTimeout ? "timeout" : msg})`];
	}
}
