/**
 * OMP-610-s06: the WP3/WP7 ledger drafts are the exact input `ledger_admin.py create`
 * accepts. A wrong key set, empty field or reused client_ref is rejected by the owner
 * step (OMP-610-s07) that creates the items.
 */
import { describe, expect, it } from "bun:test";
import * as path from "node:path";

const DRAFTS = path.join(import.meta.dir, "../../docs/programme/ECC-WP3-WP7-LEDGER-ITEMS.json");
const KEYS = ["acceptance_criteria", "client_ref", "description", "parent", "project", "title"];

describe("ECC WP3/WP7 ledger drafts", () => {
	it("holds two well-formed create-input objects", async () => {
		const items = (await Bun.file(DRAFTS).json()) as Record<string, unknown>[];
		expect(Array.isArray(items)).toBe(true);
		expect(items).toHaveLength(2);
		for (const item of items) {
			expect(Object.keys(item).sort()).toEqual(KEYS);
			for (const key of ["client_ref", "title", "description", "project", "parent"]) {
				expect(typeof item[key]).toBe("string");
				expect((item[key] as string).trim()).not.toBe("");
			}
			const criteria = item.acceptance_criteria as unknown[];
			expect(Array.isArray(criteria)).toBe(true);
			expect(criteria.length).toBeGreaterThan(0);
			for (const c of criteria) {
				expect(typeof c).toBe("string");
				expect((c as string).trim()).not.toBe("");
			}
			expect(item.parent).toBe("OMP-610");
			expect(item.project).toBe("Session System");
		}
		expect(new Set(items.map(i => i.client_ref)).size).toBe(2);
		expect((items[0].title as string).startsWith("ECC WP3")).toBe(true);
		expect((items[1].title as string).startsWith("ECC WP7")).toBe(true);
	});
});
