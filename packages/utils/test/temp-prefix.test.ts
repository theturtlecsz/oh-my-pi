import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TempDir } from "../src/temp";

const RELATIVE_PREFIX = "omp-prefix-relative-";

function expectUnderTemp(dirPath: string, namePrefix: string): void {
	expect(path.dirname(dirPath)).toBe(os.tmpdir());
	expect(path.basename(dirPath).startsWith(namePrefix)).toBe(true);
	expect(fs.existsSync(dirPath)).toBe(true);
}

function expectNoRelativePrefixInCwd(): void {
	const names = fs.readdirSync(process.cwd()).filter(name => name.startsWith(RELATIVE_PREFIX));
	expect(names).toEqual([]);
}

function expectRelativePrefixError(error: unknown): void {
	expect(error).toBeInstanceOf(Error);
	const message = error instanceof Error ? error.message : "";
	expect(message).toContain(RELATIVE_PREFIX);
	expect(message).toContain("@");
	expect(message).toContain("absolute path");
}

describe("TempDir prefix", () => {
	test("createSync places omitted and @ prefixes in the system temp directory", () => {
		const created: TempDir[] = [];
		try {
			const omitted = TempDir.createSync();
			created.push(omitted);
			const guarded = TempDir.createSync("@omp-prefix-guard-");
			created.push(guarded);
			const absolutePrefix = path.join(os.tmpdir(), "omp-prefix-abs-");
			const absolute = TempDir.createSync(absolutePrefix);
			created.push(absolute);

			expectUnderTemp(omitted.path(), "pi-temp-");
			expectUnderTemp(guarded.path(), "omp-prefix-guard-");
			expect(path.isAbsolute(absolute.path())).toBe(true);
			expect(absolute.path().startsWith(absolutePrefix)).toBe(true);
			expect(fs.existsSync(absolute.path())).toBe(true);
		} finally {
			for (const dir of created) dir.removeSync();
		}
	});

	test("create places omitted and @ prefixes in the system temp directory", async () => {
		const created: TempDir[] = [];
		try {
			const omitted = await TempDir.create();
			created.push(omitted);
			const guarded = await TempDir.create("@omp-prefix-guard-");
			created.push(guarded);
			const absolutePrefix = path.join(os.tmpdir(), "omp-prefix-abs-");
			const absolute = await TempDir.create(absolutePrefix);
			created.push(absolute);

			expectUnderTemp(omitted.path(), "pi-temp-");
			expectUnderTemp(guarded.path(), "omp-prefix-guard-");
			expect(path.isAbsolute(absolute.path())).toBe(true);
			expect(absolute.path().startsWith(absolutePrefix)).toBe(true);
			expect(fs.existsSync(absolute.path())).toBe(true);
		} finally {
			for (const dir of created) await dir.remove();
		}
	});

	test("createSync rejects a relative prefix before it creates a directory", () => {
		let caught: unknown;
		try {
			TempDir.createSync(RELATIVE_PREFIX);
		} catch (error) {
			caught = error;
		}
		expectRelativePrefixError(caught);
		expectNoRelativePrefixInCwd();
	});

	test("create rejects a relative prefix before it creates a directory", async () => {
		let caught: unknown;
		try {
			await TempDir.create(RELATIVE_PREFIX);
		} catch (error) {
			caught = error;
		}
		expectRelativePrefixError(caught);
		expectNoRelativePrefixInCwd();
	});
});
