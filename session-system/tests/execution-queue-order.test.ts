import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import type { RelationEdge, WorkItemView, WorkspaceTree } from "@oh-my-pi/pi-work-client";
import { createWorkBackend } from "../extensions/workflow/work";

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";
const PROJECT_ID = "00000000-0000-7000-8000-000000000003";

function makeItem(
	key: string,
	state = "BACKLOG",
	options?: {
		workId?: string;
		archived?: boolean;
		description?: string;
		projectId?: string;
	},
): WorkItemView {
	const workId = options?.workId ?? `work-${key.toLowerCase()}`;
	return {
		work_id: workId,
		workspace_id: WORKSPACE_ID,
		alias: { work_id: workId, key, primary: true, origin: "local" },
		state,
		revision: {
			revision_id: `rev-${key.toLowerCase()}`,
			work_id: workId,
			revision_number: 1,
			title: `Title for ${key}`,
			description: options?.description ?? `Description for ${key}`,
			scope: "test",
			acceptance_criteria: ["acceptance criterion"],
			content_sha256: "0".repeat(64),
			created_by: "tester",
			created_at: "2026-09-25T10:00:00Z",
		},
		candidate: null,
		project_id: options?.projectId ?? PROJECT_ID,
		archived: options?.archived ?? false,
	};
}

function makeParentRelation(childWorkId: string, parentWorkId: string, active = true): RelationEdge {
	return {
		workspace_id: WORKSPACE_ID,
		source_work_id: childWorkId,
		target_work_id: parentWorkId,
		kind: "parent",
		active,
	};
}

function createBackendForTree(tempDir: string, tree: { items: WorkItemView[]; relations?: RelationEdge[] }) {
	const workspaceTree: WorkspaceTree = {
		workspace_id: WORKSPACE_ID,
		items: tree.items,
		relations: tree.relations ?? [],
		projects: [
			{
				project_id: PROJECT_ID,
				workspace_id: WORKSPACE_ID,
				key: "OMP",
				name: "OMP",
				health: "onTrack",
				health_updated_at: "2026-09-25T10:00:00Z",
			},
		],
	};

	const mockFetch = async (input: RequestInfo | URL): Promise<Response> => {
		const url = String(input);
		if (url.includes("/tree")) {
			return Response.json(workspaceTree);
		}
		return new Response("not found", { status: 404 });
	};

	return createWorkBackend(
		{ baseUrl: "http://127.0.0.1:9999", workspaceId: WORKSPACE_ID, ownerId: OWNER_ID },
		() => "test-token",
		mockFetch as never,
		tempDir,
	);
}

