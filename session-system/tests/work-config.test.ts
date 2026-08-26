import { afterEach, beforeEach, expect, test } from "bun:test";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadBearer, type WorkClientConfig } from "../extensions/workflow/config";

const originalBearer = process.env.OMP_WORK_BEARER;
const temporaryDirectories: string[] = [];

beforeEach(() => {
	delete process.env.OMP_WORK_BEARER;
});

afterEach(() => {
	if (originalBearer === undefined) {
		delete process.env.OMP_WORK_BEARER;
	} else {
		process.env.OMP_WORK_BEARER = originalBearer;
	}
	for (const directory of temporaryDirectories.splice(0)) {
		rmSync(directory, { recursive: true, force: true });
	}
});

function configWithBearerFile(contents: string, mode = 0o600): WorkClientConfig {
	const directory = mkdtempSync(join(tmpdir(), "omp-work-config-"));
	temporaryDirectories.push(directory);
	const bearerFile = join(directory, "capability.json");
	writeFileSync(bearerFile, contents, { mode });
	chmodSync(bearerFile, mode);
	return {
		baseUrl: "http://127.0.0.1:8787",
		workspaceId: "workspace-test",
		ownerId: "owner-test",
		bearerFile,
	};
}

test("loadBearer returns only the JSON token string", () => {
	const capability = JSON.stringify({
		token: "capability-token",
		workspace_id: "workspace-test",
		grants: ["read", "write"],
	});
	const config = configWithBearerFile(capability);

	expect(loadBearer(config)).toBe("capability-token");
});

test("loadBearer refuses a bearer file that is not mode 0600", () => {
	const capability = JSON.stringify({ token: "insecure-token" });
	const config = configWithBearerFile(capability, 0o644);

	expect(loadBearer(config)).toBeNull();
});

test("loadBearer never returns raw capability JSON", () => {
	const capability = JSON.stringify({
		workspace_id: "workspace-test",
		grants: ["read"],
	});
	const config = configWithBearerFile(capability);

	const bearer = loadBearer(config);
	expect(bearer).toBeNull();
	expect(bearer).not.toBe(capability);
});
