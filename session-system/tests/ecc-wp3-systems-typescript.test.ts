import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { sha256Hex } from "../ecc/adapter/hash";
import { applyOverlay, loadOverlay } from "../ecc/adapter/overlay";
import type { Overlay } from "../ecc/adapter/types";

const eccRoot = path.resolve(import.meta.dir, "../ecc");

async function tsRule(stem: string): Promise<{ original: string; overlay: Overlay; edited: string }> {
	const upstreamPath = `rules/typescript/${stem}.md`;
	const original = await Bun.file(path.join(eccRoot, "mirror", upstreamPath)).text();
	const overlay = await loadOverlay(
		`developer/overlays/typescript-${stem}.json`,
		eccRoot,
		`ecc-wp3-typescript-${stem}`,
		upstreamPath,
	);
	return { original, overlay, edited: applyOverlay(original, overlay, sha256Hex(original)) };
}

describe("WP3 TypeScript overlays without systems manifest", () => {
	test("full-source pins reject upstream drift and edits beyond the reviewed replacement", async () => {
		for (const stem of ["coding-style", "testing", "security"]) {
			const { original, overlay, edited } = await tsRule(stem);
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

	test("coding style preserves types, immutability, validation, and logging while removing hook references", async () => {
		const { edited } = await tsRule("coding-style");
		// Retain common-relative link
		expect(edited).toContain("[common/coding-style.md](../common/coding-style.md)");
		// Preserve exported types
		expect(edited).toContain("export function formatUser(user: User): string");
		// Preserve unknown narrowing
		expect(edited).toContain("function getErrorMessage(error: unknown): string");
		expect(edited).toContain("if (error instanceof Error)");
		// Preserve immutability
		expect(edited).toContain("function updateUser(user: Readonly<User>, name: string): User");
		expect(edited).toContain("...user");
		// Preserve React props
		expect(edited).toContain("interface UserCardProps");
		expect(edited).toContain("function UserCard({ user, onSelect }: UserCardProps)");
		// Preserve JSDoc
		expect(edited).toContain("* @param {{ firstName: string, lastName: string }} user");
		expect(edited).toContain("* @returns {string}");
		// Preserve schema validation
		expect(edited).toContain("import { z } from 'zod'");
		expect(edited).toContain("type UserInput = z.infer<typeof userSchema>");
		expect(edited).toContain("userSchema.parse(input)");
		// Preserve async errors and logging
		expect(edited).toContain("declare function riskyOperation(userId: string): Promise<User>");
		expect(edited).toContain("logger.error('Operation failed', error)");
		// Precedence of existing project tools over illustrative packages
		expect(edited).toContain("Existing project validators take precedence over illustrative libraries");
		expect(edited).toContain("Existing project loggers take precedence over illustrative libraries");
		expect(edited).toContain("no dependency installation");
		// Task-scoped command guidance (bun check)
		expect(edited).toContain("bun check");
		// Automatic hook detection reference removed and no hook/approval mandate
		expect(edited).not.toContain("See hooks for automatic detection");
		expect(edited).not.toMatch(/pre-commit|commit gate|approval mandate|push mandate/i);
	});

	test("testing retains Playwright for relevant flows, references common testing, and removes e2e-runner", async () => {
		const { edited } = await tsRule("testing");
		// Retain common-relative link
		expect(edited).toContain("[common/testing.md](../common/testing.md)");
		// Keep Playwright for relevant critical flows without compulsory gate
		expect(edited).toContain("Playwright");
		expect(edited).toContain("critical user flows");
		expect(edited).toContain("no compulsory E2E run");
		// Reference included common testing guidance
		expect(edited).toContain("rule://ecc-wp3-common-testing");
		// Independent audit is explicitly optional
		expect(edited).toContain("An independent audit is explicitly optional and never compulsory");
		// Removed unavailable agent
		expect(edited).not.toMatch(/80%|ALL required|MANDATORY|e2e-runner/);
	});

	test("security preserves secret validation, references common security, and removes security-reviewer while keeping audits optional", async () => {
		const { edited } = await tsRule("security");
		// Retain common-relative link
		expect(edited).toContain("[common/security.md](../common/security.md)");
		// Preserve environment-secret validation
		expect(edited).toContain('const apiKey = "sk-proj-xxxxx"');
		expect(edited).toContain("const apiKey = process.env.API_KEY");
		expect(edited).toContain("if (!apiKey)");
		expect(edited).toContain("throw new Error('API_KEY not configured')");
		// Reference included common security guidance
		expect(edited).toContain("rule://ecc-wp3-common-security");
		// Optional audit stays optional, including when a security issue is found
		expect(edited).toContain("An independent audit is explicitly optional, including when a security issue is found.");
		expect(edited).toContain("Finding an issue does not require an independent audit or expand task scope.");
		// Removed unavailable agent
		expect(edited).not.toContain("security-reviewer");
		expect(edited).not.toMatch(/compulsory|mandatory audit|STOP immediately/);
	});

	test("patterns rule requires no overlay and retains response, repository, and debounce examples", async () => {
		const patternsPath = path.join(eccRoot, "mirror/rules/typescript/patterns.md");
		const original = await Bun.file(patternsPath).text();
		expect(original).toContain("interface ApiResponse<T>");
		expect(original).toContain("useDebounce");
		expect(original).toContain("interface Repository<T>");
		// Metadata-only adaptation needs no overlay file
		const overlayPath = path.join(eccRoot, "developer/overlays/typescript-patterns.json");
		expect(await Bun.file(overlayPath).exists()).toBe(false);
	});
});
