import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { sha256Hex } from "../ecc/adapter/hash";
import { applyOverlay, loadOverlay } from "../ecc/adapter/overlay";
import type { Overlay } from "../ecc/adapter/types";

const eccRoot = path.resolve(import.meta.dir, "../ecc");

async function common(stem: string): Promise<{ original: string; overlay: Overlay; edited: string }> {
	const upstreamPath = `rules/common/${stem}.md`;
	const original = await Bun.file(path.join(eccRoot, "mirror", upstreamPath)).text();
	const overlay = await loadOverlay(
		`developer/overlays/common-${stem}.json`,
		eccRoot,
		`ecc-wp3-common-${stem}`,
		upstreamPath,
	);
	return { original, overlay, edited: applyOverlay(original, overlay, sha256Hex(original)) };
}

describe("WP3 common guidance overlays without systems manifest", () => {
	test("full-source pins reject upstream drift and edits beyond the reviewed replacement", async () => {
		for (const stem of ["patterns", "testing", "security"]) {
			const { original, overlay, edited } = await common(stem);
			expect(overlay.edits).toHaveLength(1);
			expect(overlay.edits[0].find.join("\n")).toBe(original);
			expect(overlay.edits[0].replace.join("\n")).toBe(edited);
			expect(overlay.upstreamSha256).toBe(sha256Hex(original));
			expect(overlay.adaptedSha256).toBe(sha256Hex(edited));
			const stale = `${original}\nupstream drift`;
			expect(() => applyOverlay(stale, overlay, sha256Hex(stale))).toThrow("upstream sha256 mismatch");
			expect(() => applyOverlay(stale, overlay, overlay.upstreamSha256)).toThrow("adapted sha256 mismatch");
		}
	});

	test("skeleton selection keeps repository and API methods without agents or cloning", async () => {
		const { original, edited } = await common("patterns");
		expect(edited.split("## Design Patterns")[1]).toBe(original.split("## Design Patterns")[1]);
		expect(edited).toContain("existing repository patterns");
		expect(edited).toContain("Evaluate security, extensibility, relevance, and implementation fit within task scope");
		expect(edited).not.toMatch(/parallel agents|clone/i);
	});

	test("testing selects relevant checks and RED/GREEN/refactor while retaining diagnosis and AAA examples", async () => {
		const { original, edited } = await common("testing");
		expect(edited.split("## Test Structure")[1]).toBe(original.split("## Test Structure")[1]);
		for (const method of [
			"Select unit, integration, or E2E tests according to the changed behavior",
			"Use RED/GREEN/refactor when selected for the task",
			"confirm it fails for the intended reason",
			"Check test isolation",
			"Verify mocks are correct",
			"Fix implementation, not tests (unless tests are wrong)",
		]) expect(edited).toContain(method);
		expect(edited).not.toMatch(/80%|ALL required|MANDATORY|tdd-guide|PROACTIVELY/);
	});

	test("security keeps scoped prevention methods and makes audits optional even after an issue is found", async () => {
		const { edited } = await common("security");
		for (const method of [
			"checks relevant to the changed behavior and its trust boundaries",
			"User inputs validated",
			"parameterized queries with bound values",
			"XSS prevention (sanitized HTML)",
			"CSRF protection",
			"Authentication/authorization verified",
			"Rate limiting where relevant",
			"Error messages don't leak sensitive data",
			"NEVER hardcode secrets",
			"environment variables or a secret manager",
			"required secrets are present at startup",
		]) expect(edited).toContain(method);
		const response = edited.split("If security issue found:\n")[1];
		expect(response).toContain("Fix the root cause within authorized task scope");
		expect(response).toContain("Rotate any exposed secrets");
		expect(response).toContain("Check directly related paths");
		expect(response).toContain("An independent audit is explicitly optional, including when a security issue is found.");
		expect(response).toContain("Finding an issue does not require an independent audit or expand task scope.");
		expect(edited).not.toMatch(/Before ANY commit|security-reviewer|STOP immediately|entire codebase/);
	});
});
