import { spyOn } from "bun:test";
import * as os from "node:os";
import * as path from "node:path";
import { providerEntries } from "@oh-my-pi/pi-catalog/compat/providers";
import { __resetDirsFromEnvForTests, getAgentDir, removeSyncWithRetries, TempDir } from "@oh-my-pi/pi-utils";

/**
 * Ambient AWS credentials that surface a Bedrock source without any provider
 * env key (`AWS_PROFILE` reads the host's `~/.aws` files).
 */
const AMBIENT_AWS_ENV_NAMES = [
	"AWS_ACCESS_KEY_ID",
	"AWS_SECRET_ACCESS_KEY",
	"AWS_SESSION_TOKEN",
	"AWS_BEARER_TOKEN_BEDROCK",
	"AWS_PROFILE",
] as const;

/** Process keys the isolation owns outright (scrubbed, then repointed at the temp home). */
const HOME_ENV_NAMES = [
	"PI_CODING_AGENT_DIR",
	"HOME",
	"USERPROFILE",
	"AWS_SHARED_CREDENTIALS_FILE",
	"AWS_CONFIG_FILE",
] as const;

/**
 * Every environment variable that can carry a host credential into a test:
 * the catalog provider `env` keys plus the ambient AWS set.
 */
export function hostCredentialEnvNames(): string[] {
	const names = new Set<string>();
	for (const provider of Object.values(providerEntries())) {
		for (const name of provider.envVars ?? []) names.add(name);
	}
	for (const name of AMBIENT_AWS_ENV_NAMES) names.add(name);
	return [...names];
}

function isolatedNames(): string[] {
	return [...HOME_ENV_NAMES, ...hostCredentialEnvNames()];
}

/**
 * Copy `base` into a child environment that cannot inherit the host's provider
 * credentials, agent-dir override, or `~/.aws` files. Pure: `base` is not
 * mutated. `AWS_*_FILE` point at absent files under `home`.
 */
export function hostIsolatedEnv(
	base: Record<string, string | undefined>,
	home: string,
): Record<string, string | undefined> {
	const env: Record<string, string | undefined> = { ...base };
	delete env.PI_CODING_AGENT_DIR;
	for (const name of hostCredentialEnvNames()) delete env[name];
	env.HOME = home;
	env.USERPROFILE = home;
	env.AWS_SHARED_CREDENTIALS_FILE = path.join(home, ".aws", "credentials");
	env.AWS_CONFIG_FILE = path.join(home, ".aws", "config");
	return env;
}

export interface HostIsolation {
	/** The isolated home (`opts.home`, or a fresh temp dir). */
	home: string;
	/** `getAgentDir()` while isolation is active — under `home`, missing or empty. */
	agentDir: string;
	/** Restores every managed env key and the dirs resolver. Idempotent. */
	restore(): void;
	[Symbol.dispose](): void;
}

/**
 * Point the current process at an isolated home: scrub the agent-dir override
 * and every host credential key, repoint `HOME`/`USERPROFILE` and the `~/.aws`
 * files, spy `os.homedir()` (Bun caches it), and rebuild the dirs resolver.
 * `restore()` puts each managed key back, ends the spy, rebuilds the resolver,
 * and removes the temp home it made.
 */
export function isolateHost(opts: { home?: string } = {}): HostIsolation {
	const temp = opts.home === undefined ? TempDir.createSync("@omp-host-isolation-") : undefined;
	const home = opts.home ?? temp!.path();
	const homedirSpy = spyOn(os, "homedir").mockReturnValue(home);

	const savedEnv = new Map<string, string | undefined>();
	for (const key of isolatedNames()) {
		savedEnv.set(key, process.env[key]);
		delete process.env[key];
	}
	process.env.HOME = home;
	process.env.USERPROFILE = home;
	process.env.AWS_SHARED_CREDENTIALS_FILE = path.join(home, ".aws", "credentials");
	process.env.AWS_CONFIG_FILE = path.join(home, ".aws", "config");
	__resetDirsFromEnvForTests();
	const agentDir = getAgentDir();

	let restored = false;
	const restore = (): void => {
		if (restored) return;
		restored = true;
		for (const [key, value] of savedEnv) {
			if (value === undefined) {
				delete process.env[key];
				delete Bun.env[key];
			} else {
				process.env[key] = value;
				Bun.env[key] = value;
			}
		}
		homedirSpy.mockRestore();
		__resetDirsFromEnvForTests();
		if (temp) removeSyncWithRetries(temp.path());
	};

	return { home, agentDir, restore, [Symbol.dispose]: restore };
}
