import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { gitTreeId } from "../ecc/adapter/tree-hash";

const eccDir = path.resolve(import.meta.dir, "../ecc");
const mirrorDir = path.join(eccDir, "mirror");

describe("ecc mirror tree", () => {
	let tmp: string | undefined;
	afterEach(async () => {
		if (tmp) await fs.rm(tmp, { recursive: true, force: true });
		tmp = undefined;
	});

	async function copyMirror(): Promise<string> {
		tmp = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-mirror-"));
		const dest = path.join(tmp, "mirror");
		await fs.cp(mirrorDir, dest, { recursive: true });
		return dest;
	}

	it("equals the manifest upstream tree", async () => {
		const manifest = await Bun.file(path.join(eccDir, "manifest.json")).json();
		// The upstream pin a7489fb4… covers all 3716 vendored files. The retired
		// database-reviewer agent (agents/database-reviewer.md) is deleted from the
		// fork, so the mirror tree is that pinned tree minus the one removed path.
		expect(manifest.upstream.tree).toBe("a7489fb4da00fc7b4995df3a3c59c018d08a3807");
		expect(await gitTreeId(mirrorDir)).toBe("4524dc0542aa4a7062669ce185848ea94c0e4050");
	});

	it("changes on byte edit, exec bit, and new file", async () => {
		const expected = await gitTreeId(mirrorDir);
		const target = "rules/common/coding-style.md";

		const edited = await copyMirror();
		await fs.appendFile(path.join(edited, target), "x");
		expect(await gitTreeId(edited)).not.toBe(expected);
		await fs.rm(path.dirname(edited), { recursive: true, force: true });

		const chmodded = await copyMirror();
		await fs.chmod(path.join(chmodded, target), 0o755);
		expect(await gitTreeId(chmodded)).not.toBe(expected);
		await fs.rm(path.dirname(chmodded), { recursive: true, force: true });

		const added = await copyMirror();
		await Bun.write(path.join(added, "NEW.md"), "new\n");
		expect(await gitTreeId(added)).not.toBe(expected);
	});
});
