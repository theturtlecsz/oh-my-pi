import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";
import { checkWorkspacePaths, comparePathsKeys, exportsToPaths } from "./gen-workspace-paths";

describe("exportsToPaths", () => {
	test("exact vs wildcard precedence: exact export subpaths precede wildcard patterns", () => {
		const fixture = {
			".": "./src/index.ts",
			"./*": "./src/*.ts",
			"./oauth": "./src/registry/oauth/index.ts",
			"./oauth/*": "./src/registry/oauth/*.ts",
			"./auth-broker": "./src/auth-broker/index.ts",
		};

		const paths = exportsToPaths("@oh-my-pi/pi-ai", "ai", fixture);
		const keys = Object.keys(paths);

		// All exact keys must appear before any wildcard key
		const firstWildcardIndex = keys.findIndex(k => k.includes("*"));
		expect(firstWildcardIndex).toBeGreaterThan(-1);

		const exactKeys = keys.slice(0, firstWildcardIndex);
		const wildcardKeys = keys.slice(firstWildcardIndex);

		expect(exactKeys).toEqual(["@oh-my-pi/pi-ai", "@oh-my-pi/pi-ai/auth-broker", "@oh-my-pi/pi-ai/oauth"]);

		expect(wildcardKeys).toEqual(["@oh-my-pi/pi-ai/oauth/*", "@oh-my-pi/pi-ai/*"]);

		// Verify target resolution
		expect(paths["@oh-my-pi/pi-ai/oauth"]).toEqual(["./ai/src/registry/oauth/index.ts"]);
		expect(paths["@oh-my-pi/pi-ai/*"]).toEqual(["./ai/src/*.ts"]);
	});

	test("suffix pattern precedence: suffix patterns and specific prefixes precede general wildcard", () => {
		const fixture = {
			"./*": "./src/*.ts",
			"./*.js": "./src/*.ts",
			"./prompts/*": "./src/prompts/*.md",
			"./providers/*": "./src/providers/*.ts",
			"./providers/openai-codex/*": "./src/providers/openai-codex/*.ts",
		};

		const paths = exportsToPaths("@oh-my-pi/pi-coding-agent", "coding-agent", fixture);
		const keys = Object.keys(paths);

		// Longer base prefixes precede shorter base prefixes
		expect(keys.indexOf("@oh-my-pi/pi-coding-agent/providers/openai-codex/*")).toBeLessThan(
			keys.indexOf("@oh-my-pi/pi-coding-agent/providers/*"),
		);
		expect(keys.indexOf("@oh-my-pi/pi-coding-agent/prompts/*")).toBeLessThan(
			keys.indexOf("@oh-my-pi/pi-coding-agent/*"),
		);

		// Suffix pattern ./*.js precedes catch-all ./*
		expect(keys.indexOf("@oh-my-pi/pi-coding-agent/*.js")).toBeLessThan(keys.indexOf("@oh-my-pi/pi-coding-agent/*"));

		// Targets retain their extension patterns
		expect(paths["@oh-my-pi/pi-coding-agent/prompts/*"]).toEqual(["./coding-agent/src/prompts/*.md"]);
		expect(paths["@oh-my-pi/pi-coding-agent/*.js"]).toEqual(["./coding-agent/src/*.ts"]);
	});

	test("conditions object: resolves import condition, nested conditions, and defaults", () => {
		const fixture = {
			".": {
				types: "./src/index.d.ts",
				import: "./src/index.ts",
			},
			"./nested": {
				import: {
					types: "./src/nested.d.ts",
					default: "./src/nested.ts",
				},
			},
			"./fallback": {
				default: "./src/fallback.ts",
			},
			"./unsupported": {
				require: "./dist/index.cjs",
			},
		};

		const paths = exportsToPaths("@oh-my-pi/test-pkg", "test-pkg", fixture);

		expect(paths["@oh-my-pi/test-pkg"]).toEqual(["./test-pkg/src/index.ts"]);
		expect(paths["@oh-my-pi/test-pkg/nested"]).toEqual(["./test-pkg/src/nested.ts"]);
		expect(paths["@oh-my-pi/test-pkg/fallback"]).toEqual(["./test-pkg/src/fallback.ts"]);
		expect(paths["@oh-my-pi/test-pkg/unsupported"]).toBeUndefined();
	});
});

describe("comparePathsKeys", () => {
	test("orders exact match keys before pattern keys within a package", () => {
		expect(comparePathsKeys("@oh-my-pi/pi-ai/oauth", "@oh-my-pi/pi-ai/*")).toBeLessThan(0);
		expect(comparePathsKeys("@oh-my-pi/pi-ai/*", "@oh-my-pi/pi-ai/oauth")).toBeGreaterThan(0);
	});

	test("orders longer base pattern prefixes before shorter base prefixes", () => {
		expect(comparePathsKeys("@oh-my-pi/pi-ai/providers/openai-codex/*", "@oh-my-pi/pi-ai/providers/*")).toBeLessThan(
			0,
		);
	});

	test("orders suffix patterns before pattern keys with no suffix", () => {
		expect(comparePathsKeys("@oh-my-pi/pi-ai/*.js", "@oh-my-pi/pi-ai/*")).toBeLessThan(0);
	});
});

describe("checkWorkspacePaths", () => {
	test("passes when tsconfig paths match workspace exports", async () => {
		const repoRoot = path.resolve(import.meta.dir, "..");
		const tsconfigPath = path.join(repoRoot, "packages/tsconfig.workspace.json");
		const packagesDir = path.join(repoRoot, "packages");

		const result = await checkWorkspacePaths(tsconfigPath, packagesDir);
		expect(result.ok).toBe(true);
	});

	test("--check fails when tsconfig paths drift from expected", async () => {
		const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-drift-test-"));
		try {
			const fakeTsconfig = path.join(tmpDir, "tsconfig.workspace.json");
			await Bun.write(
				fakeTsconfig,
				JSON.stringify({
					extends: "../tsconfig.base.json",
					compilerOptions: {
						paths: {
							"@oh-my-pi/pi-ai": ["./ai/src/index.ts"],
							// Missing all other mappings
						},
					},
				}),
			);

			const repoRoot = path.resolve(import.meta.dir, "..");
			const packagesDir = path.join(repoRoot, "packages");

			const result = await checkWorkspacePaths(fakeTsconfig, packagesDir);
			expect(result.ok).toBe(false);
			expect(result.driftDetails).toContain("differs from expected");

			// Test CLI exit code on drift
			const cliScript = path.join(repoRoot, "scripts/gen-workspace-paths.ts");
			const proc = await $`bun ${cliScript} --check --tsconfig ${fakeTsconfig} --packages ${packagesDir}`
				.quiet()
				.nothrow();
			expect(proc.exitCode).toBe(1);
		} finally {
			await fs.rm(tmpDir, { recursive: true, force: true });
		}
	});
});
