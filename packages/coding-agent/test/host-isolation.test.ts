import { describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { getEnvApiKey } from "@oh-my-pi/pi-ai";
import { providerEntries } from "@oh-my-pi/pi-catalog/compat/providers";
import { __resetDirsFromEnvForTests, getAgentDir, TempDir } from "@oh-my-pi/pi-utils";
import { hostCredentialEnvNames, hostIsolatedEnv, isolateHost } from "./helpers/host-isolation";

describe("host isolation helper", () => {
	it("lists every catalog provider env key plus the ambient AWS set", () => {
		const names = new Set(hostCredentialEnvNames());
		for (const provider of Object.values(providerEntries())) {
			for (const name of provider.envVars ?? []) expect(names.has(name)).toBe(true);
		}
		for (const name of ["OPENAI_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "AWS_PROFILE"]) {
			expect(names.has(name)).toBe(true);
		}
	});

	it("hostIsolatedEnv drops host credentials and leaves the input untouched", () => {
		const base = {
			PI_CODING_AGENT_DIR: "/host/agent",
			OPENAI_API_KEY: "sk-test",
			AWS_PROFILE: "default",
			KEEP: "1",
		};
		const env = hostIsolatedEnv(base, "/iso/home");

		expect(env.PI_CODING_AGENT_DIR).toBeUndefined();
		expect(env.OPENAI_API_KEY).toBeUndefined();
		expect(env.AWS_PROFILE).toBeUndefined();
		expect(env.KEEP).toBe("1");
		expect(env.HOME).toBe("/iso/home");
		expect(env.USERPROFILE).toBe("/iso/home");
		expect(env.AWS_SHARED_CREDENTIALS_FILE).toBe(path.join("/iso/home", ".aws", "credentials"));
		expect(env.AWS_CONFIG_FILE).toBe(path.join("/iso/home", ".aws", "config"));

		expect(base).toEqual({
			PI_CODING_AGENT_DIR: "/host/agent",
			OPENAI_API_KEY: "sk-test",
			AWS_PROFILE: "default",
			KEEP: "1",
		});
	});

	it("isolates the process and restores the prior agent dir and credential on restore", () => {
		using before = TempDir.createSync("@omp-host-isolation-before-");
		const priorAgentDirEnv = process.env.PI_CODING_AGENT_DIR;
		const priorOpenai = process.env.OPENAI_API_KEY;
		fs.writeFileSync(path.join(before.path(), "session.json"), "{}");

		const setup = (): void => {
			process.env.PI_CODING_AGENT_DIR = before.path();
			process.env.OPENAI_API_KEY = "sk-test";
			__resetDirsFromEnvForTests();
		};
		const teardown = (): void => {
			if (priorAgentDirEnv === undefined) delete process.env.PI_CODING_AGENT_DIR;
			else process.env.PI_CODING_AGENT_DIR = priorAgentDirEnv;
			if (priorOpenai === undefined) delete process.env.OPENAI_API_KEY;
			else process.env.OPENAI_API_KEY = priorOpenai;
			__resetDirsFromEnvForTests();
		};

		try {
			setup();
			const agentDirBefore = getAgentDir();
			expect(agentDirBefore).toBe(before.path());

			using isolated = isolateHost();
			expect(isolated.agentDir).toBe(getAgentDir());
			expect(path.relative(isolated.home, isolated.agentDir).startsWith("..")).toBe(false);
			expect(process.env.PI_CODING_AGENT_DIR).toBeUndefined();
			expect(Bun.env.PI_CODING_AGENT_DIR).toBeUndefined();
			expect(process.env.OPENAI_API_KEY).toBeUndefined();
			expect(Bun.env.OPENAI_API_KEY).toBeUndefined();
			expect(getEnvApiKey("openai")).toBeUndefined();
			expect(!fs.existsSync(isolated.agentDir) || fs.readdirSync(isolated.agentDir).length === 0).toBe(true);

			isolated.restore();
			isolated.restore();
			expect(process.env.PI_CODING_AGENT_DIR).toBe(before.path());
			expect(process.env.OPENAI_API_KEY).toBe("sk-test");
			expect(getAgentDir()).toBe(agentDirBefore);
			expect(fs.existsSync(isolated.home)).toBe(false);
		} finally {
			teardown();
		}
	});
});
