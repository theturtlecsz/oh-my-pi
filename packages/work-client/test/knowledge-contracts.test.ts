import { expect, test } from "bun:test";
import path from "node:path";
import { type CodeSnapshotManifest, childFactId, codeSnapshotId, type EnolaFact } from "../src/index";

type SnapshotVector = {
	name: string;
	workspace_id: string;
	repository_id: string;
	base_commit: string;
	files: CodeSnapshotManifest["files"];
	snapshot_id: string;
};

type ChildFactVector = {
	name: string;
	snap: string;
	fact_id: string;
	kind: string;
	name_fact: string;
	file: string;
	line: number;
	child_fact_id: string;
};

const FIXTURE_PATH = path.resolve(import.meta.dir, "../../../fixtures/knowledge_vectors.json");
const vectors = (await Bun.file(FIXTURE_PATH).json()) as {
	code_snapshots: SnapshotVector[];
	child_facts: ChildFactVector[];
};

test("every code_snapshots vector recomputes its snapshot_id, order-independent", () => {
	expect(vectors.code_snapshots.length).toBeGreaterThan(0);
	for (const item of vectors.code_snapshots) {
		const manifest: CodeSnapshotManifest = {
			workspace_id: item.workspace_id,
			repository_id: item.repository_id,
			base_commit: item.base_commit,
			files: item.files,
		};
		expect(codeSnapshotId(manifest)).toBe(item.snapshot_id);

		// File order must not change the id: the canonical sort is by path bytes.
		const reversed: CodeSnapshotManifest = { ...manifest, files: [...item.files].reverse() };
		expect(codeSnapshotId(reversed)).toBe(item.snapshot_id);
	}
});

test("every child_facts vector recomputes its child_fact_id", () => {
	expect(vectors.child_facts.length).toBeGreaterThan(0);
	for (const item of vectors.child_facts) {
		const fact: EnolaFact = {
			fact_id: item.fact_id,
			kind: item.kind,
			name: item.name_fact,
			file: item.file,
			line: item.line,
		};
		expect(childFactId(item.snap, fact)).toBe(item.child_fact_id);
	}
});

test("childFactId rejects a malformed snapshot id", () => {
	const fact: EnolaFact = {
		fact_id: "a".repeat(32),
		kind: "function",
		name: "load_config",
		file: "src/config.py",
		line: 25,
	};
	for (const bad of ["", "short", "Z".repeat(64), "1".repeat(63), "1".repeat(65)]) {
		expect(() => childFactId(bad, fact)).toThrow(/snap/);
	}
});
