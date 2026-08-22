import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RIDER_BATCH_MAX_BYTES, RiderBatchError, consumeStagedRiderBatch, readStagedRiderBatch } from "../extensions/workflow/rider-batch";

const VALID = JSON.stringify([{ key: "OMP-1", evidence: "probe: pytest -> 3 passed" }]);

let dir: string;
let path: string;

function stage(content: string, mode = 0o600): void {
	writeFileSync(path, content, { mode });
	chmodSync(path, mode); // writeFileSync mode is masked by umask; force exact bits
}

beforeEach(() => {
	dir = mkdtempSync(join(tmpdir(), "rider-batch-"));
	path = join(dir, "batch.json");
});

afterEach(() => {
	rmSync(dir, { recursive: true, force: true });
});

describe("readStagedRiderBatch", () => {
	test("absent file is null, not an error", () => {
		expect(readStagedRiderBatch(path)).toBeNull();
	});

	test("valid 0600 file parses with digest and inode identity", () => {
		stage(VALID);
		const batch = readStagedRiderBatch(path);
		expect(batch).not.toBeNull();
		expect(batch?.entries).toEqual([{ key: "OMP-1", evidence: "probe: pytest -> 3 passed" }]);
		expect(batch?.digest).toMatch(/^[0-9a-f]{64}$/);
		expect(batch?.ino).toBeGreaterThan(0);
	});

	test("symlink to a valid file is refused at open", () => {
		const real = join(dir, "real.json");
		writeFileSync(real, VALID, { mode: 0o600 });
		chmodSync(real, 0o600);
		symlinkSync(real, path);
		expect(() => readStagedRiderBatch(path)).toThrow(RiderBatchError);
		expect(() => readStagedRiderBatch(path)).toThrow(/symlink/);
	});

	test("wrong mode is refused — 0644 and 0700 alike", () => {
		stage(VALID, 0o644);
		expect(() => readStagedRiderBatch(path)).toThrow(/0600 exactly/);
		chmodSync(path, 0o700);
		expect(() => readStagedRiderBatch(path)).toThrow(/0600 exactly/);
	});

	test("oversized batch is refused", () => {
		stage(JSON.stringify([{ key: "OMP-1", evidence: "x".repeat(RIDER_BATCH_MAX_BYTES) }]));
		expect(() => readStagedRiderBatch(path)).toThrow(/exceeds/);
	});

	test("malformed JSON, empty array, and empty evidence are refused", () => {
		stage("not json");
		expect(() => readStagedRiderBatch(path)).toThrow(/not valid JSON/);
		stage("[]");
		expect(() => readStagedRiderBatch(path)).toThrow(/non-empty/);
		stage(JSON.stringify([{ key: "OMP-1", evidence: "   " }]));
		expect(() => readStagedRiderBatch(path)).toThrow(/non-empty evidence/);
	});
});

describe("consumeStagedRiderBatch", () => {
	test("archives the exact approved inode", () => {
		stage(VALID);
		const batch = readStagedRiderBatch(path);
		if (!batch) throw new Error("batch expected");
		const target = consumeStagedRiderBatch(batch, "consumed");
		expect(existsSync(path)).toBe(false);
		expect(target).toContain(".consumed-");
		expect(readdirSync(dir).some(name => name.includes(".consumed-"))).toBe(true);
	});

	test("declined batches archive under a distinct suffix", () => {
		stage(VALID);
		const batch = readStagedRiderBatch(path);
		if (!batch) throw new Error("batch expected");
		expect(consumeStagedRiderBatch(batch, "declined")).toContain(".declined-");
	});

	test("a post-approval inode swap is detected after archive, loudly", () => {
		stage(VALID);
		const batch = readStagedRiderBatch(path);
		if (!batch) throw new Error("batch expected");
		rmSync(path);
		stage(JSON.stringify([{ key: "OMP-99", evidence: "planted" }])); // new inode at same path
		expect(() => consumeStagedRiderBatch(batch, "consumed")).toThrow(/NOT the approved batch/);
	});

	test("a same-inode rewrite after approval is detected by digest", () => {
		stage(VALID);
		const batch = readStagedRiderBatch(path);
		if (!batch) throw new Error("batch expected");
		writeFileSync(path, JSON.stringify([{ key: "OMP-1", evidence: "quietly altered" }])); // same inode, new bytes
		expect(() => consumeStagedRiderBatch(batch, "consumed")).toThrow(/NOT the approved batch/);
	});

	test("a vanished file is a typed error, not a crash", () => {
		stage(VALID);
		const batch = readStagedRiderBatch(path);
		if (!batch) throw new Error("batch expected");
		rmSync(path);
		expect(() => consumeStagedRiderBatch(batch, "consumed")).toThrow(/vanished/);
	});
});
