import { describe, expect, test } from "bun:test";
import {
	buildSubjectIndex,
	checkInventory,
	computeDivergence,
	type DivergingPath,
	formatInventoryTsv,
	type InventoryRow,
	mergeInventory,
	parseInventoryTsv,
} from "./upstream-inventory.ts";

const DIFF_TREE = [
	":000000 100644 0000000000000000000000000000000000000000 1111111111111111111111111111111111111111 A\tsession-system/update.sh",
	":100644 100644 2222222222222222222222222222222222222222 3333333333333333333333333333333333333333 M\tpackage.json",
	":100644 000000 4444444444444444444444444444444444444444 0000000000000000000000000000000000000000 D\tupstream-only.txt",
	":100644 100755 5555555555555555555555555555555555555555 6666666666666666666666666666666666666666 T\tscripts/tool.ts",
	"",
].join("\n");

function rows(): InventoryRow[] {
	return [
		{
			path: "package.json",
			scope: "shared",
			state: "modified",
			headBlob: "333333333333",
			behavior: "fork adds test:scripts entries",
			classification: "re-fitted",
		},
		{
			path: "scripts/tool.ts",
			scope: "shared",
			state: "modified",
			headBlob: "666666666666",
			behavior: "fork made executable with edits",
			classification: "retained",
		},
		{
			path: "session-system/update.sh",
			scope: "fork-only",
			state: "added",
			headBlob: "111111111111",
			behavior: "fork updater",
			classification: "retained",
		},
		{
			path: "upstream-only.txt",
			scope: "shared",
			state: "deleted",
			headBlob: "-",
			behavior: "fork removed upstream sample",
			classification: "retained",
		},
	];
}

describe("computeDivergence", () => {
	test("maps A/M/D/T statuses to scope and state with 12-hex head blobs, sorted by path", () => {
		expect(computeDivergence(DIFF_TREE)).toEqual([
			{ path: "package.json", scope: "shared", state: "modified", headBlob: "333333333333" },
			{ path: "scripts/tool.ts", scope: "shared", state: "modified", headBlob: "666666666666" },
			{ path: "session-system/update.sh", scope: "fork-only", state: "added", headBlob: "111111111111" },
			{ path: "upstream-only.txt", scope: "shared", state: "deleted", headBlob: "-" },
		] satisfies DivergingPath[]);
	});
});

describe("inventory TSV", () => {
	test("round-trips through format/parse", () => {
		expect(parseInventoryTsv(formatInventoryTsv(rows()))).toEqual(rows());
	});
	test("rejects a bad header", () => {
		expect(() => parseInventoryTsv("wrong\theader\n")).toThrow(/bad header/);
	});
	test("rejects rows with the wrong field count", () => {
		const header = "path\tscope\tstate\thead_blob\tbehavior\tclassification\n";
		expect(() => parseInventoryTsv(`${header}too\tfew\n`)).toThrow(/row 2/);
	});
});

