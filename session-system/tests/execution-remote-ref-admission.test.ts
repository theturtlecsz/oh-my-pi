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
import type { WorkClient } from "@oh-my-pi/pi-work-client";
import { z } from "zod";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import {
	cleanupExecutionWorkspace,
	defaultExecutionWorkspaceManager,
	ensureExecutionWorkspace,
	executionRemoteRef,
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

describe("execution remote ref admission end-to-end (OMP-233 AC6)", () => {
	const tempDirs: string[] = [];
	const provisionedWorkspaces: ExecutionWorkspace[] = [];

	afterEach(async () => {
		vi.restoreAllMocks();
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
		const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), `omp-exec-adm-${repoSeq++}-`));
		tempDirs.push(tempRoot);

		const repoDir = path.join(tempRoot, "repo");
		const cacheDir = path.join(tempRoot, "cache");
		const worktreesRoot = path.join(tempRoot, "worktrees");

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
		git(repoDir, "add", ".");
		git(repoDir, "commit", "-q", "-m", "initial commit");

		// vi.spyOn on the git module for preflight only (restore in afterEach)
		vi.spyOn(gitModule, "ensureUpToDateWithDefault").mockReturnValue({ ok: true, detail: "up to date" });
		vi.spyOn(gitModule, "requiredStatusCheckCount").mockReturnValue({
			ok: true,
			count: 12,
			detail: "12 required status check context(s) on main",
		});

		let lastProvisionedWorkspace: ExecutionWorkspace | undefined;
		const executionWorkspaceManager: ExecutionWorkspaceManager = {
			primaryRoot: defaultExecutionWorkspaceManager.primaryRoot,
			cleanup: defaultExecutionWorkspaceManager.cleanup,
			ensure: async (cwd, key, grantId, baseline, options) => {
				const ws = await ensureExecutionWorkspace(cwd, key, grantId, baseline, options, worktreesRoot);
				lastProvisionedWorkspace = ws;
				provisionedWorkspaces.push(ws);
				return ws;
			},
		};

		let capturedInput: BeginExecutionInput | undefined;
		let beginExecutionOverride: ((input: BeginExecutionInput) => Promise<unknown>) | undefined;
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
				if (beginExecutionOverride) return beginExecutionOverride(input);
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
			appendEntry: () => {},
			sendMessage: () => {},
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
			git,
			fakeCtx,
			registeredCommands,
			notifications,
			get capturedInput() {
				return capturedInput;
			},
			get setExecutionStateCalledWith() {
				return setExecutionStateCalledWith;
			},
			get lastProvisionedWorkspace() {
				return lastProvisionedWorkspace;
			},
			setBeginExecutionOverride: (fn: (input: BeginExecutionInput) => Promise<unknown>) => {
				beginExecutionOverride = fn;
			},
		};
	}

	test("case 1: start on default branch: recorded remote_ref == worktree git symbolic-ref HEAD == executionRemoteRef('OMP-237', capturedGrantId)", async () => {
		const fixture = setupFixture("main");
		const handler = fixture.registeredCommands.get("execute");
		expect(handler).toBeDefined();

		await handler!("OMP-237", fixture.fakeCtx);

		expect(fixture.capturedInput).toBeDefined();
		const capturedGrantId = fixture.capturedInput!.grantId;
		const capturedRemoteRef = fixture.capturedInput!.remoteRef;
		const expectedRef = executionRemoteRef("OMP-237", capturedGrantId);

		expect(capturedRemoteRef).toBe(expectedRef);
		expect(fixture.lastProvisionedWorkspace).toBeDefined();
		const worktreeHeadRef = fixture.git(fixture.lastProvisionedWorkspace!.path, "symbolic-ref", "HEAD");
		expect(worktreeHeadRef).toBe(expectedRef);
		expect(capturedRemoteRef).toBe(worktreeHeadRef);
	});

	test("case 2: start on non-default branch: recorded remote_ref == worktree git symbolic-ref HEAD == executionRemoteRef('OMP-237', capturedGrantId), ref is not non-default branch", async () => {
		const fixture = setupFixture("main");
		fixture.git(fixture.repoDir, "checkout", "-b", "release/x");
		expect(fixture.git(fixture.repoDir, "symbolic-ref", "HEAD")).toBe("refs/heads/release/x");

		const handler = fixture.registeredCommands.get("execute");
		expect(handler).toBeDefined();

		await handler!("OMP-237", fixture.fakeCtx);

		expect(fixture.capturedInput).toBeDefined();
		const capturedGrantId = fixture.capturedInput!.grantId;
		const capturedRemoteRef = fixture.capturedInput!.remoteRef;
		const expectedRef = executionRemoteRef("OMP-237", capturedGrantId);

		expect(capturedRemoteRef).toBe(expectedRef);
		expect(fixture.lastProvisionedWorkspace).toBeDefined();
		const worktreeHeadRef = fixture.git(fixture.lastProvisionedWorkspace!.path, "symbolic-ref", "HEAD");
		expect(worktreeHeadRef).toBe(expectedRef);
		expect(capturedRemoteRef).toBe(worktreeHeadRef);
		expect(capturedRemoteRef).not.toBe("refs/heads/release/x");
		expect(worktreeHeadRef).not.toBe("refs/heads/release/x");
	});

	test("case 3: backend returns remote_ref refs/heads/execution/omp-238 -> grant stopped with reason starting execution_workspace_provision_failed", async () => {
		const fixture = setupFixture("main");
		fixture.setBeginExecutionOverride(async input => ({
			grant: {
				grant_id: input.grantId,
				grant_version: 1,
				remote_ref: "refs/heads/execution/omp-238",
				state: "active",
			},
			items: [],
			activeItem: null,
		}));

		const handler = fixture.registeredCommands.get("execute");
		expect(handler).toBeDefined();

		await handler!("OMP-237", fixture.fakeCtx);

		expect(fixture.setExecutionStateCalledWith).toBeDefined();
		expect(fixture.setExecutionStateCalledWith?.targetState).toBe("stopped");
		expect(fixture.setExecutionStateCalledWith?.reason?.startsWith("execution_workspace_provision_failed:")).toBe(true);
		expect(fixture.setExecutionStateCalledWith?.reason).toContain("refs/heads/execution/omp-238");
		expect(fixture.lastProvisionedWorkspace).toBeUndefined();
	});

	test("case 4: backend returns executionRemoteRef('OMP-237', otherUuid) -> grant stopped with reason starting execution_workspace_provision_failed", async () => {
		const fixture = setupFixture("main");
		const otherUuid = "00000000-0000-7000-8000-000000000999";
		fixture.setBeginExecutionOverride(async input => ({
			grant: {
				grant_id: input.grantId,
				grant_version: 1,
				remote_ref: executionRemoteRef("OMP-237", otherUuid),
				state: "active",
			},
			items: [],
			activeItem: null,
		}));

		const handler = fixture.registeredCommands.get("execute");
		expect(handler).toBeDefined();

		await handler!("OMP-237", fixture.fakeCtx);

		expect(fixture.setExecutionStateCalledWith).toBeDefined();
		expect(fixture.setExecutionStateCalledWith?.targetState).toBe("stopped");
		expect(fixture.setExecutionStateCalledWith?.reason?.startsWith("execution_workspace_provision_failed:")).toBe(true);
		expect(fixture.setExecutionStateCalledWith?.reason).toContain(executionRemoteRef("OMP-237", otherUuid));
		expect(fixture.lastProvisionedWorkspace).toBeUndefined();
	});
});