describe("snapshotQueue dependency-first ordering (OMP-219-s05)", () => {
	let tempDir: string;

	beforeEach(async () => {
		tempDir = await fs.mkdtemp(path.join(os.tmpdir(), "exec-queue-order-"));
	});

	afterEach(async () => {
		await fs.rm(tempDir, { recursive: true, force: true });
	});

	test("umbrella claim position follows all eligible direct AND transitive children regardless of key order; first claim is child (OMP-202/OMP-209/OMP-218)", async () => {
		const umbrella = makeItem("OMP-1", "TODO", { workId: "work-umbrella" });
		const child = makeItem("OMP-2", "TODO", { workId: "work-child" });
		const grandchild = makeItem("OMP-3", "TODO", { workId: "work-grandchild" });

		const relations: RelationEdge[] = [
			makeParentRelation(grandchild.work_id, child.work_id),
			makeParentRelation(child.work_id, umbrella.work_id),
		];

		const backend = createBackendForTree(tempDir, {
			items: [umbrella, child, grandchild],
			relations,
		});

		// Focus is on umbrella OMP-1
		const claims = await backend.snapshotQueue(undefined, "OMP-1");

		expect(claims).toHaveLength(3);
		// Admission order: position 0 must activate first
		expect(claims[0].work_id).toBe(grandchild.work_id);
		expect(claims[0].position).toBe(0);
		expect(claims[1].work_id).toBe(child.work_id);
		expect(claims[1].position).toBe(1);
		expect(claims[2].work_id).toBe(umbrella.work_id);
		expect(claims[2].position).toBe(2);
	});

	test("ready current item stays first; blocked current umbrella never bypasses its child", async () => {
		// Ready current item stays first among ready items
		const readyCurrent = makeItem("OMP-5", "TODO", { workId: "work-5" });
		const other1 = makeItem("OMP-1", "TODO", { workId: "work-1" });
		const other2 = makeItem("OMP-2", "TODO", { workId: "work-2" });

		const backendReady = createBackendForTree(tempDir, {
			items: [other1, other2, readyCurrent],
			relations: [],
		});

		const readyClaims = await backendReady.snapshotQueue(undefined, "OMP-5");
		expect(readyClaims.map(c => c.work_id)).toEqual(["work-5", "work-1", "work-2"]);
		expect(readyClaims[0].position).toBe(0);

		// Blocked current umbrella never bypasses its child
		const blockedUmbrella = makeItem("OMP-1", "TODO", { workId: "work-umbrella-1" });
		const deliverableChild = makeItem("OMP-10", "TODO", { workId: "work-child-10" });
		const backendBlocked = createBackendForTree(tempDir, {
			items: [blockedUmbrella, deliverableChild],
			relations: [makeParentRelation(deliverableChild.work_id, blockedUmbrella.work_id)],
		});

		const blockedClaims = await backendBlocked.snapshotQueue(undefined, "OMP-1");
		expect(blockedClaims.map(c => c.work_id)).toEqual(["work-child-10", "work-umbrella-1"]);
		expect(blockedClaims[0].work_id).toBe(deliverableChild.work_id);
		expect(blockedClaims[1].work_id).toBe(blockedUmbrella.work_id);
	});

	test("edge-free items retain ascending (numeric key, work_id) order", async () => {
		const item100 = makeItem("OMP-100", "TODO", { workId: "work-100" });
		const item5 = makeItem("OMP-5", "TODO", { workId: "work-5" });
		const item20 = makeItem("OMP-20", "TODO", { workId: "work-20" });
		const item2 = makeItem("OMP-2", "TODO", { workId: "work-2" });

		const backend = createBackendForTree(tempDir, {
			items: [item100, item5, item20, item2],
			relations: [],
		});

		const claims = await backend.snapshotQueue();
		expect(claims.map(c => c.work_id)).toEqual(["work-2", "work-5", "work-20", "work-100"]);
		expect(claims.map(c => c.position)).toEqual([0, 1, 2, 3]);

		// Numeric tie-breaker: same numeric key sorted by work_id
		const tieA = makeItem("ALPHA-10", "TODO", { workId: "work-b" });
		const tieB = makeItem("BETA-10", "TODO", { workId: "work-a" });
		const backendTie = createBackendForTree(tempDir, {
			items: [tieA, tieB],
			relations: [],
		});

		const tieClaims = await backendTie.snapshotQueue();
		expect(tieClaims.map(c => c.work_id)).toEqual(["work-a", "work-b"]);
	});

	test("closed (DONE, CANCELED, CANCELLED) children do not delay parent", async () => {
		const parent = makeItem("OMP-1", "TODO", { workId: "work-parent" });
		const childDone = makeItem("OMP-2", "DONE", { workId: "work-done" });
		const childCanceled = makeItem("OMP-3", "CANCELED", { workId: "work-canceled" });
		const childCancelled = makeItem("OMP-4", "CANCELLED", { workId: "work-cancelled" });

		const backendAllClosed = createBackendForTree(tempDir, {
			items: [parent, childDone, childCanceled, childCancelled],
			relations: [
				makeParentRelation(childDone.work_id, parent.work_id),
				makeParentRelation(childCanceled.work_id, parent.work_id),
				makeParentRelation(childCancelled.work_id, parent.work_id),
			],
		});

		const allClosedClaims = await backendAllClosed.snapshotQueue(undefined, "OMP-1");
		expect(allClosedClaims).toHaveLength(1);
		expect(allClosedClaims[0].work_id).toBe(parent.work_id);
		expect(allClosedClaims[0].position).toBe(0);

		// One closed child and one open child: parent only waits for open child
		const openChild = makeItem("OMP-10", "TODO", { workId: "work-open-child" });
		const backendMixed = createBackendForTree(tempDir, {
			items: [parent, childDone, openChild],
			relations: [
				makeParentRelation(childDone.work_id, parent.work_id),
				makeParentRelation(openChild.work_id, parent.work_id),
			],
		});

		const mixedClaims = await backendMixed.snapshotQueue();
		expect(mixedClaims.map(c => c.work_id)).toEqual(["work-open-child", "work-parent"]);
	});

	test("cycle plus downstream remainder is deterministic and does not throw", async () => {
		const independent = makeItem("OMP-2", "TODO", { workId: "work-independent" });
		const cyclicA = makeItem("OMP-10", "TODO", { workId: "work-cycle-a" });
		const cyclicB = makeItem("OMP-20", "TODO", { workId: "work-cycle-b" });
		const downstream = makeItem("OMP-5", "TODO", { workId: "work-downstream" });

		// cyclicA <-> cyclicB, and downstream depends on cyclicB
		const relations: RelationEdge[] = [
			makeParentRelation(cyclicA.work_id, cyclicB.work_id),
			makeParentRelation(cyclicB.work_id, cyclicA.work_id),
			makeParentRelation(cyclicB.work_id, downstream.work_id),
		];

		const backend = createBackendForTree(tempDir, {
			items: [independent, cyclicA, cyclicB, downstream],
			relations,
		});

		const claims = await backend.snapshotQueue();
		expect(claims).toHaveLength(4);
		// Independent pops first via Kahn ordering
		expect(claims[0].work_id).toBe(independent.work_id);
		expect(claims[0].position).toBe(0);
		// Unplaced cycle and downstream nodes are appended in ascending (numeric key, work_id) order:
		// OMP-5 (work-downstream), OMP-10 (work-cycle-a), OMP-20 (work-cycle-b)
		expect(claims[1].work_id).toBe(downstream.work_id);
		expect(claims[2].work_id).toBe(cyclicA.work_id);
		expect(claims[3].work_id).toBe(cyclicB.work_id);
		expect(claims.map(c => c.position)).toEqual([0, 1, 2, 3]);
	});

	test("umbrella follows multiple direct children with branch merges", async () => {
		const parent = makeItem("OMP-1", "TODO", { workId: "work-parent" });
		const child1 = makeItem("OMP-10", "TODO", { workId: "work-child-10" });
		const child2 = makeItem("OMP-20", "TODO", { workId: "work-child-20" });
		const grandchild = makeItem("OMP-5", "TODO", { workId: "work-grandchild-5" });

		// grandchild -> child1 -> parent
		// child2 -> parent
		const relations: RelationEdge[] = [
			makeParentRelation(grandchild.work_id, child1.work_id),
			makeParentRelation(child1.work_id, parent.work_id),
			makeParentRelation(child2.work_id, parent.work_id),
		];

		const backend = createBackendForTree(tempDir, {
			items: [parent, child1, child2, grandchild],
			relations,
		});

		const claims = await backend.snapshotQueue(undefined, "OMP-1");
		expect(claims.map(c => c.work_id)).toEqual([
			"work-grandchild-5",
			"work-child-10",
			"work-child-20",
			"work-parent",
		]);
	});
});