describe("checkInventory", () => {
	const computed = computeDivergence(DIFF_TREE);

	test("current inventory passes", () => {
		expect(checkInventory(computed, rows())).toEqual([]);
	});
	test("fails when a diverging path has no row", () => {
		const errors = checkInventory(
			computed,
			rows().filter(r => r.path !== "package.json"),
		);
		expect(errors).toHaveLength(1);
		expect(errors[0]).toContain(
			"package.json diverges from the upstream baseline (modified) but has no inventory row",
		);
	});
	test("fails when a row is stale", () => {
		const stale = rows();
		stale[0] = { ...stale[0], headBlob: "aaaaaaaaaaaa" };
		const errors = checkInventory(computed, stale);
		expect(errors).toHaveLength(1);
		expect(errors[0]).toContain("package.json row is stale");
		expect(errors[0]).toContain("computed shared/modified/333333333333");
	});
	test("fails when a row outlives its divergence", () => {
		const extra = [
			...rows(),
			{
				path: "gone.ts",
				scope: "fork-only",
				state: "added",
				headBlob: "abcabcabcabc",
				behavior: "old feature",
				classification: "retained",
			} satisfies InventoryRow,
		];
		const errors = checkInventory(computed, extra);
		expect(errors).toHaveLength(1);
		expect(errors[0]).toContain("gone.ts no longer diverges");
	});
	test("fails on empty behavior, invalid classification, duplicates, and ruling-less drops", () => {
		const bad = rows();
		bad[0] = { ...bad[0], behavior: "  " };
		bad[1] = { ...bad[1], classification: "kept" };
		bad[2] = { ...bad[2], classification: "dropped" };
		bad.push(bad[3]);
		const errors = checkInventory(computed, bad);
		expect(errors.some(e => e.includes("package.json has an empty behavior description"))).toBe(true);
		expect(errors.some(e => e.includes("scripts/tool.ts has invalid classification 'kept'"))).toBe(true);
		expect(
			errors.some(e => e.includes("session-system/update.sh 'dropped' requires an explicit owner-ruling:")),
		).toBe(true);
		expect(errors.some(e => e.includes("duplicate row for upstream-only.txt"))).toBe(true);
	});
	test("accepts dropped rows carrying an owner-ruling reference", () => {
		const ruled = rows();
		ruled[2] = { ...ruled[2], classification: "dropped", behavior: "dropped per owner-ruling: OMP-999" };
		expect(checkInventory(computed, ruled)).toEqual([]);
	});
});

describe("mergeInventory", () => {
	const computed = computeDivergence(DIFF_TREE);

	test("preserves human columns, refreshes machine columns, drops stale rows, seeds new ones", () => {
		const existing: InventoryRow[] = [
			{
				path: "package.json",
				scope: "shared",
				state: "modified",
				headBlob: "000000000000",
				behavior: "fork adds test:scripts entries",
				classification: "re-fitted",
			},
			{
				path: "gone.ts",
				scope: "fork-only",
				state: "added",
				headBlob: "abcabcabcabc",
				behavior: "old feature",
				classification: "retained",
			},
		];
		const merged = mergeInventory(computed, existing, path =>
			path === "session-system/update.sh" ? "seeded subject" : "",
		);
		expect(merged.map(r => r.path)).toEqual(computed.map(c => c.path));
		const pkg = merged.find(r => r.path === "package.json") as InventoryRow;
		expect(pkg.headBlob).toBe("333333333333");
		expect(pkg.behavior).toBe("fork adds test:scripts entries");
		expect(pkg.classification).toBe("re-fitted");
		const updater = merged.find(r => r.path === "session-system/update.sh") as InventoryRow;
		expect(updater.behavior).toBe("seeded subject");
		expect(updater.classification).toBe("retained");
		const unseeded = merged.find(r => r.path === "scripts/tool.ts") as InventoryRow;
		expect(unseeded.behavior).toBe("fork change (describe)");
	});
	test("sanitizes seeded subjects for TSV safety", () => {
		const merged = mergeInventory(computed, [], () => "line\none\ttwo");
		expect(merged[0].behavior).toBe("line one two");
	});
});

describe("buildSubjectIndex", () => {
	test("indexes recent distinct subjects per path from a NUL-marked log", () => {
		const log = [
			"\u0000fix(scripts): newer change",
			"scripts/tool.ts",
			"package.json",
			"",
			"\u0000fix(scripts): newer change",
			"scripts/tool.ts",
			"",
			"\u0000feat: older change",
			"scripts/tool.ts",
			"",
		].join("\n");
		const index = buildSubjectIndex(log);
		expect(index.get("scripts/tool.ts")).toEqual(["fix(scripts): newer change", "feat: older change"]);
		expect(index.get("package.json")).toEqual(["fix(scripts): newer change"]);
	});
	test("caps subjects per path", () => {
		const log = Array.from({ length: 8 }, (_, i) => `\u0000subject ${i}\na.ts\n`).join("");
		expect(buildSubjectIndex(log, 3).get("a.ts")).toEqual(["subject 0", "subject 1", "subject 2"]);
	});
});
