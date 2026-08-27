import { afterEach, beforeEach, describe, expect, it, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Settings } from "@oh-my-pi/pi-coding-agent/config/settings";
import { logger, TempDir } from "@oh-my-pi/pi-utils";
import { beginSettingsTest, restoreSettingsTestState, type SettingsTestState } from "../helpers/settings-test-state";

describe("Settings backup-on-rewrite and real agent dir guard", () => {
	let settingsState: SettingsTestState | undefined;
	let tempDir: TempDir;
	let agentDir: string;
	let projectDir: string;

	beforeEach(() => {
		settingsState = beginSettingsTest();
		tempDir = TempDir.createSync("@pi-backup-test-");
		agentDir = tempDir.join("agent");
		projectDir = tempDir.join("project");

		fs.mkdirSync(agentDir, { recursive: true });
		fs.mkdirSync(path.join(projectDir, ".omp"), { recursive: true });
	});

	afterEach(() => {
		restoreSettingsTestState(settingsState);
		try {
			tempDir.removeSync();
		} catch {}
	});

	it("creates .bak.1 with previous contents on rewrite", async () => {
		const configPath = path.join(agentDir, "config.yml");
		const initialContent = "theme:\n  dark: theme-0\n";
		fs.writeFileSync(configPath, initialContent, "utf8");

		const s = await Settings.init({ agentDir, cwd: projectDir });
		s.set("theme.dark", "theme-1");
		await s.flush();

		expect(fs.readFileSync(configPath, "utf8")).toContain("theme-1");
		const bak1Path = `${configPath}.bak.1`;
		expect(fs.existsSync(bak1Path)).toBe(true);
		expect(fs.readFileSync(bak1Path, "utf8")).toBe(initialContent);
	});

	it("rotates up to 5 backup generations and caps at 5", async () => {
		const configPath = path.join(agentDir, "config.yml");
		const initialContent = "theme:\n  dark: theme-0\n";
		fs.writeFileSync(configPath, initialContent, "utf8");

		const s = await Settings.init({ agentDir, cwd: projectDir });

		const savedContents: string[] = [initialContent];
		for (let i = 1; i <= 7; i++) {
			s.set("theme.dark", `theme-${i}`);
			await s.flush();
			savedContents.push(fs.readFileSync(configPath, "utf8"));
		}

		// Current config should be theme-7 (savedContents[7])
		expect(fs.readFileSync(configPath, "utf8")).toBe(savedContents[7]!);

		// .bak.1 should be theme-6 (savedContents[6]), .bak.2 should be theme-5, ..., .bak.5 should be theme-2 (savedContents[2])
		expect(fs.existsSync(`${configPath}.bak.1`)).toBe(true);
		expect(fs.readFileSync(`${configPath}.bak.1`, "utf8")).toBe(savedContents[6]!);

		expect(fs.existsSync(`${configPath}.bak.2`)).toBe(true);
		expect(fs.readFileSync(`${configPath}.bak.2`, "utf8")).toBe(savedContents[5]!);

		expect(fs.existsSync(`${configPath}.bak.3`)).toBe(true);
		expect(fs.readFileSync(`${configPath}.bak.3`, "utf8")).toBe(savedContents[4]!);

		expect(fs.existsSync(`${configPath}.bak.4`)).toBe(true);
		expect(fs.readFileSync(`${configPath}.bak.4`, "utf8")).toBe(savedContents[3]!);

		expect(fs.existsSync(`${configPath}.bak.5`)).toBe(true);
		expect(fs.readFileSync(`${configPath}.bak.5`, "utf8")).toBe(savedContents[2]!);

		// .bak.6 should not exist (capped at 5 generations)
		expect(fs.existsSync(`${configPath}.bak.6`)).toBe(false);
	});

	it("refuses to persist smoke/* or mock/* model roles to real agent directory", async () => {
		const fakeHome = tempDir.join("fake-home");
		const realAgentDir = path.normalize(path.join(fakeHome, ".omp", "agent"));
		fs.mkdirSync(realAgentDir, { recursive: true });
		const realConfigPath = path.join(realAgentDir, "config.yml");
		fs.writeFileSync(realConfigPath, "theme:\n  dark: original-theme\n", "utf8");

		vi.spyOn(os, "homedir").mockReturnValue(fakeHome);
		process.env.HOME = fakeHome;
		Bun.env.HOME = fakeHome;

		const warnSpy = vi.spyOn(logger, "warn");

		// Create Settings pointing to realAgentDir
		const s = await Settings.init({ agentDir: realAgentDir, cwd: projectDir });

		s.setModelRole("default", "smoke/test-model-role");
		await s.flush();

		expect(warnSpy).toHaveBeenCalledWith(
			"Settings: refusing to persist test model role to real agent directory",
			expect.objectContaining({ role: "default", value: "smoke/test-model-role" }),
		);

		// Verify fake real config file was not altered
		expect(fs.readFileSync(realConfigPath, "utf8")).toBe("theme:\n  dark: original-theme\n");

		s.set("modelRoles", { smol: "mock/smol-model" });
		await s.flush();

		expect(warnSpy).toHaveBeenCalledWith(
			"Settings: refusing to persist test model role to real agent directory",
			expect.objectContaining({ role: "smol", value: "mock/smol-model" }),
		);

		expect(fs.readFileSync(realConfigPath, "utf8")).toBe("theme:\n  dark: original-theme\n");
	});

	it("refuses all persistence to real agent directory when OMP_REAL_AGENT_DIR_READONLY is set", async () => {
		const fakeHome = tempDir.join("fake-home-ro");
		const realAgentDir = path.normalize(path.join(fakeHome, ".omp", "agent"));
		fs.mkdirSync(realAgentDir, { recursive: true });
		const realConfigPath = path.join(realAgentDir, "config.yml");
		fs.writeFileSync(realConfigPath, "theme:\n  dark: ro-theme\n", "utf8");

		vi.spyOn(os, "homedir").mockReturnValue(fakeHome);
		process.env.HOME = fakeHome;
		Bun.env.HOME = fakeHome;
		process.env.OMP_REAL_AGENT_DIR_READONLY = "1";

		const warnSpy = vi.spyOn(logger, "warn");

		const s = await Settings.init({ agentDir: realAgentDir, cwd: projectDir });
		s.set("theme.dark", "should-not-persist");
		await s.flush();

		expect(warnSpy).toHaveBeenCalledWith(
			"Settings: refusing to persist to real agent directory (OMP_REAL_AGENT_DIR_READONLY)",
			expect.anything(),
		);

		expect(fs.readFileSync(realConfigPath, "utf8")).toBe("theme:\n  dark: ro-theme\n");
	});

	it("persists legitimate settings while filtering test model roles in a mixed save", async () => {
		const fakeHome = tempDir.join("fake-home-mixed");
		const realAgentDir = path.normalize(path.join(fakeHome, ".omp", "agent"));
		fs.mkdirSync(realAgentDir, { recursive: true });
		const realConfigPath = path.join(realAgentDir, "config.yml");
		fs.writeFileSync(realConfigPath, "theme:\n  dark: old-theme\n", "utf8");

		vi.spyOn(os, "homedir").mockReturnValue(fakeHome);
		process.env.HOME = fakeHome;
		Bun.env.HOME = fakeHome;

		const warnSpy = vi.spyOn(logger, "warn");

		const s = await Settings.init({ agentDir: realAgentDir, cwd: projectDir });
		s.setModelRole("default", "smoke/test-model-role");
		s.setModelRole("smol", "mock/smol-model");
		s.set("theme.dark", "new-theme");
		await s.flush();

		expect(warnSpy).toHaveBeenCalledWith(
			"Settings: refusing to persist test model role to real agent directory",
			expect.objectContaining({ role: "default", value: "smoke/test-model-role" }),
		);
		expect(warnSpy).toHaveBeenCalledWith(
			"Settings: refusing to persist test model role to real agent directory",
			expect.objectContaining({ role: "smol", value: "mock/smol-model" }),
		);

		const updatedConfig = fs.readFileSync(realConfigPath, "utf8");
		expect(updatedConfig).toContain("new-theme");
		expect(updatedConfig).not.toContain("smoke/");
		expect(updatedConfig).not.toContain("mock/");

		s.set("modelRoles", { custom: "mock/custom-model" });
		s.set("theme.dark", "newer-theme");
		await s.flush();

		expect(warnSpy).toHaveBeenCalledWith(
			"Settings: refusing to persist test model role to real agent directory",
			expect.objectContaining({ role: "custom", value: "mock/custom-model" }),
		);

		const finalConfig = fs.readFileSync(realConfigPath, "utf8");
		expect(finalConfig).toContain("newer-theme");
		expect(finalConfig).not.toContain("mock/custom-model");

		// Test a mixed map with both valid and mock roles plus an ordinary setting
		s.set("modelRoles", { default: "openai/gpt-4o", smol: "mock/smol-fake" });
		s.set("theme.dark", "mixed-map-theme");
		await s.flush();

		expect(warnSpy).toHaveBeenCalledWith(
			"Settings: refusing to persist test model role to real agent directory",
			expect.objectContaining({ role: "smol", value: "mock/smol-fake" }),
		);

		const mixedMapConfig = fs.readFileSync(realConfigPath, "utf8");
		expect(mixedMapConfig).toContain("mixed-map-theme");
		expect(mixedMapConfig).toContain("openai/gpt-4o");
		expect(mixedMapConfig).not.toContain("mock/smol-fake");
	});
});
