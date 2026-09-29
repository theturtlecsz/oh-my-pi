import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, describe, expect, test, vi } from "bun:test";
import {
	SessionManager,
	type ExtensionAPI,
	type ExtensionCommandContext,
	type ExtensionContext,
} from "@oh-my-pi/pi-coding-agent";
import {
	RpcInputDispatcher,
	type PendingExtensionRequest,
	type RpcInputFrameDeps,
} from "@oh-my-pi/pi-coding-agent/modes/rpc/rpc-mode";
import type { RpcCommand, RpcResponse } from "@oh-my-pi/pi-coding-agent/modes/rpc/rpc-types";
import type { WorkClient } from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import {
	cleanupExecutionWorkspace,
	defaultExecutionWorkspaceManager,
	ensureExecutionWorkspace,
	type ExecutionWorkspace,
	type ExecutionWorkspaceManager,
} from "../extensions/workflow/git";
import * as gitModule from "../extensions/workflow/git";
import { createWorkflowHost } from "../extensions/workflow/host";

interface BeginExecutionInput {
	grantId: string;
	remoteRef: string;
	mode: "single" | "queue";
	items: unknown[];
	expectedFocusVersion: number;
	judgeSha256: string;
	judgeManifest: unknown;
	provenance: unknown;
}

interface SetExecutionStateInput {
	grantId: string;
	expectedGrantVersion: number;
	targetState: string;
	reason?: string | null;
	judgeSha256: string;
}

