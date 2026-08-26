import { afterEach, describe, expect, test } from "bun:test";
import {
	closeSync,
	existsSync,
	cpSync,
	chmodSync,
	fstatSync,
	mkdirSync,
	mkdtempSync,
	openSync,
	readFileSync,
	readSync,
	readdirSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// refresh-natives.sh contract (OMP-157): atomic native addon replacement.
//   * replaces addons by staging in packages/natives/native/ and renaming —
//     never by copying over the loaded file (which replaces mapped text pages
//     and crashed every live process in OMP-156)
//   * a still-open descriptor on the old addon keeps reading the old bytes;
//     the path resolves to a new inode with the new bytes
//   * marker written only after both renames; staging directory cleaned up
//   * a marker-current second run is a no-op printing `natives OK (<ver>)`

const realScript = join(import.meta.dir, "..", "refresh-natives.sh");
const onLinuxX64 = process.platform === "linux" && process.arch === "x64";
const tempDirs: string[] = [];

afterEach(() => {
	for (const dir of tempDirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

const VERSION = "9.9.9";

interface Fixture {
	repo: string;
	nativeDir: string;
	modernPath: string;
	baselinePath: string;
	markerPath: string;
	run: () => { exitCode: number; stdout: string; stderr: string };
}

function makeFixture(options: { omitBaseline?: boolean } = {}): Fixture {
	const root = mkdtempSync(join(tmpdir(), "omp-natives-test-"));
	tempDirs.push(root);
	const repo = join(root, "repo");
	const binDir = join(root, "bin");
	const nativeDir = join(repo, "packages", "natives", "native");
	mkdirSync(join(repo, "session-system"), { recursive: true });
	mkdirSync(nativeDir, { recursive: true });
	mkdirSync(binDir, { recursive: true });
	cpSync(realScript, join(repo, "session-system", "refresh-natives.sh"));
	writeFileSync(
		join(repo, "packages", "natives", "package.json"),
		JSON.stringify({ name: "@oh-my-pi/pi-natives", version: VERSION }),
	);
	const modernPath = join(nativeDir, "pi_natives.linux-x64-modern.node");
	const baselinePath = join(nativeDir, "pi_natives.linux-x64-baseline.node");
	writeFileSync(modernPath, "OLD-MODERN");
	writeFileSync(baselinePath, "OLD-BASELINE");

	// Locally built package tarball — the fake npm serves it; no network.
	const pkgDir = join(root, "pkg", "package");
	mkdirSync(pkgDir, { recursive: true });
	writeFileSync(join(pkgDir, "pi_natives.linux-x64-modern.node"), "NEW-MODERN");
	if (!options.omitBaseline) writeFileSync(join(pkgDir, "pi_natives.linux-x64-baseline.node"), "NEW-BASELINE");
	const tarball = join(root, "natives.tgz");
	const tar = Bun.spawnSync(["tar", "czf", tarball, "-C", join(root, "pkg"), "package"]);
	if (tar.exitCode !== 0) throw new Error(`tar: ${tar.stderr.toString()}`);

	const npmStub = join(binDir, "npm");
	writeFileSync(
		npmStub,
		`#!/bin/sh
if [ "$1" != "pack" ] || [ "$2" != "@oh-my-pi/pi-natives-linux-x64@${VERSION}" ]; then
	echo "fake npm: unexpected args: $*" >&2
	exit 1
fi
cp "$FAKE_TARBALL" ./oh-my-pi-pi-natives-linux-x64-${VERSION}.tgz
`,
	);
	chmodSync(npmStub, 0o755);

	const run = () => {
		const proc = Bun.spawnSync(["bash", "session-system/refresh-natives.sh"], {
			cwd: repo,
			env: {
				...process.env,
				PATH: `${binDir}:${process.env.PATH ?? ""}`,
				FAKE_TARBALL: tarball,
			} as Record<string, string>,
			stdout: "pipe",
			stderr: "pipe",
		});
		return { exitCode: proc.exitCode, stdout: proc.stdout.toString(), stderr: proc.stderr.toString() };
	};
	return {
		repo,
		nativeDir,
		modernPath,
		baselinePath,
		markerPath: join(repo, "packages", "natives", "npm", ".native-dropin-version"),
		run,
	};
}

describe("refresh-natives.sh atomic replacement", () => {
	test.skipIf(!onLinuxX64)(
		"renames new inodes into place while an open descriptor keeps the old bytes",
		() => {
			const fx = makeFixture();
			const fd = openSync(fx.modernPath, "r");
			try {
				const oldIno = fstatSync(fd).ino;

				const first = fx.run();
				expect(first.exitCode, first.stderr).toBe(0);
				expect(first.stdout).toContain(`natives refreshed to ${VERSION}`);

				// path: new bytes, new inode
				expect(readFileSync(fx.modernPath, "utf8")).toBe("NEW-MODERN");
				const newIno = statSync(fx.modernPath).ino;
				expect(newIno).not.toBe(oldIno);

				// still-open descriptor: old inode, old bytes — mapped code survives
				expect(fstatSync(fd).ino).toBe(oldIno);
				const buf = Buffer.alloc("OLD-MODERN".length);
				readSync(fd, buf, 0, buf.length, 0);
				expect(buf.toString()).toBe("OLD-MODERN");

				// both variants present; marker written; staging cleaned up
				expect(readFileSync(fx.baselinePath, "utf8")).toBe("NEW-BASELINE");
				expect(readFileSync(fx.markerPath, "utf8").trim()).toBe(VERSION);
				const leftovers = readdirSync(fx.nativeDir).filter(name => name.startsWith(".refresh-natives."));
				expect(leftovers).toEqual([]);

				// marker-current second run is a no-op on the same inode
				const second = fx.run();
				expect(second.exitCode, second.stderr).toBe(0);
				expect(second.stdout).toContain(`natives OK (${VERSION})`);
				expect(statSync(fx.modernPath).ino).toBe(newIno);
			} finally {
				closeSync(fd);
			}
		},
	);
});

describe("refresh-natives.sh partial-package refusal", () => {
	test.skipIf(!onLinuxX64)("a tarball missing a variant renames nothing and writes no marker", () => {
		const fx = makeFixture({ omitBaseline: true });
		const result = fx.run();
		expect(result.exitCode).toBe(1);
		expect(result.stderr).toContain("missing pi_natives.linux-x64-baseline.node");

		// destination untouched — no mixed-version install gets blessed
		expect(readFileSync(fx.modernPath, "utf8")).toBe("OLD-MODERN");
		expect(readFileSync(fx.baselinePath, "utf8")).toBe("OLD-BASELINE");
		expect(existsSync(fx.markerPath)).toBe(false);
		const leftovers = readdirSync(fx.nativeDir).filter(name => name.startsWith(".refresh-natives."));
		expect(leftovers).toEqual([]);

		// next run retries instead of printing `natives OK`
		const retry = fx.run();
		expect(retry.exitCode).toBe(1);
		expect(retry.stdout).not.toContain("natives OK");
	});
});
