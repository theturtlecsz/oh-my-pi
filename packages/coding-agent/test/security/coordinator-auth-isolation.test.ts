import { afterEach, describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { unregisterCustomApis } from "@oh-my-pi/pi-ai/api-registry";
import { AuthStorage, SqliteAuthCredentialStore } from "@oh-my-pi/pi-ai/auth-storage";
import { createMockModel, registerMockApi } from "@oh-my-pi/pi-ai/providers/mock";
import { ModelRegistry } from "../../src/config/model-registry";
import { Settings } from "../../src/config/settings";
import * as skillDescriptions from "../../src/extensibility/skill-descriptions";
import type { Skill } from "../../src/extensibility/skills";
import { AgentRegistry } from "../../src/registry/agent-registry";
import { createAgentSession } from "../../src/sdk";
import { selectSecurityOAuthAccount } from "../../src/security/auth";
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
});
