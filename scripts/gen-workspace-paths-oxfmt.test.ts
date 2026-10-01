import { describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { $ } from "bun";

describe("gen-workspace-paths oxfmt integration", () => {
	test("spawns gen-workspace-paths and verifies oxfmt requirement and formatting", async () => {
		const repoRoot = path.resolve(import.meta.dir, "..");
		const scriptPath = path.join(repoRoot, "scripts/gen-workspace-paths.ts");

		const tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "gen-ws-oxfmt-test-"));
		const emptyDir = await fs.mkdtemp(path.join(os.tmpdir(), "empty-path-"));

		try {
			const tmpPackages = path.join(tmpDir, "packages");
			const tmpPkg = path.join(tmpPackages, "test-pkg");
			await fs.mkdir(tmpPkg, { recursive: true });
			await Bun.write(
				path.join(tmpPkg, "package.json"),
				JSON.stringify({
					name: "@oh-my-pi/test-pkg",
					exports: {
						".": "./src/index.ts",
					},
				}),
			);

			const tmpTsconfig = path.join(tmpDir, "tsconfig.workspace.json");
			const initialConfig = {
				extends: "../tsconfig.base.json",
				compilerOptions: {},
			};
			await Bun.write(tmpTsconfig, `${JSON.stringify(initialConfig, null, "\t")}\n`);

			// 1. env PATH = an empty temp dir: exit code != 0, stderr names oxfmt, (optionally) no formatted output.
			const proc1 = await $`${process.execPath} ${scriptPath} --tsconfig ${tmpTsconfig} --packages ${tmpPackages}`
				.env({ ...process.env, PATH: emptyDir })
				.quiet()
				.nothrow();

			expect(proc1.exitCode).not.toBe(0);
			expect(proc1.stderr.toString()).toContain("oxfmt");

			const contentAfterFail = await Bun.file(tmpTsconfig).text();
			expect(contentAfterFail).not.toContain('"@oh-my-pi/test-pkg"');

			// 2. PATH = `<repo>/node_modules/.bin` + process.env.PATH: exit 0 and the file equals oxfmt's layout, not raw JSON.stringify(…, "\t")
			const nodeModulesBin = path.join(repoRoot, "node_modules/.bin");
			const proc2 = await $`${process.execPath} ${scriptPath} --tsconfig ${tmpTsconfig} --packages ${tmpPackages}`
				.env({ ...process.env, PATH: `${nodeModulesBin}${path.delimiter}${process.env.PATH ?? ""}` })
				.quiet()
				.nothrow();

			expect(proc2.exitCode).toBe(0);

			const formattedContent = await Bun.file(tmpTsconfig).text();
			// oxfmt 0.65 formats single-entry arrays on one line: ["./test-pkg/src/index.ts"]
			expect(formattedContent).toContain('"@oh-my-pi/test-pkg": ["./test-pkg/src/index.ts"]');

			// Raw JSON.stringify(..., "\t") expands the array across multiple lines
			const rawStringified = `${JSON.stringify(
				{
					extends: "../tsconfig.base.json",
					compilerOptions: {
						paths: {
							"@oh-my-pi/test-pkg": ["./test-pkg/src/index.ts"],
						},
					},
				},
				null,
				"\t",
			)}\n`;
			expect(rawStringified).toContain('[\n\t\t\t\t"./test-pkg/src/index.ts"\n\t\t\t]');
			expect(formattedContent).not.toBe(rawStringified);
		} finally {
			await fs.rm(tmpDir, { recursive: true, force: true });
			await fs.rm(emptyDir, { recursive: true, force: true });
		}
	});
});
