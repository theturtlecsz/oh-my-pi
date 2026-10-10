import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { Settings, resetSettingsForTest } from "@oh-my-pi/pi-coding-agent/config/settings";
import { discoverExtensionPaths } from "@oh-my-pi/pi-coding-agent/extensibility/extensions/loader";
import { discoverSessionExtensionPaths } from "@oh-my-pi/pi-coding-agent/sdk";
import { TempDir } from "@oh-my-pi/pi-utils";

describe("extension discovery project trust gate (L-RT-04)", () => {
	let tempDir: TempDir;
	let projectDir: string;

	beforeEach(() => {
		tempDir = TempDir.createSync("@pi-ext-untrusted-");
		projectDir = path.join(tempDir.path(), "proj");
		fs.mkdirSync(path.join(projectDir, ".omp", "extensions"), { recursive: true });
		fs.writeFileSync(path.join(projectDir, ".omp", "extensions", "x.ts"), "export default function() {}\n");
		fs.writeFileSync(path.join(tempDir.path(), "y.ts"), "export default function() {}\n");
		fs.writeFileSync(path.join(tempDir.path(), "z.ts"), "export default function() {}\n");
		fs.writeFileSync(path.join(projectDir, ".omp", "settings.json"), JSON.stringify({ extensions: ["../y.ts"] }));
		resetSettingsForTest();
	});
	afterEach(() => {
		resetSettingsForTest();
		tempDir.removeSync();
	});

	const has = (paths: string[], target: string): boolean =>
		paths.some(candidate => path.resolve(candidate) === path.resolve(target));

	it("skips project-level native extensions and project settings entries when untrusted", async () => {
		const paths = await discoverExtensionPaths([], projectDir, undefined, { projectTrusted: false });

		expect(has(paths, path.join(projectDir, ".omp", "extensions", "x.ts"))).toBe(false);
		expect(has(paths, path.join(tempDir.path(), "y.ts"))).toBe(false);
	});

	it("keeps project-level native extensions and project settings entries when trusted or unspecified", async () => {
		const trusted = await discoverExtensionPaths([], projectDir, undefined, { projectTrusted: true });
		const unspecified = await discoverExtensionPaths([], projectDir, undefined, {});

		for (const paths of [trusted, unspecified]) {
			expect(has(paths, path.join(projectDir, ".omp", "extensions", "x.ts"))).toBe(true);
			expect(has(paths, path.join(tempDir.path(), "y.ts"))).toBe(true);
		}
	});

	it("still loads explicitly configured paths when the project is untrusted", async () => {
		const paths = await discoverExtensionPaths(["../z.ts"], projectDir, undefined, { projectTrusted: false });

		expect(has(paths, path.join(tempDir.path(), "z.ts"))).toBe(true);
	});

	it("gates session discovery on project trust from trusted-projects.json", async () => {
		const agentDir = path.join(tempDir.path(), "agent");
		fs.mkdirSync(agentDir, { recursive: true });

		// Untrusted: no trusted-projects.json yet, so the project-native x.ts and
		// the project-settings `../y.ts` entry are both withheld.
		const untrustedSettings = await Settings.init({ inMemory: true, cwd: projectDir, agentDir });
		const untrusted = await discoverSessionExtensionPaths({}, projectDir, untrustedSettings);
		expect(has(untrusted, path.join(projectDir, ".omp", "extensions", "x.ts"))).toBe(false);
		expect(has(untrusted, path.join(tempDir.path(), "y.ts"))).toBe(false);

		// Trusted: after the project lands in the allowlist, both load again.
		fs.writeFileSync(
			path.join(agentDir, "trusted-projects.json"),
			JSON.stringify({ version: 1, paths: [projectDir] }),
		);
		resetSettingsForTest();
		const trustedSettings = await Settings.init({ inMemory: true, cwd: projectDir, agentDir });
		const trusted = await discoverSessionExtensionPaths({}, projectDir, trustedSettings);
		expect(has(trusted, path.join(projectDir, ".omp", "extensions", "x.ts"))).toBe(true);
		expect(has(trusted, path.join(tempDir.path(), "y.ts"))).toBe(true);
	});
});
