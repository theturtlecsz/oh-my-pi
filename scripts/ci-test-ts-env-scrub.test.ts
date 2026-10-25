import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { isEnoent } from "@oh-my-pi/pi-utils/fs-error";
import { buildChildEnv, withTempAgentDir } from "./ci-test-ts.ts";

const tempDirs: string[] = [];
const dummyValue = "dummy-m1g-value";

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) {
		await fs.rm(dir, { recursive: true, force: true });
	}
});

describe("buildChildEnv secret-name scrubbing", () => {
	it("removes secret-like names including bare and mixed-case names", () => {
		const names = [
			"APP_DB_PASSWORD",
			"APP_SIGNING_SECRET",
			"APP_SSH_PRIVATE_KEY",
			"APP_WEBAUTHN_PASSKEY",
			"DATABASE_URL",
			"PASSWORD",
			"SECRET",
			"app_db_password",
			"Password",
			"STRIPE_SECRET_KEY",
			"my_service_api_key",
		];
		const baseEnv = Object.fromEntries(names.map(name => [name, dummyValue]));
		const env = buildChildEnv({ baseEnv });
		expect(Object.keys(env).filter(key => names.includes(key)).sort()).toEqual([]);
	});

	it("preserves near-miss names and service tokens", () => {
		const names = ["KEEP_ME", "PASSWORD_STORE_DIR", "SECRETS_DIR", "MY_SERVICE_TOKEN", "SECRET_SANTA_LIST"];
		const baseEnv = Object.fromEntries(names.map(name => [name, dummyValue]));
		const env = buildChildEnv({ baseEnv });
		expect(Object.keys(env).filter(key => names.includes(key)).sort()).toEqual([...names].sort());
	});

	it("still removes existing GitHub AWS and provider credentials", () => {
		const names = ["GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY"];
		const baseEnv = Object.fromEntries(names.map(name => [name, dummyValue]));
		const env = buildChildEnv({ baseEnv });
		expect(Object.keys(env).filter(key => names.includes(key)).sort()).toEqual([]);
	});
});

describe("withTempAgentDir private HOME", () => {
	it("gives each call a fresh empty HOME and removes it after the callback", async () => {
		const baseHome = await fs.mkdtemp(path.join(os.tmpdir(), "omp-m1g-base-home-"));
		tempDirs.push(baseHome);
		const tmpRoot = await fs.mkdtemp(path.join(os.tmpdir(), "omp-m1g-runner-"));
		tempDirs.push(tmpRoot);
		const baseEnv = { HOME: baseHome, TMPDIR: tmpRoot };
		const homes = new Set<string>();

		for (let call = 0; call < 2; call++) {
			const home = await withTempAgentDir(async (_agentDir, env) => {
				const home = env.HOME;
				expect(["HOME"].filter(() => home === undefined || home === baseEnv.HOME).sort()).toEqual([]);
				if (home === undefined) throw new Error("HOME missing from child environment");
				expect(["HOME"].filter(() => homes.has(home)).sort()).toEqual([]);
				homes.add(home);
				const stat = await fs.stat(home).catch(() => {
					throw new Error("Child HOME must exist as a directory");
				});
				expect(["HOME"].filter(() => !stat.isDirectory()).sort()).toEqual([]);
				const entries = await fs.readdir(home).catch(() => {
					throw new Error("Could not inspect child HOME directory");
				});
				expect(["HOME"].filter(() => entries.length !== 0).sort()).toEqual([]);
				return home;
			}, baseEnv);

			const remainingKeys = await fs.stat(home).then(
				() => ["HOME"],
				(error: unknown) => {
					if (isEnoent(error)) return [];
					throw new Error("Could not verify child HOME cleanup");
				},
			);
			expect(remainingKeys.sort()).toEqual([]);
		}
	});
});
