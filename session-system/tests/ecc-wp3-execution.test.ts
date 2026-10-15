import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { ECC_ROOT, loadManifest } from "../ecc/adapter/catalog";
import { sha256Hex } from "../ecc/adapter/hash";
import { applyOverlay, loadOverlay } from "../ecc/adapter/overlay";
import type { ManifestAsset } from "../ecc/adapter/types";

const SKILLS = [
	"verification-loop",
	"iterative-retrieval",
	"dynamic-workflow-mode",
	"agentic-engineering",
	"tdd-workflow",
] as const;
type Skill = (typeof SKILLS)[number];

const assets = (await Bun.file(path.join(ECC_ROOT, "developer/execution.json")).json()) as ManifestAsset[];
const manifest = await loadManifest();

async function render(skill: Skill): Promise<string> {
	const overlay = await loadOverlay(`developer/overlays/${skill}.json`, ECC_ROOT, `ecc-wp3-${skill}`);
	const source = await Bun.file(path.join(ECC_ROOT, "mirror", overlay.upstreamPath)).text();
	return applyOverlay(source, overlay, sha256Hex(source));
}

/** Native evidence, audit, completion, and lesson destinations each skill must keep. */
const RETAINED: Record<Skill, RegExp[]> = {
	"verification-loop": [
		/bun check/,
		/bun run test:rs/,
		/bun test <file>/,
		/append_evidence/,
		/run_audit/,
		/`\/done`/,
		/skill-observations\/log\.md/,
		/git diff --stat/,
	],
	"iterative-retrieval": [/Max 3 cycles/, /maxCycles = 3/, /relevance >= 0\.7/, /existing intake and plan records/],
	"dynamic-workflow-mode": [
		/Eval/,
		/append_evidence/,
		/run_audit/,
		/`\/done`/,
		/skill-observations\/log\.md/,
		/existing intake and plan records/,
	],
	"agentic-engineering": [/15-minute unit rule/, /effective assignment/, /run_audit/, /skill-observations\/log\.md/],
	"tdd-workflow": [
		/RED/,
		/GREEN/,
		/Refactor/,
		/bun check/,
		/bun run test:rs/,
		/append_evidence/,
		/`\/plan`/,
		/spyOn/,
		/skill-observations\/log\.md/,
	],
};

/** Mandates, targets, and references that must not survive adaptation. */
const FORBIDDEN: Array<[string, RegExp]> = [
	["numeric coverage target", /80\s*%|80\+|coverage/i],
	["forced browser/E2E", /\bE2E\b|Playwright/],
	["git checkpoint, commit, or push", /checkpoint commit|git (?:commit|push|add)\b|Pre-Commit|\bpush\b/i],
	["Claude config or helper path", /Claude|\.claude|CLAUDE_|setup-package-manager|ECC2|control pane/i],
	["provider model name", /\b(?:Haiku|Sonnet|Opus)\b/],
	["absent slash command", /\/(?:verify|tdd|e2e|code-review|build-fix|learn|skill-create)\b/],
	["separate report path", /\.tdd\.md|docs\/releases|\.github\/tdd/],
	["forced reviewer or gate", /merge gate|tdd-guide|code-reviewer|operator approval/],
	["unfetchable or absent reference", /https?:\/\/x\.com|continuous-learning|bun-runtime|\bnpx\b/],
];

const NATIVE_COMMANDS = new Set(manifest.nativeCommandNames);

function stripFences(text: string): string {
	return text.replace(/^\s*(```|~~~)[\s\S]*?^\s*\1/gm, "");
}

describe("ECC WP3 execution manifest", () => {
	test("pins each full SKILL.md source to the mirror bytes and the manifest upstream commit", async () => {
		expect(assets.map(asset => asset.id)).toEqual(SKILLS.map(skill => `ecc-wp3-${skill}`));
		for (const [index, asset] of assets.entries()) {
			const skill = SKILLS[index]!;
			const source = await Bun.file(path.join(ECC_ROOT, "mirror", asset.upstream.path)).text();
			expect(asset.upstream).toEqual({
				repository: manifest.upstream.repository,
				commit: manifest.upstream.commit,
				path: `skills/${skill}/SKILL.md`,
				sha256: sha256Hex(source),
			});
			const overlay = await loadOverlay(asset.adaptation.overlay!, ECC_ROOT, asset.id, asset.upstream.path);
			expect(overlay.upstreamSha256).toBe(asset.upstream.sha256);
		}
	});

	test("installs only as project rulebook rules with no always-on or TTSR activation", () => {
		for (const asset of assets) {
			expect(asset.adaptation.kind).toBe("rule-metadata");
			expect(asset.disposition).toBe("supported-with-adaptation");
			expect(asset.target).toEqual({ path: `.omp/rules/${asset.id}.md`, scope: "project" });
			expect(asset.activation.mode).toBe("rulebook");
			expect(asset.activation.description?.trim()).toBeTruthy();
			expect(JSON.stringify(asset)).not.toMatch(/alwaysApply|ttsr|globs/i);
		}
	});
});

describe("ECC WP3 execution overlays", () => {
	for (const skill of SKILLS) {
		test(`${skill} renders with native evidence destinations and no extra mandates`, async () => {
			const rendered = await render(skill);
			for (const pattern of RETAINED[skill]) expect(rendered).toMatch(pattern);
			for (const [label, pattern] of FORBIDDEN) {
				const hit = rendered.match(pattern);
				expect(hit === null ? null : `${label}: ${hit[0]}`).toBeNull();
			}
			const prose = stripFences(rendered);
			for (const match of prose.matchAll(/(?:^|[\s`(])\/([a-z][a-z-]*)\b/g)) {
				expect(NATIVE_COMMANDS.has(match[1]!)).toBe(true);
			}
		});

		test(`${skill} refuses a stale source`, async () => {
			const overlay = await loadOverlay(`developer/overlays/${skill}.json`, ECC_ROOT, `ecc-wp3-${skill}`);
			const source = await Bun.file(path.join(ECC_ROOT, "mirror", overlay.upstreamPath)).text();
			const stale = `${source}\nupstream drift\n`;
			expect(() => applyOverlay(stale, overlay, sha256Hex(stale))).toThrow(/upstream sha256 mismatch/);
			const first = overlay.edits[0]!.find.join("\n");
			const edited = source.replace(first, `${first} changed`);
			expect(() => applyOverlay(edited, overlay, overlay.upstreamSha256)).toThrow(/Overlay drift/);
		});
	}
});
