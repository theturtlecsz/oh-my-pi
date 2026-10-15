import { afterEach, describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { unregisterCustomApis } from "@oh-my-pi/pi-ai/api-registry";
import { AuthStorage, SqliteAuthCredentialStore } from "@oh-my-pi/pi-ai/auth-storage";
import { createMockModel, registerMockApi } from "@oh-my-pi/pi-ai/providers/mock";
import { $ } from "bun";
import { ModelRegistry } from "../../src/config/model-registry";
import { Settings } from "../../src/config/settings";
import * as skillDescriptions from "../../src/extensibility/skill-descriptions";
import type { Skill } from "../../src/extensibility/skills";
import { AgentRegistry } from "../../src/registry/agent-registry";
import { createAgentSession } from "../../src/sdk";
import { selectSecurityOAuthAccount } from "../../src/security/auth";
import { SecurityCoordinator } from "../../src/security/coordinator";
import type { SecurityGitAdapter } from "../../src/security/preflight";
import * as securityPublication from "../../src/security/publication";
import { SecurityStore } from "../../src/security/store";
import type { AgentSession } from "../../src/session/agent-session";
import { SessionManager } from "../../src/session/session-manager";
import { isolateHost } from "../helpers/host-isolation";

const provider = "openai-codex";
const sourceId = "coordinator-auth-isolation";

// Each private root owns agent-dir caches and SQLite rows. No case inherits
// another case's disabled credential or the host's compression cache.
async function authFixture(expires = Date.now() + 60 * 60_000) {
	const root = await fs.mkdtemp(path.join(import.meta.dir, ".auth-isolation-"));
	const host = isolateHost({ home: root });
	const store = await SqliteAuthCredentialStore.open(path.join(host.agentDir, "agent.db"));
	const entered = Promise.withResolvers<void>();
	const release = Promise.withResolvers<void>();
	const refresh = vi.fn(async () => {
		entered.resolve();
		await release.promise;
		throw new Error("OAuth refresh failed: 400 invalid_grant: refresh token revoked");
	});
	const usageFetch: typeof fetch = Object.assign(
		vi.fn(async () => Response.json({ plan_type: "pro", rate_limit: { allowed: true, limit_reached: false } })),
		{ preconnect: () => undefined },
	);
	const storage = new AuthStorage(store, { refreshOAuthCredential: refresh, usageFetch });
	await storage.credentials.set(provider, {
		type: "oauth",
		access: "fixture-access-token",
		refresh: "fixture-refresh-token",
		expires,
		accountId: "workspace-fixture",
		email: "security@example.invalid",
		orgId: "workspace-fixture",
		orgName: "pro",
	});
	const account = selectSecurityOAuthAccount(storage, provider);
	const registry = new ModelRegistry(storage, path.join(host.agentDir, "models.yml"));
	const settings = Settings.isolated({
		"security.enabled": true,
		"compaction.enabled": false,
		modelRoles: { smol: `${provider}/auth-isolation-smol` },
	});
	return {
		root,
		host,
		store,
		storage,
		registry,
		settings,
		account,
		entered,
		release,
		refresh,
		usageFetch,
		async close() {
			release.resolve();
			unregisterCustomApis(sourceId);
			settings.cancelPendingSaves();
			storage.close();
			host.restore();
			await fs.rm(root, { recursive: true, force: true });
		},
	};
}

afterEach(() => vi.restoreAllMocks());

describe("coordinator shared-auth isolation controls", () => {
	test("definitive refresh rejection soft-disables the pinned durable row", async () => {
		const f = await authFixture(Date.now() - 1000);
		const pending = f.storage.oauth.accessById(provider, f.account.credentialId, { forceRefresh: true });
		try {
			await Promise.race([
				f.entered.promise,
				pending.then(() => {
					throw new Error("Pinned access settled before entering refresh");
				}),
			]);
			expect(selectSecurityOAuthAccount(f.storage, provider, f.account.credentialId)).toEqual(f.account);
			expect(f.store.listAuthCredentials(provider)).toHaveLength(1);
			expect(await f.store.listDisabledCredentials(provider)).toEqual([]);
			f.release.resolve();
			expect(await pending).toMatchObject({ ok: false, credentialId: f.account.credentialId });
			expect(f.store.listAuthCredentials(provider)).toEqual([]);
			expect(await f.store.listDisabledCredentials(provider)).toMatchObject([
				{ id: f.account.credentialId, cause: expect.stringContaining("invalid_grant") },
			]);
			await f.storage.credentials.reload();
			expect(() => selectSecurityOAuthAccount(f.storage, provider, f.account.credentialId)).toThrow("not available");
		} finally {
			f.release.resolve();
			await pending;
			vi.restoreAllMocks();
			await f.close();
		}
	});

	for (const kind of ["runtime", "config"] as const) {
		test(`${kind} override hides OAuth without disabling or deleting its durable row`, async () => {
			const f = await authFixture();
			try {
				if (kind === "runtime") f.storage.keys.setRuntime(provider, "fixture-override");
				else f.storage.keys.setConfig(provider, "fixture-override");
				expect(f.storage.oauth.accounts(provider)).toEqual([]);
				expect(await f.storage.oauth.accessById(provider, f.account.credentialId)).toBeUndefined();
				expect(() => selectSecurityOAuthAccount(f.storage, provider, f.account.credentialId)).toThrow(
					"not available",
				);
				expect(f.store.listAuthCredentials(provider).map(row => row.id)).toEqual([f.account.credentialId]);
				expect(await f.store.listDisabledCredentials(provider)).toEqual([]);
				if (kind === "runtime") f.storage.keys.removeRuntime(provider);
				else f.storage.keys.removeConfig(provider);
				await f.storage.credentials.reload();
				expect(selectSecurityOAuthAccount(f.storage, provider, f.account.credentialId)).toEqual(f.account);
				expect(await f.storage.oauth.accessById(provider, f.account.credentialId)).toMatchObject({
					ok: true,
					credentialId: f.account.credentialId,
					accessToken: "fixture-access-token",
				});
				expect(f.refresh).not.toHaveBeenCalled();
			} finally {
				vi.restoreAllMocks();
				await f.close();
			}
		});
	}

	test("SDK skill compression can disable reused auth after session disposal", async () => {
		const f = await authFixture();
		const pending: Promise<void>[] = [];
		let compressionError: unknown;
		let session: AgentSession | undefined;
		const agents = new AgentRegistry();
		const agentId = "AuthIsolation";
		// Observe completion only; retain production compression, registry, auth,
		// usage parsing, retry driver and SQLite CAS mutation.
		const createCompressor = skillDescriptions.createSkillDescriptionCompressor;
		vi.spyOn(skillDescriptions, "createSkillDescriptionCompressor").mockImplementation((...args) => {
			const compress = createCompressor(...args);
			return (...input) => {
				const work = compress(...input);
				pending.push(
					work.then(
						() => undefined,
						error => {
							compressionError = error;
						},
					),
				);
				return work;
			};
		});
		const rejected = createMockModel({
			provider,
			id: "auth-isolation-smol",
			baseUrl: "https://chatgpt.com/backend-api/codex",
			handler: () => ({ stopReason: "error", errorMessage: "401 Unauthorized", responseStatus: 401 }),
		});
		const foreground = createMockModel({ provider, id: "auth-isolation-foreground" });
		const skill: Skill = {
			name: "auth-isolation",
			description: "Use to investigate shared authentication state during background skill compression.",
			filePath: path.join(f.root, "SKILL.md"),
			baseDir: f.root,
			source: "test",
		};
		try {
			registerMockApi(sourceId);
			vi.spyOn(f.registry, "getAvailable").mockReturnValue([rejected]);
			await f.storage.usage.reports();
			({ session } = await createAgentSession({
				cwd: f.root,
				agentDir: f.host.agentDir,
				authStorage: f.storage,
				modelRegistry: f.registry,
				settings: f.settings,
				model: foreground,
				sessionManager: SessionManager.inMemory(f.root),
				agentRegistry: agents,
				agentId,
				spawns: "security-reviewer",
				bindProcessState: false,
				skills: [skill],
				contextFiles: [],
				toolNames: ["read"],
				restrictToolNames: true,
				disableExtensionDiscovery: true,
				enableMCP: false,
				enableIrc: false,
				enableLsp: false,
				skipPythonPreflight: true,
				hasUI: false,
			}));
			await Promise.race([
				f.entered.promise,
				Promise.all(pending).then(() => {
					throw new Error("Compression settled before entering refresh", { cause: compressionError });
				}),
			]);
			expect(rejected.calls).toHaveLength(1);
			expect(f.refresh).toHaveBeenCalledTimes(1);
			expect(f.usageFetch).toHaveBeenCalled();
			expect(selectSecurityOAuthAccount(f.storage, provider, f.account.credentialId)).toEqual(f.account);
			await session.dispose();
			session = undefined;
			expect(selectSecurityOAuthAccount(f.storage, provider, f.account.credentialId)).toEqual(f.account);
			f.release.resolve();
			await Promise.all(pending);
			expect(f.store.listAuthCredentials(provider)).toEqual([]);
			expect(await f.store.listDisabledCredentials(provider)).toMatchObject([
				{ id: f.account.credentialId, cause: expect.stringContaining("invalid_grant") },
			]);
			expect(() => selectSecurityOAuthAccount(f.storage, provider, f.account.credentialId)).toThrow("not available");
		} finally {
			f.release.resolve();
			await Promise.all(pending);
			await session?.dispose();
			agents.unregister(agentId);
			vi.restoreAllMocks();
			await f.close();
		}
	});

	test("the default security session keeps pinned auth out of background compression", async () => {
		const f = await authFixture();
		const repoRoot = path.join(f.root, "repo");
		const stateRoot = path.join(f.root, "state");
		await fs.mkdir(path.join(repoRoot, "src"), { recursive: true });
		// A real repository bounds skill discovery's repoRoot at the private root,
		// so the default session scans only this fixture's skills, not the host.
		await $`git init --initial-branch=main`.cwd(repoRoot).quiet();
		await $`git config user.name Fixture`.cwd(repoRoot).quiet();
		await $`git config user.email fixture@example.invalid`.cwd(repoRoot).quiet();
		await Bun.write(path.join(repoRoot, "src", "app.ts"), "export const app = true;\n");
		await $`git add src/app.ts`.cwd(repoRoot).quiet();
		await $`git commit -m base`.cwd(repoRoot).quiet();
		// Cold skill-description cache + one visible skill: the catalog would have to
		// start unpinned background model work on the real default session if the
		// caller-owned `getApiKey` did not suppress its compressor.
		const skillDir = path.join(f.host.agentDir, "skills", "auth-isolation-regression");
		await fs.mkdir(skillDir, { recursive: true });
		await Bun.write(
			path.join(skillDir, "SKILL.md"),
			[
				"---",
				"name: auth-isolation-regression",
				"description: Use when verifying that security sessions never run unpinned background credential refreshes.",
				"---",
				"",
				"Body.",
				"",
			].join("\n"),
		);
		const storeFactory = (): Promise<SecurityStore> => SecurityStore.open(repoRoot, { stateRoot });
		const gitAdapter: SecurityGitAdapter = {
			root: async () => repoRoot,
			headSha: async () => "a".repeat(40),
			resolveRef: async (_cwd, refName) => (refName === "base" ? "b".repeat(40) : "c".repeat(40)),
			diffTree: async () => "fixture-diff",
			status: async () => "",
			files: async () => ["src/app.ts"],
			untracked: async () => [],
		};
		const pending: Promise<void>[] = [];
		let compressorInvocations = 0;
		let unpinnedAuthRequests = 0;
		const publicationEntered = Promise.withResolvers<void>();
		const publicationRelease = Promise.withResolvers<void>();
		// Opened only by publication entry. A captured unpinned compression request
		// is held here BEFORE the underlying auth runs, so a regressed fix is
		// observed rather than silently interleaved.
		const authGate = Promise.withResolvers<void>();
		let publicationStarted = false;
		let publicationCalls = 0;
		let capturedSession: AgentSession | undefined;
		// Observe only; retain production compression, registry, auth, usage parsing,
		// retry driver and SQLite CAS mutation.
		const createCompressor = skillDescriptions.createSkillDescriptionCompressor;
		const compressorFactory = vi
			.spyOn(skillDescriptions, "createSkillDescriptionCompressor")
			.mockImplementation((...args) => {
				const compress = createCompressor(...args);
				return (...input) => {
					compressorInvocations++;
					const work = compress(...input);
					pending.push(
						work.then(
							() => undefined,
							() => undefined,
						),
					);
					return work;
				};
			});
		// Call through to the real publication tool so the canonical bundle, SARIF
		// export and store writes all run; the wrapper only owns the gates.
		const realCreatePublication = securityPublication.createSecurityPublicationTool;
		vi.spyOn(securityPublication, "createSecurityPublicationTool").mockImplementation(options => {
			const tool = realCreatePublication(options);
			return {
				...tool,
				async execute(toolCallId, params, signal, onUpdate, ctx) {
					publicationCalls++;
					publicationStarted = true;
					const live = AgentRegistry.global()
						.list()
						.find(ref => ref.id.startsWith("Security-") && ref.session !== null);
					capturedSession = live?.session ?? capturedSession;
					publicationEntered.resolve();
					authGate.resolve();
					// Hold the return until the test has released and joined captured
					// compression/refresh, so neither outlives the session.
					await publicationRelease.promise;
					const result = await tool.execute(toolCallId, params, signal, onUpdate, ctx);
					await Promise.allSettled(pending);
					return result;
				},
			};
		});
		const rejected = createMockModel({
			provider,
			id: "auth-isolation-smol",
			baseUrl: "https://chatgpt.com/backend-api/codex",
			handler: () => ({ stopReason: "error", errorMessage: "401 Unauthorized", responseStatus: 401 }),
		});
		const foreground = createMockModel({
			provider,
			id: "security-mock",
			responses: [
				{
					content: [
						{
							type: "toolCall",
							name: "security_publish",
							arguments: {
								findings: [
									{
										rule_id: "fixture.command-injection",
										title: "Untrusted command reaches a shell",
										summary: "A fixture value is interpolated into a shell command.",
										severity: "high",
										confidence: "high",
										category: "command-injection",
										locations: [{ path: "src/app.ts", start_line: 1, role: "sink" }],
										evidence: [{ label: "shell sink", explanation: "Fixture evidence" }],
										remediation: "Use an argument-vector API.",
										validation: "validated",
									},
								],
								coverage: { completeness: "complete" },
								report: "# Fixture security report\n\nOne validated finding.\n",
							},
						},
					],
				},
				{ content: ["Security publication completed."] },
			],
		});
		const coordinator = new SecurityCoordinator(
			{
				cwd: repoRoot,
				settings: f.settings,
				authStorage: f.storage,
				modelRegistry: f.registry,
				activeModel: foreground,
				sessionId: "parent-session",
				agentId: "Main",
			},
			{ openStore: storeFactory, gitAdapter },
		);
		let drain: Promise<unknown> | undefined;
		// Capture the unbound original so the seam can call through after the gate.
		const realGetApiKey = f.registry.getApiKey;
		try {
			registerMockApi(sourceId);
			vi.spyOn(f.registry, "getAvailable").mockReturnValue([rejected]);
			// Unpinned auth entry points on this instance's registry and storage; the
			// session's pinned path is `oauth.accessById`, spied to prove it is used.
			// The getApiKey seam is where unpinned compression auth enters, so it is
			// held until publication entry before the underlying auth runs.
			const getApiKeySeam = vi
				.spyOn(f.registry, "getApiKey")
				.mockImplementation(async (model, sessionId, options) => {
					unpinnedAuthRequests++;
					await authGate.promise;
					return realGetApiKey.call(f.registry, model, sessionId, options);
				});
			const unpinned = [
				getApiKeySeam,
				vi.spyOn(f.registry, "getApiKeyForProvider"),
				vi.spyOn(f.registry, "getApiKeyAndHeaders"),
				vi.spyOn(f.registry, "getApiKeyWithCredentialForProvider"),
				vi.spyOn(f.storage.keys, "get"),
			];
			const pinnedAccess = vi.spyOn(f.storage.oauth, "accessById");
			await f.storage.usage.reports();
			const plan = await coordinator.preflight({ credentialId: f.account.credentialId, model: foreground });
			const started = await coordinator.start({ planId: plan.id });
			const waitPromise = coordinator.wait(started.operationId);
			drain = waitPromise;
			// Wait only for publication entry — never for refresh entry — then drain
			// and join captured unpinned work while publication is still held.
			await publicationEntered.promise;
			f.release.resolve();
			await Promise.allSettled(pending);
			publicationRelease.resolve();
			const terminal = await waitPromise;
			expect(publicationStarted).toBe(true);
			expect(publicationCalls).toBe(1);
			expect(terminal.phase).toBe("completed");
			expect(terminal.findingCount).toBe(1);
			const bundle = await (await storeFactory()).getBundle(terminal.scanId);
			expect(bundle?.scan.status).toBe("completed");
			expect(bundle?.findings).toHaveLength(1);
			// Zero unpinned auth requests: the caller-owned `getApiKey` owns auth, so
			// the unpinned compressor is neither constructed nor run, and no smol or
			// refresh request ever leaves the session.
			expect(compressorFactory).not.toHaveBeenCalled();
			expect(compressorInvocations).toBe(0);
			expect(unpinnedAuthRequests).toBe(0);
			expect(rejected.calls).toHaveLength(0);
			expect(f.refresh).not.toHaveBeenCalled();
			for (const spy of unpinned) expect(spy).not.toHaveBeenCalled();
			expect(pinnedAccess).toHaveBeenCalled();
			for (const call of pinnedAccess.mock.calls) expect(call[1]).toBe(f.account.credentialId);
			expect(selectSecurityOAuthAccount(f.storage, provider, f.account.credentialId)).toEqual(f.account);
			// The real default session was disposed by the coordinator's teardown.
			expect(capturedSession?.isDisposed).toBe(true);
			expect(AgentRegistry.global().get(`Security-${started.scanId.slice(-12)}`)).toBeUndefined();
			// The pinned credential and its full durable identity survive the scan.
			const nextPlan = await coordinator.preflight({ credentialId: f.account.credentialId, model: foreground });
			expect(nextPlan.account).toEqual({
				provider,
				credentialId: f.account.credentialId,
				accountId: "workspace-fixture",
				email: "security@example.invalid",
				organizationId: "workspace-fixture",
				organizationName: "pro",
			});
		} finally {
			f.release.resolve();
			publicationRelease.resolve();
			authGate.resolve();
			await Promise.allSettled(pending);
			await drain?.catch(() => undefined);
			await capturedSession?.dispose();
			for (const ref of AgentRegistry.global().list()) {
				if (!ref.id.startsWith("Security-")) continue;
				await ref.session?.dispose();
				AgentRegistry.global().unregister(ref.id);
			}
			vi.restoreAllMocks();
			await f.close();
		}
	});
});
