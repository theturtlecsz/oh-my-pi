import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { getPasswdHomeDir, readHomeDotenvFile } from "@oh-my-pi/pi-utils/env";

const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) {
		await fs.rm(dir, { force: true, recursive: true });
	}
});

async function createRoot(): Promise<string> {
	const root = await fs.mkdtemp(path.join(os.tmpdir(), "omp-m1g-passwd-"));
	tempDirs.push(root);
	return root;
}

describe("getPasswdHomeDir", () => {
	it("returns the home for the matching uid", async () => {
		const passwdPath = path.join(await createRoot(), "passwd");
		await Bun.write(
			passwdPath,
			"other:x:123:123::/other/home:/bin/sh\nfixture:x:424242:123::/fixture/home:/bin/sh\n",
		);
		expect(getPasswdHomeDir({ passwdPath, uid: 424242 })).toBe("/fixture/home");
	});

	it("returns undefined when no uid matches", async () => {
		const passwdPath = path.join(await createRoot(), "passwd");
		await Bun.write(passwdPath, "fixture:x:123:123::/fixture/home:/bin/sh\n");
		expect(getPasswdHomeDir({ passwdPath, uid: 424242 })).toBeUndefined();
	});

	it("returns undefined when the passwd file is missing", async () => {
		const passwdPath = path.join(await createRoot(), "missing-passwd");
		expect(getPasswdHomeDir({ passwdPath, uid: 424242 })).toBeUndefined();
	});

	it("ignores comment lines and rejects a relative home", async () => {
		const passwdPath = path.join(await createRoot(), "passwd");
		await Bun.write(passwdPath, "#fixture:x:424242:123::/fixture/home:/bin/sh\n\n");
		expect(getPasswdHomeDir({ passwdPath, uid: 424242 })).toBeUndefined();
		await Bun.write(passwdPath, "fixture:x:424242:123::relative/home:/bin/sh\n");
		expect(getPasswdHomeDir({ passwdPath, uid: 424242 })).toBeUndefined();
	});
});

describe("readHomeDotenvFile passwd root", () => {
	it("skips fixture dotenv files when the passwd root cannot be read", async () => {
		const root = await createRoot();
		const filePath = path.join(root, "fixture", ".env");
		await Bun.write(filePath, "FIXTURE_DOTENV_PROBE_PASSWD=dummy-m1g-value\n");
		const options = { testMode: true, passwdPath: path.join(root, "missing-passwd") };
		expect(Object.keys(readHomeDotenvFile(filePath, options))).toEqual([]);
	});

	it.skipIf(process.getuid === undefined)(
		"loads fixtures outside the passwd home and skips files inside",
		async () => {
			const root = await createRoot();
			const simulatedRealHome = path.join(root, "simulated-real-home");
			const passwdPath = path.join(root, "passwd");
			await Bun.write(passwdPath, `fixture:x:${process.getuid?.()}:123::${simulatedRealHome}:/bin/sh\n`);
			const outsideFile = path.join(root, "fixture", ".env");
			const insideFile = path.join(simulatedRealHome, ".env");
			await Bun.write(outsideFile, "FIXTURE_DOTENV_PROBE_PASSWD=dummy-m1g-value\n");
			await Bun.write(insideFile, "REAL_HOME_DOTENV_PROBE_PASSWD=dummy-m1g-value\n");
			const options = { testMode: true, passwdPath };
			expect(Object.keys(readHomeDotenvFile(outsideFile, options))).toEqual(["FIXTURE_DOTENV_PROBE_PASSWD"]);
			expect(Object.keys(readHomeDotenvFile(insideFile, options))).toEqual([]);
		},
	);
});
