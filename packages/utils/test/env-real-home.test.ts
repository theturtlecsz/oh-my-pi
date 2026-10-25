import { afterEach, describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { readHomeDotenvFile } from "@oh-my-pi/pi-utils/env";

const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) {
		await fs.promises.rm(dir, { force: true, recursive: true });
	}
});

async function createHomes() {
	const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), "pi-utils-real-home-"));
	tempDirs.push(root);
	const simulatedRealHome = path.join(root, "simulatedRealHome");
	const fixtureHome = path.join(root, "fixtureHome");
	const relativePaths = [".env", path.join(".omp", ".env"), path.join(".omp", "agent", ".env")];
	const realFiles = relativePaths.map((relativePath, index) => ({
		filePath: path.join(simulatedRealHome, relativePath),
		keys: [`REAL_HOME_DOTENV_${index}`],
	}));
	const fixtureFiles = relativePaths.map((relativePath, index) => ({
		filePath: path.join(fixtureHome, relativePath),
		keys: [`FIXTURE_HOME_DOTENV_${index}`],
	}));
	for (const file of [...realFiles, ...fixtureFiles]) {
		await Bun.write(file.filePath, file.keys.map(key => `${key}=dummy-m1g-value`).join("\n"));
	}
	return { root, simulatedRealHome, fixtureHome, realFiles, fixtureFiles };
}

describe("readHomeDotenvFile real home boundary", () => {
	it("skips all three dotenv locations inside the real home in test mode", async () => {
		const { simulatedRealHome, realFiles } = await createHomes();
		for (const file of realFiles) {
			expect(
				Object.keys(readHomeDotenvFile(file.filePath, { realHomeRoot: simulatedRealHome, testMode: true })).sort(),
			).toEqual([]);
		}
	});

	it("loads all three fixture dotenv locations outside the real home in test mode", async () => {
		const { simulatedRealHome, fixtureFiles } = await createHomes();
		for (const file of fixtureFiles) {
			expect(
				Object.keys(readHomeDotenvFile(file.filePath, { realHomeRoot: simulatedRealHome, testMode: true })).sort(),
			).toEqual(file.keys.toSorted());
		}
	});

	it("loads real-home and fixture dotenv files outside test mode", async () => {
		const { simulatedRealHome, realFiles, fixtureFiles } = await createHomes();
		for (const file of [...realFiles, ...fixtureFiles]) {
			expect(
				Object.keys(readHomeDotenvFile(file.filePath, { realHomeRoot: simulatedRealHome, testMode: false })).sort(),
			).toEqual(file.keys.toSorted());
		}
	});

	it("skips a fixture file symlink targeting the real home in test mode", async () => {
		const { simulatedRealHome, fixtureHome, realFiles } = await createHomes();
		const linkedFile = path.join(fixtureHome, "linked.env");
		await fs.promises.symlink(realFiles[0].filePath, linkedFile, "file");
		expect(
			Object.keys(readHomeDotenvFile(linkedFile, { realHomeRoot: simulatedRealHome, testMode: true })).sort(),
		).toEqual([]);
	});

	it("skips a fixture directory symlink targeting the real home in test mode", async () => {
		const { simulatedRealHome, fixtureHome } = await createHomes();
		const linkedDir = path.join(fixtureHome, "linked-omp");
		await fs.promises.symlink(path.join(simulatedRealHome, ".omp"), linkedDir, "dir");
		expect(
			Object.keys(
				readHomeDotenvFile(path.join(linkedDir, ".env"), { realHomeRoot: simulatedRealHome, testMode: true }),
			).sort(),
		).toEqual([]);
	});

	it("loads file and directory symlinks targeting the real home outside test mode", async () => {
		const { simulatedRealHome, fixtureHome, realFiles } = await createHomes();
		const linkedFile = path.join(fixtureHome, "linked.env");
		const linkedDir = path.join(fixtureHome, "linked-omp");
		await fs.promises.symlink(realFiles[0].filePath, linkedFile, "file");
		await fs.promises.symlink(path.join(simulatedRealHome, ".omp"), linkedDir, "dir");
		expect(
			Object.keys(readHomeDotenvFile(linkedFile, { realHomeRoot: simulatedRealHome, testMode: false })).sort(),
		).toEqual(realFiles[0].keys.toSorted());
		expect(
			Object.keys(
				readHomeDotenvFile(path.join(linkedDir, ".env"), { realHomeRoot: simulatedRealHome, testMode: false }),
			).sort(),
		).toEqual(realFiles[1].keys.toSorted());
	});

	it("resolves a symlinked real-home root before skipping its dotenv file in test mode", async () => {
		const { root, simulatedRealHome, realFiles } = await createHomes();
		const linkedHome = path.join(root, "linked-home");
		await fs.promises.symlink(simulatedRealHome, linkedHome, "dir");
		expect(
			Object.keys(readHomeDotenvFile(realFiles[0].filePath, { realHomeRoot: linkedHome, testMode: true })).sort(),
		).toEqual([]);
	});

	it("loads a sibling dotenv file whose directory shares the real-home name prefix in test mode", async () => {
		const { simulatedRealHome } = await createHomes();
		const siblingFile = path.join(`${simulatedRealHome}-sibling`, ".env");
		await Bun.write(siblingFile, "SIBLING_HOME_DOTENV=dummy-m1g-value\n");
		expect(
			Object.keys(readHomeDotenvFile(siblingFile, { realHomeRoot: simulatedRealHome, testMode: true })).sort(),
		).toEqual(["SIBLING_HOME_DOTENV"]);
	});
});

