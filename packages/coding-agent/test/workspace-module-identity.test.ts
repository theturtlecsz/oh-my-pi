import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { isEnoent } from "@oh-my-pi/pi-utils";

function resolveExportTarget(value: unknown): string | null {
	if (typeof value === "string") {
		return value;
	}
	if (value && typeof value === "object") {
		if (Array.isArray(value)) {
			for (const item of value) {
				const resolved = resolveExportTarget(item);
				if (resolved) return resolved;
			}
			return null;
		}
		const obj = value as Record<string, unknown>;
		if ("import" in obj) {
			const resolved = resolveExportTarget(obj.import);
			if (resolved) return resolved;
		}
		if ("default" in obj) {
			const resolved = resolveExportTarget(obj.default);
			if (resolved) return resolved;
		}
	}
	return null;
}

function formatPathsKey(pkgName: string, exportKey: string): string {
	if (exportKey === ".") {
		return pkgName;
	}
	if (exportKey.startsWith("./")) {
		return `${pkgName}/${exportKey.slice(2)}`;
	}
	return `${pkgName}/${exportKey}`;
}

describe("workspace module identity with foreign node_modules", () => {
	test("resolves workspace package exports to local checkout instead of decoy node_modules", async () => {
		const repoRoot = path.resolve(import.meta.dir, "../../..");
		const packagesDir = path.join(repoRoot, "packages");
		const tsconfigWorkspacePath = path.join(packagesDir, "tsconfig.workspace.json");

		const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "omp-workspace-module-identity-"));

		try {
			// Write tsconfig.json extending the checkout's packages/tsconfig.workspace.json
			await Bun.write(
				path.join(tempDir, "tsconfig.json"),
				`${JSON.stringify({ extends: tsconfigWorkspacePath }, null, "\t")}\n`,
			);

			const entries = await fs.readdir(packagesDir, { withFileTypes: true });
			const packageDirs = entries
				.filter(entry => entry.isDirectory())
				.map(entry => entry.name)
				.sort();

			const decoyRoot = path.join(tempDir, "node_modules");
			const specsMap = new Map<string, string>();

			for (const dirName of packageDirs) {
				const pkgJsonPath = path.join(packagesDir, dirName, "package.json");
				let pkg: { name?: string; exports?: Record<string, unknown> };
				try {
					pkg = (await Bun.file(pkgJsonPath).json()) as { name?: string; exports?: Record<string, unknown> };
				} catch (err) {
					if (isEnoent(err)) continue;
					throw err;
				}

				if (!pkg.name?.startsWith("@oh-my-pi/") || !pkg.exports) {
					continue;
				}

				// pi-natives is a binary addon whose runtime bindings load via dlopen rather than TS source modules
				if (dirName === "natives") {
					continue;
				}

				const decoyPkgDir = path.join(decoyRoot, pkg.name);
				await Bun.write(
					path.join(decoyPkgDir, "package.json"),
					`${JSON.stringify({ name: pkg.name, exports: pkg.exports }, null, "\t")}\n`,
				);

				for (const [exportKey, exportVal] of Object.entries(pkg.exports)) {
					if (exportKey.includes("*")) continue;
					const rawTarget = resolveExportTarget(exportVal);
					if (!rawTarget || rawTarget.includes("*")) continue;

					const relTarget = rawTarget.startsWith("./") ? rawTarget.slice(2) : rawTarget;
					// Write empty decoy file at the export target
					await Bun.write(path.join(decoyPkgDir, relTarget), "");

					const spec = formatPathsKey(pkg.name, exportKey);
					const expectedPath = path.join(packagesDir, dirName, relTarget);
					specsMap.set(spec, expectedPath);
				}
			}

			// Wildcard samples
			const promptName = "review-request";
			const wildcardSamples: Array<{ spec: string; pkgDir: string; relTarget: string }> = [
				{ spec: "@oh-my-pi/pi-ai/oauth", pkgDir: "ai", relTarget: "src/registry/oauth/index.ts" },
				{ spec: "@oh-my-pi/pi-ai/providers/mock", pkgDir: "ai", relTarget: "src/providers/mock.ts" },
				{
					spec: "@oh-my-pi/pi-coding-agent/config/model-registry",
					pkgDir: "coding-agent",
					relTarget: "src/config/model-registry.ts",
				},
				{
					spec: `@oh-my-pi/pi-coding-agent/prompts/${promptName}`,
					pkgDir: "coding-agent",
					relTarget: `src/prompts/${promptName}.md`,
				},
			];

			for (const sample of wildcardSamples) {
				const pkgJsonPath = path.join(packagesDir, sample.pkgDir, "package.json");
				const pkg = (await Bun.file(pkgJsonPath).json()) as { name: string };
				const decoyPkgDir = path.join(decoyRoot, pkg.name);
				await Bun.write(path.join(decoyPkgDir, sample.relTarget), "");
				specsMap.set(sample.spec, path.join(packagesDir, sample.pkgDir, sample.relTarget));
			}

			// Spawn bun with cwd = tempDir to resolve each spec
			const specKeys = Array.from(specsMap.keys());
			const runnerScript = `
const specs = ${JSON.stringify(specKeys)};
const out = {};
for (const spec of specs) {
	try {
		out[spec] = Bun.resolveSync(spec, process.cwd());
	} catch (err) {
		out[spec] = { error: String(err) };
	}
}
console.log(JSON.stringify(out));
`;

			const runnerFile = path.join(tempDir, "resolve-specs.js");
			await Bun.write(runnerFile, runnerScript);

			const proc = Bun.spawnSync([process.execPath, "resolve-specs.js"], {
				cwd: tempDir,
				stdout: "pipe",
				stderr: "pipe",
			});

			expect(proc.exitCode).toBe(0);
			const resolvedMap = JSON.parse(proc.stdout.toString()) as Record<string, string | { error: string }>;

			for (const [spec, expectedPath] of specsMap.entries()) {
				const resolved = resolvedMap[spec];
				expect(resolved, `Spec '${spec}' failed resolution`).toBe(expectedPath);
				expect(
					typeof resolved === "string" && resolved.startsWith(decoyRoot),
					`Spec '${spec}' resolved under decoy: ${String(resolved)}`,
				).toBe(false);
			}
		} finally {
			await fs.rm(tempDir, { recursive: true, force: true });
		}
	});
});
