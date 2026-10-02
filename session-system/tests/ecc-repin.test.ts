import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { install } from "../ecc/adapter/apply";
import { assetsForPack, ECC_ROOT, loadManifest } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { lockPathFor } from "../ecc/adapter/lock";
import { verifyBuildLock } from "../ecc/adapter/pin";
import { RepinError, repin } from "../ecc/adapter/repin";
import type { BuildLock, UpdateReview } from "../ecc/adapter/types";
import { type UpstreamCheckResult, checkUpstream } from "../ecc/adapter/upstream-check";

const repoRoot = path.resolve(import.meta.dir, "../..");
const repinCli = path.join(repoRoot, "session-system/ecc/adapter/repin.ts");
const mirrorRoot = path.join(ECC_ROOT, "mirror");

const CANDIDATE_COMMIT = "c".repeat(40);
const CANDIDATE_TREE = "d".repeat(40);
const EDIT_MARKER = "<!-- repin test edit -->\n";

const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
});

async function makeTempDir(prefix: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

async function makeEccCopy(): Promise<string> {
	const parent = await makeTempDir("ecc-repin-");
	const root = path.join(parent, "ecc");
	await fs.cp(ECC_ROOT, root, { recursive: true });
	return root;
}

async function makeCandidate(edit?: string): Promise<string> {
	const dir = await makeTempDir("ecc-repin-candidate-");
	await fs.cp(mirrorRoot, dir, { recursive: true });
	if (edit) {
		const target = path.join(dir, edit);
		await Bun.write(target, `${await Bun.file(target).text()}\n${EDIT_MARKER}`);
	}
	return dir;
}

function makeAcceptedReview(report: UpstreamCheckResult): UpdateReview {
	return {
		schemaVersion: 1,
		baseCommit: report.baseCommit,
		candidate: {
			commit: CANDIDATE_COMMIT,
			tree: CANDIDATE_TREE,
			version: "2.2.2",
			describe: "v2.2.2-1-gcccccccc",
		},
		changes: report.files
			.filter(file => file.status === "changed")
			.map(file => ({
				assetId: file.assetId,
				path: file.path,
				fromSha256: file.fromSha256,
				toSha256: file.toSha256 as string,
			})),
		newFiles: [...report.newFiles],
		reviewer: "owner",
		reviewedAt: "2026-10-02T00:00:00Z",
		decision: "accept",
		notes: "reviewed upstream change",
	};
}

async function writeReview(eccRoot: string, review: unknown): Promise<string> {
	const full = path.join(eccRoot, "reviews", "update.json");
	await fs.mkdir(path.dirname(full), { recursive: true });
	await Bun.write(full, `${JSON.stringify(review, null, 2)}\n`);
	return full;
}

async function snapshot(dir: string): Promise<Map<string, string>> {
	const files = new Map<string, string>();
	const walk = async (current: string): Promise<void> => {
		for (const entry of await fs.readdir(current, { withFileTypes: true }).catch(() => [])) {
			const full = path.join(current, entry.name);
			if (entry.isDirectory()) await walk(full);
			else if (entry.isFile()) {
				const bytes = new Uint8Array(await Bun.file(full).arrayBuffer());
				files.set(path.relative(dir, full), sha256Hex(bytes));
			}
		}
	};
	await walk(dir);
	return files;
}

async function expectRefusalUnchanged(eccRoot: string, run: () => Promise<unknown>): Promise<Error> {
	const before = await snapshot(eccRoot);
	const err = await run().then(
		() => null,
		(e: unknown) => e,
	);
	expect(err).toBeInstanceOf(RepinError);
	expect(await snapshot(eccRoot)).toEqual(before);
	return err as Error;
}

async function runCli(args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
	const proc = Bun.spawn(["bun", repinCli, ...args], {
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
	return { code, stdout, stderr };
}

async function acceptedFixture(): Promise<{
	eccRoot: string;
	candidateRoot: string;
	reviewPath: string;
	report: UpstreamCheckResult;
}> {
	const eccRoot = await makeEccCopy();
	const candidateRoot = await makeCandidate("skills/research-ops/SKILL.md");
	const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
	const reviewPath = await writeReview(eccRoot, makeAcceptedReview(report));
	return { eccRoot, candidateRoot, reviewPath, report };
}

describe("repin applies an accepted review", () => {
	test("re-pins the manifest, mirror, review, and lock and installs the new bytes", async () => {
		const { eccRoot, candidateRoot, reviewPath, report } = await acceptedFixture();

		const fromManifestSha256 = sha256Hex(
			new Uint8Array(await Bun.file(path.join(eccRoot, "manifest.json")).arrayBuffer()),
		);
		const newResearchBytes = new Uint8Array(
			await Bun.file(path.join(candidateRoot, "skills/research-ops/SKILL.md")).arrayBuffer(),
		);
		const expectedResearchSha = sha256Hex(newResearchBytes);

		const result = await repin({ eccRoot, candidateRoot, review: reviewPath });

		expect(result.candidateCommit).toBe(CANDIDATE_COMMIT);
		expect(result.changedFiles).toEqual(["skills/research-ops/SKILL.md"]);
		expect(result.fromManifestSha256).toBe(fromManifestSha256);
		expect(result.toManifestSha256).not.toBe(result.fromManifestSha256);

		const manifestText = await Bun.file(path.join(eccRoot, "manifest.json")).text();
		expect(result.toManifestSha256).toBe(sha256Hex(manifestText));
		const manifest = await loadManifest(eccRoot);
		expect(manifest.upstream.commit).toBe(CANDIDATE_COMMIT);
		const research = manifest.assets.find(asset => asset.id === "ecc-research-ops");
		expect(research?.upstream.sha256).toBe(expectedResearchSha);
		expect(research?.upstream.commit).toBe(CANDIDATE_COMMIT);

		// mirror carries the candidate bytes
		const mirrorBytes = new Uint8Array(
			await Bun.file(path.join(eccRoot, "mirror", "skills/research-ops/SKILL.md")).arrayBuffer(),
		);
		expect(sha256Hex(mirrorBytes)).toBe(expectedResearchSha);

		// review copied under reviews/<commit>.json and pinned
		const reviewCopy = await Bun.file(path.join(eccRoot, "reviews", `${CANDIDATE_COMMIT}.json`)).arrayBuffer();
		const lock = JSON.parse(await Bun.file(lockPathFor(eccRoot)).text()) as BuildLock;
		expect(lock.manifestSha256).toBe(result.toManifestSha256);
		expect(lock.upstream.commit).toBe(CANDIDATE_COMMIT);
		expect(lock.review).toEqual({
			path: `reviews/${CANDIDATE_COMMIT}.json`,
			sha256: sha256Hex(new Uint8Array(reviewCopy)),
		});
		await expect(verifyBuildLock(eccRoot)).resolves.toBeUndefined();

		// the research pack install writes the new adapted bytes
		const projectRoot = await makeTempDir("ecc-repin-project-");
		const loaded = await loadManifest(eccRoot);
		await install({ eccRoot, projectRoot }, loaded, assetsForPack(loaded, "ecc-research"));
		const installed = await Bun.file(path.join(projectRoot, ".omp/skills/ecc-research-ops/SKILL.md")).text();
		expect(installed).toContain(EDIT_MARKER.trim());
		expect(installed).toContain("name: ecc-research-ops");
		expect(report.changed).toBe(true);
	});
});

describe("repin refuses without touching the ECC root", () => {
	test("decision 'reject'", async () => {
		const eccRoot = await makeEccCopy();
		const candidateRoot = await makeCandidate("skills/research-ops/SKILL.md");
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		const reviewPath = await writeReview(eccRoot, { ...makeAcceptedReview(report), decision: "reject" });

		const err = await expectRefusalUnchanged(eccRoot, () => repin({ eccRoot, candidateRoot, review: reviewPath }));
		expect(err.message).toContain("expected 'accept'");
	});

	test("wrong toSha256", async () => {
		const eccRoot = await makeEccCopy();
		const candidateRoot = await makeCandidate("skills/research-ops/SKILL.md");
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		const review = makeAcceptedReview(report);
		review.changes[0].toSha256 = "0".repeat(64);
		const reviewPath = await writeReview(eccRoot, review);

		const err = await expectRefusalUnchanged(eccRoot, () => repin({ eccRoot, candidateRoot, review: reviewPath }));
		expect(err.message).toContain("review.changes does not match");
	});

	test("missing change row", async () => {
		const eccRoot = await makeEccCopy();
		const candidateRoot = await makeCandidate("skills/research-ops/SKILL.md");
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		const reviewPath = await writeReview(eccRoot, { ...makeAcceptedReview(report), changes: [] });

		const err = await expectRefusalUnchanged(eccRoot, () => repin({ eccRoot, candidateRoot, review: reviewPath }));
		expect(err.message).toContain("review.changes does not match");
	});

	test("wrong baseCommit", async () => {
		const eccRoot = await makeEccCopy();
		const candidateRoot = await makeCandidate("skills/research-ops/SKILL.md");
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		const reviewPath = await writeReview(eccRoot, { ...makeAcceptedReview(report), baseCommit: "1".repeat(40) });

		const err = await expectRefusalUnchanged(eccRoot, () => repin({ eccRoot, candidateRoot, review: reviewPath }));
		expect(err.message).toContain("baseCommit");
	});

	test("candidate edits an overlay-backed asset", async () => {
		const eccRoot = await makeEccCopy();
		const candidateRoot = await makeCandidate("skills/search-first/SKILL.md");
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		expect(report.overlayConflicts).toEqual(["ecc-search-first"]);
		const reviewPath = await writeReview(eccRoot, makeAcceptedReview(report));

		const err = await expectRefusalUnchanged(eccRoot, () => repin({ eccRoot, candidateRoot, review: reviewPath }));
		expect(err.message).toContain("overlay-backed asset(s): ecc-search-first");
	});

	test("identical candidate has nothing to change", async () => {
		const eccRoot = await makeEccCopy();
		const candidateRoot = await makeCandidate();
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		expect(report.changed).toBe(false);
		const reviewPath = await writeReview(eccRoot, makeAcceptedReview(report));

		const err = await expectRefusalUnchanged(eccRoot, () => repin({ eccRoot, candidateRoot, review: reviewPath }));
		expect(err.message).toContain("nothing to re-pin");
	});
});

describe("repin CLI", () => {
	test("prints the result object and exits 0 on an accepted review", async () => {
		const { eccRoot, candidateRoot, reviewPath } = await acceptedFixture();
		const result = await runCli(["--candidate-root", candidateRoot, "--review", reviewPath, "--ecc-root", eccRoot]);

		expect(result.code).toBe(0);
		expect(result.stderr).toBe("");
		const body = JSON.parse(result.stdout) as { candidateCommit: string; changedFiles: string[] };
		expect(body.candidateCommit).toBe(CANDIDATE_COMMIT);
		expect(body.changedFiles).toEqual(["skills/research-ops/SKILL.md"]);
	});

	test("prints an error object and exits 1 on refusal", async () => {
		const eccRoot = await makeEccCopy();
		const candidateRoot = await makeCandidate("skills/research-ops/SKILL.md");
		const report = await checkUpstream({ eccRoot, candidateRoot, candidateCommit: CANDIDATE_COMMIT });
		const reviewPath = await writeReview(eccRoot, { ...makeAcceptedReview(report), decision: "reject" });

		const result = await runCli(["--candidate-root", candidateRoot, "--review", reviewPath, "--ecc-root", eccRoot]);

		expect(result.code).toBe(1);
		expect(result.stderr).toBe("");
		const body = JSON.parse(result.stdout) as { error?: string };
		expect(body.error).toContain("expected 'accept'");
	});
});
