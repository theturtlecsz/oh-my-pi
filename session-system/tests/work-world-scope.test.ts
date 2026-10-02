import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createWorkBackend } from "../extensions/workflow/work";

const WS = "00000000-0000-7000-8000-000000000000";
const OWNER = "00000000-0000-7000-8000-000000000002";
const OMP_PROJECT = "00000000-0000-7000-8000-000000000010";
const TV_PROJECT = "00000000-0000-7000-8000-0000000000a0";

function item(id: string, key: string, title: string, projectId: string, state = "IN_PROGRESS") {
	return {
		work_id: id,
		workspace_id: WS,
		alias: { work_id: id, key, primary: true, origin: "local" },
		state,
		revision: {
			revision_id: `${id}-r1`,
			work_id: id,
			revision_number: 1,
			title,
			description: "",
			scope: "",
			acceptance_criteria: [],
			content_sha256: "0".repeat(64),
			created_by: "test",
			created_at: "2026-09-30T00:00:00Z",
		},
		candidate: null,
		project_id: projectId,
		archived: false,
	};
}

const OMP_ITEM = item("00000000-0000-7000-8000-000000000031", "OMP-1", "OMP goal item", OMP_PROJECT);
const TV_ITEM = item("00000000-0000-7000-8000-0000000000b1", "TV-1", "Live TV item", TV_PROJECT);

const OMP_TREE = {
	workspace_id: WS,
	items: [OMP_ITEM],
	relations: [],
	projects: [{ project_id: OMP_PROJECT, workspace_id: WS, key: null, name: "OMP", health: null, health_updated_at: null }],
};

const TV_TREE = {
	workspace_id: WS,
	items: [TV_ITEM],
	relations: [],
	projects: [{ project_id: TV_PROJECT, workspace_id: WS, key: null, name: "Live TV", health: null, health_updated_at: null }],
};

interface Harness {
	backend: ReturnType<typeof createWorkBackend>;
	urls: string[];
}

function makeHarness(tempDir: string, initialFocus: string | null): Harness {
	const urls: string[] = [];
	const focusId = initialFocus;
	const mockFetch = async (input: RequestInfo | URL): Promise<Response> => {
		const url = String(input);
		urls.push(url);
		const json = (body: unknown) =>
			new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
		if (url.includes("?world=media-discovery")) return json(TV_TREE);
		if (url.includes("/tree")) return json(OMP_TREE);
		if (url.includes("/focus/")) return json({ workspace_id: WS, owner_id: OWNER, work_id: focusId, version: 1 });
		return new Response("not found", { status: 404 });
	};
	const backend = createWorkBackend(
		{ baseUrl: "http://127.0.0.1:9999", workspaceId: WS, ownerId: OWNER },
		() => "mock-token",
		mockFetch as never,
		tempDir,
	);
	return { backend, urls };
}

let tempDir: string;
beforeEach(() => {
	tempDir = mkdtempSync(join(tmpdir(), "work-world-scope-"));
});
afterEach(() => {
	rmSync(tempDir, { recursive: true, force: true });
});

describe("createWorkBackend reaches Media Discovery items only when named (OMP-527)", () => {
	test("OMP focus resolves NOW and goal without reading the media world", async () => {
		const h = makeHarness(tempDir, OMP_ITEM.work_id);

		const now = await h.backend.currentNow();
		expect(now?.key).toBe("OMP-1");

		const goal = await h.backend.goalTree(now!);
		expect(goal?.goal).toBe("OMP");

		expect(h.urls.some(url => url.includes("world="))).toBe(false);
	});

	test("Live TV focus resolves NOW and goal from the media discovery world", async () => {
		const h = makeHarness(tempDir, TV_ITEM.work_id);

		const now = await h.backend.currentNow();
		expect(now?.key).toBe("TV-1");
		expect(now?.project).toBe("Live TV");

		const goal = await h.backend.goalTree(now!);
		expect(goal?.goal).toBe("Live TV");
		expect(goal?.items.map(i => i.key)).toEqual(["TV-1"]);
	});

	test("unnamed tree and map reads stay on the default world", async () => {
		const h = makeHarness(tempDir, OMP_ITEM.work_id);

		const lines = await h.backend.projectTreeLines();
		expect(lines.join("\n")).toContain("OMP");
		expect(lines.join("\n")).not.toContain("Live TV");

		const { surfaces } = await h.backend.mapData();
		expect(surfaces.map(s => s.name)).toEqual(["OMP"]);

		expect(h.urls.some(url => url.includes("world="))).toBe(false);
	});

	test("naming Live TV reaches its scope, items, and tree line", async () => {
		const h = makeHarness(tempDir, null);

		expect(await h.backend.projectScopeExists("Live TV")).toBe(true);

		const { surfaces } = await h.backend.mapData(undefined, "Live TV");
		expect(surfaces.map(s => s.name)).toEqual(["Live TV"]);
		expect(surfaces[0]?.issues.map(i => i.key)).toEqual(["TV-1"]);

		const lines = await h.backend.projectTreeLines("Live TV");
		expect(lines.join("\n")).toContain("Live TV");
	});
});
