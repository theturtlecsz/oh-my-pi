import { afterEach, expect, test } from "bun:test";
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { checkProspectiveContract, computeContractSha256FromDisk } from "../extensions/workflow/config";

const temporaryDirectories: string[] = [];

afterEach(() => {
	for (const directory of temporaryDirectories.splice(0)) {
		rmSync(directory, { recursive: true, force: true });
	}
});

function createTempRepoWithContract(): { dir: string; contractDir: string } {
	const dir = mkdtempSync(join(tmpdir(), "contract-isolation-"));
	temporaryDirectories.push(dir);
	const contractDir = join(dir, "python/omp-work/src/omp_work/contracts/v1");
	const realContractDir = resolve(import.meta.dir, "../../python/omp-work/src/omp_work/contracts/v1");
	cpSync(realContractDir, contractDir, { recursive: true });
	return { dir, contractDir };
}

test("checkProspectiveContract evaluates approved when repo contract matches approval.json", () => {
	const { dir } = createTempRepoWithContract();
	const result = checkProspectiveContract(dir);

	expect(result.approved).toBe(true);
	expect(result.prospectiveDigest).toBe(result.approvedDigest);
	expect(result.prospectiveDigest.length).toBe(64);
});

test("checkProspectiveContract detects unapproved modification in contract files", () => {
	const { dir, contractDir } = createTempRepoWithContract();
	const schemaPath = join(contractDir, "schema.json");
	const originalSchema = JSON.parse(readFileSync(schemaPath, "utf8")) as Record<string, unknown>;

	// Mutate schema.json to create an unapproved contract change
	originalSchema.__test_mutation__ = "unapproved_change";
	writeFileSync(schemaPath, JSON.stringify(originalSchema, null, 2));

	const result = checkProspectiveContract(dir);

	expect(result.approved).toBe(false);
	expect(result.prospectiveDigest).not.toBe(result.approvedDigest);
	expect(result.prospectiveDigest.length).toBe(64);
});

test("checkProspectiveContract fails closed when approval.json is missing or corrupted", () => {
	const { dir, contractDir } = createTempRepoWithContract();
	const approvalPath = join(contractDir, "approval.json");

	// Corrupt approval.json
	writeFileSync(approvalPath, "{ invalid json");

	const corruptedResult = checkProspectiveContract(dir);
	expect(corruptedResult.approved).toBe(false);
	expect(corruptedResult.approvedDigest).toBe("");

	// Remove approval.json
	rmSync(approvalPath, { force: true });
	const missingResult = checkProspectiveContract(dir);
	expect(missingResult.approved).toBe(false);
	expect(missingResult.approvedDigest).toBe("");
});

test("concurrent checkProspectiveContract executions maintain complete directory isolation", async () => {
	const repoApproved = createTempRepoWithContract();
	const repoMutated = createTempRepoWithContract();

	// Mutate only the second repository
	const contractJsonPath = join(repoMutated.contractDir, "contract.json");
	const contractData = JSON.parse(readFileSync(contractJsonPath, "utf8")) as Record<string, unknown>;
	contractData.__perturbation__ = "isolated_test_mutation";
	writeFileSync(contractJsonPath, JSON.stringify(contractData, null, 2));

	// Run concurrent checks across both repositories
	const [approvedCheck, mutatedCheck] = await Promise.all([
		Promise.resolve(checkProspectiveContract(repoApproved.dir)),
		Promise.resolve(checkProspectiveContract(repoMutated.dir)),
	]);

	expect(approvedCheck.approved).toBe(true);
	expect(mutatedCheck.approved).toBe(false);
	expect(approvedCheck.prospectiveDigest).not.toBe(mutatedCheck.prospectiveDigest);

	// Verify computeContractSha256FromDisk matches
	expect(computeContractSha256FromDisk(repoApproved.contractDir)).toBe(approvedCheck.prospectiveDigest);
	expect(computeContractSha256FromDisk(repoMutated.contractDir)).toBe(mutatedCheck.prospectiveDigest);
});
