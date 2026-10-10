import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ECC_ROOT } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { lockPathFor } from "../ecc/adapter/lock";
import { PinError, verifyBuildLock, writeBuildLock } from "../ecc/adapter/pin";
import { ReviewError, validateReview } from "../ecc/adapter/review";
import type { BuildLock, UpdateReview } from "../ecc/adapter/types";
import manifestData from "../ecc/manifest.json";

const MANIFEST_UPSTREAM = (manifestData as { upstream: UpdateReview["candidate"] }).upstream;

const repoRoot = path.resolve(import.meta.dir, "../..");
const emitLock = path.join(repoRoot, "session-system/ecc/adapter/emit-lock.ts");

const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true });
});

async function makeEccCopy(): Promise<string> {
	const parent = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-pin-"));
	tempDirs.push(parent);
	const root = path.join(parent, "ecc");
	await fs.cp(ECC_ROOT, root, { recursive: true });
	return root;
}

function validReview(overrides: Partial<UpdateReview> = {}): UpdateReview {
	return {
		schemaVersion: 1,
		baseCommit: "1".repeat(40),
		candidate: {
			commit: MANIFEST_UPSTREAM.commit,
			tree: MANIFEST_UPSTREAM.tree,
			version: MANIFEST_UPSTREAM.version,
			describe: MANIFEST_UPSTREAM.describe,
		},
		changes: [
			{
				assetId: "ecc-common-coding-style",
				path: ".omp/rules/ecc-common-coding-style.md",
				fromSha256: "a".repeat(64),
				toSha256: "b".repeat(64),
			},
		],
		newFiles: [".omp/rules/ecc-new.md"],
		reviewer: "owner",
		reviewedAt: "2026-10-02T00:00:00Z",
		decision: "accept",
		notes: "reviewed",
		...overrides,
	};
}

async function writeReviewFile(eccRoot: string, relPath: string, review: unknown): Promise<string> {
	const full = path.join(eccRoot, relPath);
	await fs.mkdir(path.dirname(full), { recursive: true });
	await Bun.write(full, `${JSON.stringify(review, null, 2)}\n`);
	return sha256Hex(new Uint8Array(await Bun.file(full).arrayBuffer()));
}

describe("committed build lock pins the manifest", () => {
	test("the committed manifestSha256 equals sha256 of the committed manifest.json", async () => {
		const manifestBytes = new Uint8Array(await Bun.file(path.join(ECC_ROOT, "manifest.json")).arrayBuffer());
		const lock = JSON.parse(await Bun.file(lockPathFor(ECC_ROOT)).text()) as BuildLock;
		expect(lock.manifestSha256).toBe(sha256Hex(manifestBytes));
		expect(lock.review).toBeNull();
	});

	test("verifyBuildLock passes for the committed ECC_ROOT", async () => {
		await expect(verifyBuildLock(ECC_ROOT)).resolves.toBeUndefined();
	});

	test("one byte appended to a temp manifest.json throws naming both hashes", async () => {
		const root = await makeEccCopy();
		const lock = JSON.parse(await Bun.file(lockPathFor(root)).text()) as BuildLock;
		const manifestPath = path.join(root, "manifest.json");
		await Bun.write(manifestPath, `${await Bun.file(manifestPath).text()} `);

		const err = await verifyBuildLock(root).catch((e: unknown) => e);
		expect(err).toBeInstanceOf(PinError);
		const message = (err as Error).message;
		expect(message).toContain(lock.manifestSha256);
		expect(message).toContain(sha256Hex(new Uint8Array(await Bun.file(manifestPath).arrayBuffer())));
	});
});