describe("execute RPC dispatcher liveness during stalled install (OMP-472)", () => {
	const tempDirs: string[] = [];
	const provisionedWorkspaces: ExecutionWorkspace[] = [];
	const childPids: number[] = [];

	afterEach(async () => {
		vi.restoreAllMocks();
		for (const pid of childPids.splice(0)) {
			try {
				process.kill(pid, "SIGKILL");
			} catch {
				// Process already exited
			}
		}
		for (const ws of provisionedWorkspaces.splice(0)) {
			try {
				await cleanupExecutionWorkspace(ws);
			} catch {
				// Workspace might not have been registered if provisioning stopped early
			}
		}
		for (const dir of tempDirs.splice(0)) {
			try {
				fs.rmSync(dir, { recursive: true, force: true });
			} catch {
				// Best-effort temp dir cleanup
			}
		}
	});

	function git(cwd: string, ...args: string[]): string {
		const res = Bun.spawnSync(["git", ...args], { cwd });
		if (res.exitCode !== 0) {
			throw new Error(`git ${args.join(" ")} failed in ${cwd}: ${res.stderr.toString()}`);
		}
		return res.stdout.toString().trim();
	}

	function temporaryCacheFile(cacheDir: string): string {
		return path.relative(path.join(os.homedir(), ".omp", "agent"), path.join(cacheDir, "cache.json"));
	}

	let repoSeq = 0;
	function setupFixture(initialBranch = "main") {
		const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), `omp-exec-live-${repoSeq++}-`));
		tempDirs.push(tempRoot);

		const repoDir = path.join(tempRoot, "repo");
		const cacheDir = path.join(tempRoot, "cache");
		const worktreesRoot = path.join(tempRoot, "worktrees");
		const startedMarkerPath = path.join(tempRoot, "install.started");

		fs.mkdirSync(repoDir, { recursive: true });
		fs.mkdirSync(cacheDir, { recursive: true });
		fs.mkdirSync(worktreesRoot, { recursive: true });

		git(repoDir, "init", `--initial-branch=${initialBranch}`, "-q");
		git(repoDir, "config", "user.email", "test@example.com");
		git(repoDir, "config", "user.name", "Test");

		// Provide auditor agent definition so computeAuditTcb succeeds under empty HOME in CI
		const agentsDir = path.join(repoDir, ".omp", "agents");
		fs.mkdirSync(agentsDir, { recursive: true });
		const repoAuditor = path.resolve("session-system/agents/auditor.md");
		if (fs.existsSync(repoAuditor)) {
			fs.copyFileSync(repoAuditor, path.join(agentsDir, "auditor.md"));
		} else {
			fs.writeFileSync(
				path.join(agentsDir, "auditor.md"),
				["---", "name: auditor", "description: Test fixture auditor", "---", ""].join("\n"),
			);
		}

		fs.writeFileSync(path.join(repoDir, "seed.txt"), "seed content\n");
		fs.writeFileSync(path.join(repoDir, "package.json"), JSON.stringify({ name: "test-fixture" }));
		git(repoDir, "add", ".");
		git(repoDir, "commit", "-q", "-m", "initial commit");

		vi.spyOn(gitModule, "ensureUpToDateWithDefault").mockReturnValue({ ok: true, detail: "up to date" });
		vi.spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({
			ok: true,
			count: 12,
			detail: "12 required status check context(s) on main",
		});

		const installScript = [
			`require("node:fs").writeFileSync(${JSON.stringify(startedMarkerPath)}, String(process.pid));`,
			"await Bun.sleep(30000);",
		].join(" ");

		let lastProvisionedWorkspace: ExecutionWorkspace | undefined;
		const executionWorkspaceManager: ExecutionWorkspaceManager = {
			primaryRoot: defaultExecutionWorkspaceManager.primaryRoot,
			cleanup: defaultExecutionWorkspaceManager.cleanup,
			ensure: async (cwd, key, grantId, baseline, options) => {
				const ws = await ensureExecutionWorkspace(cwd, key, grantId, baseline, options, worktreesRoot, {
					command: [process.execPath, "-e", installScript],
					timeoutMs: 3000,
				});
				lastProvisionedWorkspace = ws;
				provisionedWorkspaces.push(ws);
				return ws;
			},
		};

		let capturedInput: BeginExecutionInput | undefined;
		let setExecutionStateCalledWith: SetExecutionStateInput | undefined;

		const mockBackend: WorkflowBackend = {
			name: "work-now",
			cacheFile: temporaryCacheFile(cacheDir),
			markerFile: ".work-project",
			evidenceKinds: ["verification", "closeout"],
			workspaceId: "ws-test",
			pendingDeliveries: async () => [],
			findIssue: async (_keyOrId: string) => ({
				id: "uuid-237",
				key: "OMP-237",
				title: "Test 237",
				project: "The Bookends",
			}),
			issueDetail: async () => ({ key: "OMP-237", attemptSnapshot: undefined }),
			workflowState: async () => ({ open_blockers: [] }),
			getFocusVersion: async () => 1,
			executionChildren: async () => ({ umbrella: false, children: [] }),
			workClient: {
				healthReady: async () => ({
					ready: true,
					contract_sha256: "contract-sha",
					service_fingerprint: "fp",
					judge_manifest: { judge_sha256: "judge-sha" },
				}),
				workItem: async () => ({
					work_id: "uuid-237",
					project_id: "proj-1",
					revision: {
						revision_id: "rev-237",
						description: "Test 237 description",
					},
				}),
				workflow: async () => ({ relations: [] }),
			} as unknown as WorkClient,
			beginExecution: async (input: BeginExecutionInput) => {
				capturedInput = input;
				return {
					grant: {
						grant_id: input.grantId,
						grant_version: 1,
						remote_ref: input.remoteRef,
						state: "active",
					},
					items: input.items,
					activeItem: null,
				};
			},
			setExecutionState: async (input: SetExecutionStateInput) => {
				setExecutionStateCalledWith = input;
				return {
					grant: {
						grant_id: input.grantId,
						grant_version: input.expectedGrantVersion + 1,
						remote_ref: capturedInput?.remoteRef ?? "refs/heads/execution/omp-237",
						state: input.targetState,
						terminal_reason: input.reason ?? null,
					},
					items: [],
					activeItem: null,
				};
			},
		} as unknown as WorkflowBackend;

		const registeredCommands = new Map<string, (args: string, ctx: ExtensionContext) => Promise<void>>();
		const notifications: Array<{ text: string; level?: string }> = [];
		const appendedEntries: Array<{ customType: string; data?: unknown }> = [];
		const sentMessages: Array<{ customType?: string; content?: unknown; options?: unknown }> = [];

		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: () => {},
			registerMessageRenderer: () => {},
			registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionContext) => Promise<void> }) => {
				registeredCommands.set(name, def.handler);
			},
			registerFlag: () => {},
			on: () => {},
			appendEntry: (customType: string, data?: unknown) => {
				appendedEntries.push({ customType, data });
			},
			sendMessage: (message: { customType?: string; content?: unknown }, options?: unknown) => {
				sentMessages.push({ ...message, options });
			},
			getSessionId: () => "sess-1",
		} as unknown as ExtensionAPI;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
			executionWorkspaceManager,
		})(fakePi);

		const sessionManager = SessionManager.inMemory(repoDir);
		const fakeCtx = {
			cwd: repoDir,
			taskDepth: 0,
			sessionManager,
			newSession: async (options: Parameters<ExtensionCommandContext["newSession"]>[0]) => {
				await options?.setup?.(sessionManager);
				fakeCtx.cwd = sessionManager.getCwd();
				return { cancelled: false };
			},
			ui: {
				notify: (text: string, level?: string) => notifications.push({ text, level }),
				theme: { fg: (_c: string, t: string) => t },
				setStatus: () => {},
			},
		} as unknown as ExtensionContext;

		return {
			repoDir,
			tempRoot,
			startedMarkerPath,
			git,
			fakeCtx,
			registeredCommands,
			notifications,
			appendedEntries,
			sentMessages,
			get capturedInput() {
				return capturedInput;
			},
			get setExecutionStateCalledWith() {
				return setExecutionStateCalledWith;
			},
			get lastProvisionedWorkspace() {
				return lastProvisionedWorkspace;
			},
		};
	}

	test("/execute with a stalled install answers get_state promptly and ends in provision timeout error", async () => {
		const fixture = setupFixture();

		let executeSettled = false;
		let executePromise: Promise<void> | undefined;

		const outputs: Array<RpcResponse | object> = [];
		const handleCommand = async (command: RpcCommand): Promise<RpcResponse> => {
			if (command.type === "prompt") {
				if (command.message.startsWith("/execute")) {
					const executeHandler = fixture.registeredCommands.get("execute");
					if (!executeHandler) throw new Error("execute command was not registered");
					const args = command.message.slice("/execute".length).trim();
					executePromise = executeHandler(args, fixture.fakeCtx).finally(() => {
						executeSettled = true;
					});
					return {
						id: command.id,
						type: "response",
						command: "prompt",
						success: true,
					} as RpcResponse;
				}
				throw new Error(`Unhandled prompt message: ${command.message}`);
			}
			if (command.type === "get_state") {
				return {
					id: command.id,
					type: "response",
					command: "get_state",
					success: true,
					data: { isStreaming: false },
				} as RpcResponse;
			}
			throw new Error(`Unhandled command type: ${command.type}`);
		};

		const deps: RpcInputFrameDeps = {
			handleCommand,
			output: obj => {
				outputs.push(obj);
			},
			errorResponse: (id, command, message) => ({
				id,
				type: "response",
				command,
				success: false,
				error: message,
			}),
			pendingExtensionRequests: new Map<string, PendingExtensionRequest>(),
			onHostToolResult: () => {},
			onHostToolUpdate: () => {},
			onHostUriResult: () => {},
		};

		const dispatcher = new RpcInputDispatcher({ deps });

		// 1. Dispatch the prompt; poll (Bun.sleep(20)) until the started marker exists.
		dispatcher.dispatch({
			id: "prompt-1",
			type: "prompt",
			message: "/execute OMP-237",
		});

		const pollStart = performance.now();
		while (!fs.existsSync(fixture.startedMarkerPath)) {
			if (performance.now() - pollStart > 5000) {
				throw new Error("Timed out waiting for install started marker");
			}
			await Bun.sleep(20);
		}

		const pidText = fs.readFileSync(fixture.startedMarkerPath, "utf8").trim();
		const pid = Number(pidText);
		if (Number.isInteger(pid) && pid > 0) {
			childPids.push(pid);
		}

		// 2. Dispatch get_state three times 100 ms apart; each response is output within 500 ms and before the execute handler settles.
		for (let i = 1; i <= 3; i++) {
			const id = `get-state-${i}`;
			const sendTime = performance.now();
			dispatcher.dispatch({
				id,
				type: "get_state",
			});

			while (!outputs.some(o => (o as RpcResponse).id === id)) {
				if (performance.now() - sendTime > 500) {
					throw new Error(`Response for ${id} not output within 500ms`);
				}
				await Bun.sleep(10);
			}

			const duration = performance.now() - sendTime;
			expect(duration).toBeLessThan(500);
			expect(executeSettled).toBe(false);

			if (i < 3) {
				await Bun.sleep(100);
			}
		}

		// 3. Await the handler: setExecutionState got targetState `stopped` with reason starting `execution_workspace_provision_failed:` and containing `timed out`; an error-level notice starts `Execution grant stopped:`; sendMessage got customType `work-execution-status`; no appendEntry with customType `work-now-execute-outbox`.
		expect(executePromise).toBeDefined();
		await executePromise;
		expect(executeSettled).toBe(true);

		expect(fixture.setExecutionStateCalledWith).toBeDefined();
		expect(fixture.setExecutionStateCalledWith?.targetState).toBe("stopped");
		expect(fixture.setExecutionStateCalledWith?.reason?.startsWith("execution_workspace_provision_failed:")).toBe(true);
		expect(fixture.setExecutionStateCalledWith?.reason).toContain("timed out");

		const errorNotice = fixture.notifications.find(n => n.level === "error");
		expect(errorNotice).toBeDefined();
		expect(errorNotice!.text.startsWith("Execution grant stopped:")).toBe(true);

		const statusMessage = fixture.sentMessages.find(m => m.customType === "work-execution-status");
		expect(statusMessage).toBeDefined();

		const outboxEntry = fixture.appendedEntries.find(e => e.customType === "work-now-execute-outbox");
		expect(outboxEntry).toBeUndefined();

		await dispatcher.drain();
	});
});
