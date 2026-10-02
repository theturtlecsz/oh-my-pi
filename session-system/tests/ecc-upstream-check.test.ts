import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ECC_ROOT, loadManifest } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { checkUpstream } from "../ecc/adapter/upstream-check";

const repoRoot = path.resolve(import.meta.dir, "../..");
const cli = path.join(repoRoot, "session-system/ecc/adapter/upstream-check.ts");
const mirrorRoot = path.join(ECC_ROOT, "mirror");
const tempDirs: string[] = [];

afterEach(async () => {
	for (const dir of tempDirs.splice(0)) {
		await fs.rm(dir, { recursive: true, force: true }).catch(() => {});
	}
});

async function makeCandidateCopy(): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "ecc-upstream-candidate-"));
	tempDirs.push(dir);
	await fs.cp(mirrorRoot, dir, { recursive: true });
	return dir;
}

async function runCli(args: string[]): Promise<{ code: number; stdout: string; stderr: string }> {
	const env = { ...process.env };
	delete env.FORCE_COLOR;
	delete env.NO_COLOR;
	const proc = Bun.spawn(["bun", cli, ...args], {
		cwd: repoRoot,
		stdin: "ignore",
		stdout: "pipe",
		stderr: "pipe",
		env,
	});
	const [stdout, stderr, code] = await Promise.all([
		new Response(proc.stdout).text(),
		new Response(proc.stderr).text(),
		proc.exited,
	]);
	return { code, stdout, stderr };
}

describe("ECC upstream-check adapter", () => {
	test("identical copy: changed false, all rows unchanged", async () => {
		const manifest = await loadManifest(ECC_ROOT);
		const candidateDir = await makeCandidateCopy();
		const candidateCommit = "candidate-commit-8321021c";

		const result = await checkUpstream({
			candidateRoot: candidateDir,
			candidateCommit,
		});

		expect(result.changed).toBe(false);
		expect(result.baseCommit).toBe(manifest.upstream.commit);
		expect(result.candidateCommit).toBe(candidateCommit);
		expect(result.newFiles).toEqual([]);
		expect(result.overlayConflicts).toEqual([]);
		expect(result.files.length).toBe(18);

		for (const file of result.files) {
			expect(file.status).toBe("unchanged");
			expect(file.toSha256).toBe(file.fromSha256);
			expect(file.toSha256).toMatch(/^[0-9a-f]{64}$/);
		}

		// Files are sorted by mirror-relative path
		const paths = result.files.map(f => f.path);
		const sortedPaths = [...paths].sort((a, b) => a.localeCompare(b));
		expect(paths).toEqual(sortedPaths);
	});

	test("edited skills/research-ops/SKILL.md: only that row changed, new toSha256", async () => {
		const candidateDir = await makeCandidateCopy();
		const targetFile = path.join(candidateDir, "skills/research-ops/SKILL.md");
		const originalText = await Bun.file(targetFile).text();
		const editedText = `${originalText}\n<!-- added line -->\n`;
		await Bun.write(targetFile, editedText);

		const expectedSha = sha256Hex(editedText);

		const result = await checkUpstream({
			candidateRoot: candidateDir,
			candidateCommit: "candidate-commit-edited",
		});

		expect(result.changed).toBe(true);
		expect(result.newFiles).toEqual([]);
		expect(result.overlayConflicts).toEqual([]);

		const changedRows = result.files.filter(f => f.status !== "unchanged");
		expect(changedRows.length).toBe(1);

		const row = changedRows[0];
		expect(row.path).toBe("skills/research-ops/SKILL.md");
		expect(row.assetId).toBe("ecc-research-ops");
		expect(row.status).toBe("changed");
		expect(row.toSha256).toBe(expectedSha);
		expect(row.toSha256).not.toBe(row.fromSha256);

		// All other rows remain unchanged
		const unchangedRows = result.files.filter(f => f.status === "unchanged");
		expect(unchangedRows.length).toBe(17);
	});

	test("deleted helper: row missing; added skills/agent-self-evaluation/extra.md: in newFiles", async () => {
		const candidateDir = await makeCandidateCopy();
		const helperPath = path.join(candidateDir, "skills/agent-self-evaluation/scripts/evaluate.py");
		await fs.rm(helperPath);

		const extraPath = path.join(candidateDir, "skills/agent-self-evaluation/extra.md");
		await Bun.write(extraPath, "# Extra helper file\n");

		const result = await checkUpstream({
			candidateRoot: candidateDir,
			candidateCommit: "candidate-commit-deleted-added",
		});

		expect(result.changed).toBe(true);

		const missingRow = result.files.find(
			f => f.path === "skills/agent-self-evaluation/scripts/evaluate.py",
		);
		expect(missingRow).toBeDefined();
		expect(missingRow?.status).toBe("missing");
		expect(missingRow?.toSha256).toBeNull();
		expect(missingRow?.fromSha256).toMatch(/^[0-9a-f]{64}$/);

		expect(result.newFiles).toEqual(["skills/agent-self-evaluation/extra.md"]);
	});

	test("edited skills/search-first/SKILL.md: overlayConflicts [\"ecc-search-first\"]", async () => {
		const candidateDir = await makeCandidateCopy();
		const targetFile = path.join(candidateDir, "skills/search-first/SKILL.md");
		const originalText = await Bun.file(targetFile).text();
		await Bun.write(targetFile, `${originalText}\n<!-- overlay modification -->\n`);

		const result = await checkUpstream({
			candidateRoot: candidateDir,
			candidateCommit: "candidate-commit-overlay-conflict",
		});

		expect(result.changed).toBe(true);
		expect(result.overlayConflicts).toEqual(["ecc-search-first"]);

		const searchFirstRow = result.files.find(f => f.path === "skills/search-first/SKILL.md");
		expect(searchFirstRow).toBeDefined();
		expect(searchFirstRow?.status).toBe("changed");
	});

	test("CLI without --candidate-root exits 1 with an error object", async () => {
		const result = await runCli([]);
		expect(result.code).toBe(1);
		expect(result.stderr).toBe("");
		const body = JSON.parse(result.stdout) as { error?: string };
		expect(body.error).toContain("--candidate-root requires a directory");
	});

	test("CLI prints one JSON object and exits 0 on valid arguments", async () => {
		const candidateDir = await makeCandidateCopy();
		const commit = "candidate-commit-cli-test";
		const result = await runCli(["--candidate-root", candidateDir, "--commit", commit]);

		expect(result.code).toBe(0);
		expect(result.stderr).toBe("");
		const body = JSON.parse(result.stdout) as {
			baseCommit: string;
			candidateCommit: string;
			files: unknown[];
			newFiles: unknown[];
			overlayConflicts: unknown[];
			changed: boolean;
		};
		expect(body.candidateCommit).toBe(commit);
		expect(body.changed).toBe(false);
		expect(body.files.length).toBe(18);
		expect(body.newFiles).toEqual([]);
		expect(body.overlayConflicts).toEqual([]);
	});
});
