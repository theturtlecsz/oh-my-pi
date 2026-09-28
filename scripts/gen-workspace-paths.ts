#!/usr/bin/env bun
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { $ } from "bun";
import { isEnoent } from "../packages/utils/src/fs-error.ts";

/**
 * Resolve target path from an export value (string, conditions object, or array).
 * Respects `import` condition first, then `default` fallback.
 */
export function resolveExportTarget(value: unknown): string | null {
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

/** Format export key into a paths mapping key (e.g. `.` -> `@pkg`, `./oauth` -> `@pkg/oauth`). */
export function formatPathsKey(pkgName: string, exportKey: string): string {
	if (exportKey === ".") {
		return pkgName;
	}
	if (exportKey.startsWith("./")) {
		return `${pkgName}/${exportKey.slice(2)}`;
	}
	return `${pkgName}/${exportKey}`;
}

/**
 * Format raw package-relative target into paths target relative to `packages/`.
 * For native addon subpaths, route through the package directory / subpath so TypeScript resolves
 * declarations via `package.json#types` while Bun resolves the runtime bindings via `package.json#import`.
 */
export function formatPathsTarget(pkgDirName: string, rawTarget: string): string {
	const rel = rawTarget.startsWith("./") ? rawTarget.slice(2) : rawTarget;
	if (pkgDirName === "natives" && rel.startsWith("native/")) {
		const sub = rel.slice("native/".length);
		if (sub === "index.js") {
			return `./${pkgDirName}`;
		}
		if (sub.endsWith(".js")) {
			return `./${pkgDirName}/${sub.slice(0, -3)}`;
		}
	}
	return `./${pkgDirName}/${rel}`;
}

/** Extract the package name (e.g. `@oh-my-pi/pi-ai`) from a full paths key. */
export function getPackageName(key: string): string {
	if (key.startsWith("@")) {
		const slashIndex = key.indexOf("/", key.indexOf("/") + 1);
		return slashIndex === -1 ? key : key.slice(0, slashIndex);
	}
	const slashIndex = key.indexOf("/");
	return slashIndex === -1 ? key : key.slice(0, slashIndex);
}

/**
 * Compare two paths keys according to Node exports precedence:
 * 1. Scope/package alphabetical order.
 * 2. Exact match (no `*`) before pattern match (with `*`).
 * 3. Pattern match precedence: longer base prefix first, longer total length (suffix) first.
 */
export function comparePathsKeys(a: string, b: string): number {
	const pkgA = getPackageName(a);
	const pkgB = getPackageName(b);
	if (pkgA !== pkgB) {
		return pkgA.localeCompare(pkgB);
	}

	const aHasStar = a.includes("*");
	const bHasStar = b.includes("*");

	// 1. Exact matches (no *) always come before wildcard/pattern matches (has *)
	if (!aHasStar && bHasStar) return -1;
	if (aHasStar && !bHasStar) return 1;

	// 2. Both are exact matches: sort alphabetically
	if (!aHasStar && !bHasStar) {
		return a.localeCompare(b);
	}

	// 3. Both are pattern matches: follow Node's PATTERN_KEY_COMPARE
	const aStarIndex = a.indexOf("*");
	const bStarIndex = b.indexOf("*");
	const aBaseLength = aStarIndex + 1;
	const bBaseLength = bStarIndex + 1;

	// Longer base prefix first
	if (aBaseLength !== bBaseLength) {
		return bBaseLength - aBaseLength;
	}

	// Longer total length (longer suffix) first
	if (a.length !== b.length) {
		return b.length - a.length;
	}

	// Stable tie-breaker
	return a.localeCompare(b);
}

/** Convert a package's exports definition into sorted `compilerOptions.paths` mappings. */
export function exportsToPaths(
	pkgName: string,
	pkgDirName: string,
	exports: Record<string, unknown>,
): Record<string, string[]> {
	const entries: Array<{ key: string; target: string }> = [];

	for (const [exportKey, exportVal] of Object.entries(exports)) {
		const rawTarget = resolveExportTarget(exportVal);
		if (!rawTarget) continue;

		const key = formatPathsKey(pkgName, exportKey);
		const target = formatPathsTarget(pkgDirName, rawTarget);
		entries.push({ key, target });
	}

	entries.sort((a, b) => comparePathsKeys(a.key, b.key));

	const paths: Record<string, string[]> = {};
	for (const entry of entries) {
		paths[entry.key] = [entry.target];
	}
	return paths;
}

/** Scan all `packages/*` and generate the complete workspace `compilerOptions.paths`. */
export async function generateWorkspacePaths(packagesDir: string): Promise<Record<string, string[]>> {
	const entries = await fs.readdir(packagesDir, { withFileTypes: true });
	const dirNames = entries
		.filter(entry => entry.isDirectory())
		.map(entry => entry.name)
		.sort();

	const allEntries: Array<{ key: string; target: string }> = [];

	for (const dirName of dirNames) {
		const pkgPath = path.join(packagesDir, dirName, "package.json");
		let pkg: { name?: string; exports?: Record<string, unknown> };
		try {
			pkg = (await Bun.file(pkgPath).json()) as { name?: string; exports?: Record<string, unknown> };
		} catch (err) {
			if (isEnoent(err)) continue;
			throw err;
		}

		if (!pkg.name?.startsWith("@oh-my-pi/") || !pkg.exports) {
			continue;
		}

		for (const [exportKey, exportVal] of Object.entries(pkg.exports)) {
			const rawTarget = resolveExportTarget(exportVal);
			if (!rawTarget) continue;

			const key = formatPathsKey(pkg.name, exportKey);
			const target = formatPathsTarget(dirName, rawTarget);
			allEntries.push({ key, target });
		}
	}

	allEntries.sort((a, b) => comparePathsKeys(a.key, b.key));

	const allPaths: Record<string, string[]> = {};
	for (const entry of allEntries) {
		allPaths[entry.key] = [entry.target];
	}

	return allPaths;
}

export async function readWorkspaceTsConfig(tsconfigPath: string): Promise<Record<string, unknown>> {
	try {
		const text = await Bun.file(tsconfigPath).text();
		return Bun.JSON5.parse(text) as Record<string, unknown>;
	} catch (err) {
		if (isEnoent(err)) {
			throw new Error(`tsconfig not found: ${tsconfigPath}`);
		}
		throw err;
	}
}

export function updateTsConfigPaths(
	config: Record<string, unknown>,
	paths: Record<string, string[]>,
): Record<string, unknown> {
	const compilerOptions = (config.compilerOptions ?? {}) as Record<string, unknown>;
	const updatedCompilerOptions = {
		...compilerOptions,
		paths,
	};

	const updated: Record<string, unknown> = {};
	if ("extends" in config) updated.extends = config.extends;
	updated.compilerOptions = updatedCompilerOptions;
	for (const [k, v] of Object.entries(config)) {
		if (k !== "extends" && k !== "compilerOptions") {
			updated[k] = v;
		}
	}
	return updated;
}

export async function checkWorkspacePaths(
	tsconfigPath: string,
	packagesDir: string,
): Promise<{ ok: boolean; driftDetails?: string }> {
	const config = await readWorkspaceTsConfig(tsconfigPath);
	const compilerOptions = (config.compilerOptions ?? {}) as Record<string, unknown>;
	const currentPaths = (compilerOptions.paths ?? {}) as Record<string, unknown>;
	const expectedPaths = await generateWorkspacePaths(packagesDir);

	const currentJson = JSON.stringify(currentPaths);
	const expectedJson = JSON.stringify(expectedPaths);

	if (currentJson !== expectedJson) {
		return {
			ok: false,
			driftDetails: `${tsconfigPath} compilerOptions.paths differs from expected workspace package exports. Run \`bun run gen:workspace-paths\` to regenerate.`,
		};
	}

	return { ok: true };
}

export async function writeWorkspacePaths(tsconfigPath: string, packagesDir: string, repoRoot: string): Promise<void> {
	const config = await readWorkspaceTsConfig(tsconfigPath);
	const expectedPaths = await generateWorkspacePaths(packagesDir);
	const updated = updateTsConfigPaths(config, expectedPaths);

	await Bun.write(tsconfigPath, `${JSON.stringify(updated, null, "\t")}\n`);
	await $`biome format --write ${tsconfigPath}`.cwd(repoRoot).quiet().nothrow();
}

async function main(): Promise<void> {
	let check = false;
	const repoRoot = path.resolve(import.meta.dir, "..");
	let tsconfigPath = path.join(repoRoot, "packages/tsconfig.workspace.json");
	let packagesDir = path.join(repoRoot, "packages");

	const args = process.argv.slice(2);
	for (let i = 0; i < args.length; i++) {
		const arg = args[i];
		if (arg === "--check") {
			check = true;
		} else if (arg === "--tsconfig") {
			tsconfigPath = path.resolve(args[++i] ?? "");
		} else if (arg === "--packages") {
			packagesDir = path.resolve(args[++i] ?? "");
		} else if (arg === "-h" || arg === "--help") {
			console.log("Usage: bun scripts/gen-workspace-paths.ts [--check] [--tsconfig <path>] [--packages <dir>]");
			process.exit(0);
		} else {
			console.error(`Unknown argument: ${arg}`);
			process.exit(2);
		}
	}

	if (check) {
		const result = await checkWorkspacePaths(tsconfigPath, packagesDir);
		if (!result.ok) {
			console.error(`ERROR: ${result.driftDetails}`);
			process.exit(1);
		}
		console.log("PASS: workspace paths are up to date");
		process.exit(0);
	}

	await writeWorkspacePaths(tsconfigPath, packagesDir, repoRoot);
	console.log(`Updated workspace paths in ${tsconfigPath}`);
}

if (import.meta.main) {
	await main();
}