describe("build lock review pin", () => {
	test("a valid accepted review re-verifies against its file", async () => {
		const root = await makeEccCopy();
		const sha256 = await writeReviewFile(root, "reviews/update.json", validReview());
		await writeBuildLock(root, { path: "reviews/update.json", sha256 });
		await expect(verifyBuildLock(root)).resolves.toBeUndefined();
	});

	test("a review pin pointing at a missing file throws", async () => {
		const root = await makeEccCopy();
		await writeBuildLock(root, { path: "reviews/missing.json", sha256: "c".repeat(64) });
		await expect(verifyBuildLock(root)).rejects.toThrow(/reviews\/missing\.json.*is missing/);
	});

	test("a tampered review file throws with both hashes", async () => {
		const root = await makeEccCopy();
		const sha256 = await writeReviewFile(root, "reviews/update.json", validReview());
		await writeBuildLock(root, { path: "reviews/update.json", sha256 });
		await Bun.write(
			path.join(root, "reviews/update.json"),
			`${JSON.stringify(validReview({ notes: "tampered" }))}\n`,
		);

		const err = await verifyBuildLock(root).catch((e: unknown) => e);
		expect(err).toBeInstanceOf(PinError);
		expect((err as Error).message).toContain(sha256);
	});

	test("a rejected review record throws", async () => {
		const root = await makeEccCopy();
		const sha256 = await writeReviewFile(root, "reviews/update.json", validReview({ decision: "reject" }));
		await writeBuildLock(root, { path: "reviews/update.json", sha256 });
		await expect(verifyBuildLock(root)).rejects.toThrow(/is not accepted/);
	});

	test("a review naming another candidate commit throws", async () => {
		const root = await makeEccCopy();
		const review = validReview({ candidate: { ...validReview().candidate, commit: "9".repeat(40) } });
		const sha256 = await writeReviewFile(root, "reviews/update.json", review);
		await writeBuildLock(root, { path: "reviews/update.json", sha256 });
		await expect(verifyBuildLock(root)).rejects.toThrow(/names candidate 9{40}/);
	});

	test("a review escaping the ECC root is refused", async () => {
		const root = await makeEccCopy();
		await writeBuildLock(root, { path: "../outside.json", sha256: "d".repeat(64) });
		await expect(verifyBuildLock(root)).rejects.toThrow(/escapes the ECC root/);
	});

	test("a review file that is invalid JSON under a matching pin throws PinError", async () => {
		const root = await makeEccCopy();
		const reviewPath = path.join(root, "reviews/broken.json");
		await fs.mkdir(path.dirname(reviewPath), { recursive: true });
		await Bun.write(reviewPath, "{ not json");
		const sha256 = sha256Hex(new Uint8Array(await Bun.file(reviewPath).arrayBuffer()));
		await writeBuildLock(root, { path: "reviews/broken.json", sha256 });
		await expect(verifyBuildLock(root)).rejects.toThrow(PinError);
	});
});

describe("validateReview", () => {
	test("accepts a full record and lowercases its hashes", () => {
		const review = validateReview(validReview({ baseCommit: "A".repeat(40) }));
		expect(review.baseCommit).toBe("a".repeat(40));
		expect(review.decision).toBe("accept");
		expect(review.changes).toHaveLength(1);
	});

	test("rejects an unknown key at the top level", () => {
		expect(() => validateReview({ ...validReview(), surprise: true })).toThrow(ReviewError);
		expect(() => validateReview({ ...validReview(), surprise: true })).toThrow(/unknown key 'surprise'/);
	});

	test("rejects a short commit and a non-hex hash", () => {
		expect(() => validateReview(validReview({ baseCommit: "abc" }))).toThrow(/baseCommit must be a 40-character hex/);
		expect(() => validateReview(validReview({ candidate: { ...validReview().candidate, commit: "zz" } }))).toThrow(
			/candidate\.commit must be a 40-character hex/,
		);
		expect(() =>
			validateReview({
				...validReview(),
				changes: [{ ...validReview().changes[0], toSha256: "nothex" }],
			}),
		).toThrow(/toSha256 must be a 64-character hex/);
	});

	test("rejects an empty reviewer and a bad decision", () => {
		expect(() => validateReview(validReview({ reviewer: "   " }))).toThrow(/reviewer must be a non-empty string/);
		expect(() => validateReview({ ...validReview(), decision: "maybe" })).toThrow(
			/decision must be 'accept' or 'reject'/,
		);
	});

	test("rejects an unknown key inside the candidate and a missing top-level key", () => {
		expect(() => validateReview({ ...validReview(), candidate: { ...validReview().candidate, extra: 1 } })).toThrow(
			/candidate has unknown key 'extra'/,
		);
		const { notes: _drop, ...withoutNotes } = validReview();
		expect(() => validateReview(withoutNotes)).toThrow(/missing required key 'notes'/);
	});
});

describe("emit-lock --check", () => {
	test("exits 0 against the committed lock", async () => {
		const proc = Bun.spawn(["bun", emitLock, "--check"], {
			cwd: repoRoot,
			stdin: "ignore",
			stdout: "pipe",
			stderr: "pipe",
		});
		const [stdout, stderr, code] = await Promise.all([
			new Response(proc.stdout).text(),
			new Response(proc.stderr).text(),
			proc.exited,
		]);
		expect(code).toBe(0);
		expect(stderr).toBe("");
		expect(stdout).toContain("verified");
	}, 30_000);

	test("exits 1 with the error when a manifest pin is stale", async () => {
		const root = await makeEccCopy();
		const manifestPath = path.join(root, "manifest.json");
		await Bun.write(manifestPath, `${await Bun.file(manifestPath).text()} `);
		const proc = Bun.spawn(["bun", emitLock, "--ecc-root", root, "--check"], {
			cwd: repoRoot,
			stdin: "ignore",
			stdout: "pipe",
			stderr: "pipe",
		});
		const [stdout, stderr, code] = await Promise.all([
			new Response(proc.stdout).text(),
			new Response(proc.stderr).text(),
			proc.exited,
		]);
		expect(code).toBe(1);
		expect(stdout).toBe("");
		expect(stderr).toContain("manifest sha256 mismatch");
	}, 30_000);
});
