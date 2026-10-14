/** Usage: bun session-system/ecc/adapter/emit-catalog.ts [--ecc-root <dir>] [--check] */
import * as path from "node:path";
import { ECC_ROOT, loadManifest } from "./catalog";
import { buildFileCatalog, type FileCatalog } from "./file-catalog";

async function main(): Promise<void> {
	try {
		let eccRoot = ECC_ROOT;
		let check = false;
		const argv = process.argv.slice(2);
		for (let i = 0; i < argv.length; i++) {
			const arg = argv[i] ?? "";
			if (arg === "--check") check = true;
			else if (arg === "--ecc-root" || arg.startsWith("--ecc-root=")) {
				const value = arg === "--ecc-root" ? argv[++i] : arg.slice("--ecc-root=".length);
				if (!value || value.startsWith("-")) throw new Error("--ecc-root requires a value");
				eccRoot = path.resolve(value);
			} else throw new Error(`unknown argument ${arg}`);
		}
		const fresh = await buildFileCatalog(eccRoot, await loadManifest(eccRoot));
		const catalogPath = path.join(eccRoot, "catalog.json");
		if (check) {
			const committed = (await Bun.file(catalogPath).json()) as FileCatalog;
			const byPath = new Map(committed.files.map(file => [file.path, file]));
			const freshByPath = new Map(fresh.files.map(file => [file.path, file]));
			for (const file of [...new Set([...byPath.keys(), ...freshByPath.keys()])].sort()) {
				if (JSON.stringify(byPath.get(file)) !== JSON.stringify(freshByPath.get(file))) {
					throw new Error(`stale catalog path ${file}`);
				}
			}
			if (JSON.stringify(committed) !== JSON.stringify(fresh))
				throw new Error("stale catalog.json metadata or ordering");
			process.stdout.write("verified catalog.json\n");
		} else {
			await Bun.write(catalogPath, `${JSON.stringify(fresh, null, 2)}\n`);
			process.stdout.write(`wrote catalog.json: ${fresh.files.length} files\n`);
		}
	} catch (err) {
		process.stderr.write(`${err instanceof Error ? err.message : String(err)}\n`);
		process.exitCode = 1;
	}
}

if (import.meta.main) await main();
