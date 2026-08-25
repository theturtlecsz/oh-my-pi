import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { nowRefusal } from "../extensions/workflow/backend";
import { createWorkBackend } from "../extensions/workflow/work";

const WS = "00000000-0000-7000-8000-000000000000";
const OWNER = "00000000-0000-7000-8000-000000000002";
const PROJECT = "00000000-0000-7000-8000-000000000010";

function item(id: string, key: string, state: string, archived = false) {
	return {
		work_id: id,
		workspace_id: WS,
		alias: { work_id: id, key, primary: true, origin: "local" },
		state,
		revision: { revision_id: `${id}-r1`, title: `${key} title`, description: "", created_at: "2026-08-25T00:00:00Z" },
		candidate: null,
		project_id: PROJECT,
		archived,
	};
}

const OPEN = item("00000000-0000-7000-8000-000000000021", "OMP-1", "BACKLOG");
const CANCELED = item("00000000-0000-7000-8000-000000000022", "OMP-2", "CANCELED");

function makeBackend(tempDir: string, focusedId: string | null) {
	const mockFetch = async (input: RequestInfo | URL): Promise<Response> => {
		const url = String(input);
		const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
		if (url.includes("/tree")) {
			return json({ workspace_id: WS, items: [OPEN, CANCELED], relations: [], projects: [{ project_id: PROJECT, workspace_id: WS, key: null, name: "Alpha", health: null, health_updated_at: null }] });
		}
		if (url.includes("/focus/")) return json({ workspace_id: WS, owner_id: OWNER, work_id: focusedId, version: 1 });
		if (url.includes("/work-items/OMP-1")) return json(OPEN);
		if (url.includes("/work-items/OMP-2")) return json(CANCELED);
		return new Response("not found", { status: 404 });
	};
	return createWorkBackend({ baseUrl: "http://127.0.0.1:9999", workspaceId: WS, ownerId: OWNER }, () => "mock-token", mockFetch as never, tempDir);
}

let tempDir: string;
beforeEach(() => {
	tempDir = mkdtempSync(join(tmpdir(), "now-terminal-guard-"));
});
afterEach(() => {
	rmSync(tempDir, { recursive: true, force: true });
});

describe("nowRefusal — closed work never becomes NOW (owner ruling 2026-08-25)", () => {
	test("refuses DONE, CANCELED, CANCELLED, and archived refs with plain words", () => {
		for (const state of ["DONE", "CANCELED", "CANCELLED"]) {
			const refusal = nowRefusal({ id: "x", key: "OMP-9", title: "t", state });
			expect(refusal).toContain("OMP-9");
			expect(refusal).toContain("can't be NOW");
		}
		expect(nowRefusal({ id: "x", key: "OMP-9", title: "t", state: "BACKLOG", archived: true })).toContain("archived");
	});

	test("passes open states and stateless host-built refs", () => {
		for (const state of ["BACKLOG", "TRIAGE", "IN_PROGRESS"]) {
			expect(nowRefusal({ id: "x", key: "OMP-9", title: "t", state })).toBeNull();
		}
		expect(nowRefusal({ id: "x", key: "OMP-9", title: "t" })).toBeNull();
	});
});

describe("backend refs carry ledger state", () => {
	test("findIssue on a canceled key returns a ref the NOW guard refuses", async () => {
		const backend = makeBackend(tempDir, null);
		const ref = await backend.findIssue("OMP-2");
		expect(ref.state).toBe("CANCELED");
		expect(nowRefusal(ref)).toContain("OMP-2");
	});

	test("currentNow never resurrects a focus slot pointing at canceled work", async () => {
		const backend = makeBackend(tempDir, CANCELED.work_id);
		expect(await backend.currentNow()).toBeNull();
	});

	test("currentNow still restores an open focus", async () => {
		const backend = makeBackend(tempDir, OPEN.work_id);
		const ref = await backend.currentNow();
		expect(ref?.key).toBe("OMP-1");
		expect(ref?.state).toBe("BACKLOG");
	});
});