describe("default real home root in a child process", () => {
	const expectedKeys = [
		"HOME_DOTENV_PROBE_REAL_HOME",
		"PIHOME_DOTENV_PROBE_REAL_HOME",
		"AGENT_DOTENV_PROBE_REAL_HOME",
	];

	async function probeFixtureHome() {
		const fixtureHome = await fs.promises.mkdtemp(path.join(os.tmpdir(), "omp-m1g-probe-home-"));
		tempDirs.push(fixtureHome);
		const cwd = await fs.promises.mkdtemp(path.join(os.tmpdir(), "omp-m1g-probe-cwd-"));
		tempDirs.push(cwd);
		await Bun.write(path.join(fixtureHome, ".env"), `${expectedKeys[0]}=dummy-m1g-value\n`);
		await Bun.write(path.join(fixtureHome, ".omp", ".env"), `${expectedKeys[1]}=dummy-m1g-value\n`);
		await Bun.write(path.join(fixtureHome, ".omp", "agent", ".env"), `${expectedKeys[2]}=dummy-m1g-value\n`);
		const proc = Bun.spawn([process.execPath, path.join(import.meta.dir, "fixtures", "real-home-probe.ts")], {
			cwd,
			env: { PATH: process.env.PATH, HOME: fixtureHome, NODE_ENV: "test" },
			stdout: "pipe",
			stderr: "ignore",
			timeout: 10_000,
		});
		const [stdout, exitCode] = await Promise.all([new Response(proc.stdout).text(), proc.exited]);
		expect(exitCode).toBe(0);
		const result: { keys: string[]; passwdHome: string | null } = JSON.parse(stdout);
		return { ...result, fixtureHome };
	}

	it.skipIf(process.platform === "win32")(
		"loads fixture HOME dotenv files with the default passwd root",
		async () => {
			const { keys } = await probeFixtureHome();
			expect(keys).toEqual(expectedKeys.toSorted());
		},
		15_000,
	);

	it.skipIf(process.getuid === undefined || Bun.which("getent") === null)(
		"resolves the default root from the passwd entry, not HOME",
		async () => {
			const passwd = Bun.spawnSync(["getent", "passwd", String(process.getuid?.())], {
				stdout: "pipe",
				stderr: "ignore",
				timeout: 10_000,
			});
			expect(passwd.exitCode).toBe(0);
			const expectedHome = passwd.stdout.toString().trim().split(":")[5];
			const { passwdHome, fixtureHome } = await probeFixtureHome();
			expect(passwdHome).toBe(expectedHome);
			expect(passwdHome).not.toBe(fixtureHome);
		},
		25_000,
	);
});
