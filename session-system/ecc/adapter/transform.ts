/**
 * Deterministic content transforms for the ECC adapter.
 *
 * Every transform is a pure function of (upstream bytes, manifest record,
 * overlay). No filesystem, git, or network access happens here — the mirror is
 * read by the caller and passed in — so the same input bytes always produce the
 * same output bytes and the same sha256 recorded in adapted.lock.json.
 */
import * as path from "node:path";
import { parseFrontmatter } from "@oh-my-pi/pi-utils";
import { sha256Hex } from "./hash";
import { applyOverlay, type Overlay } from "./overlay";
import type { ManifestAsset, PlannedFile } from "./types";
import { buildInstallIndex, type InstallIndex, rewriteRelativeLinks } from "./vendor/link-rewrite.js";

export interface TransformResult {
	/** Files this asset installs, relative to the install root. */
	files: PlannedFile[];
	adaptiveFile: string;
	adaptedSha256: string;
}

/** Tool names a rule or agent may still request after OMP adaptation. */
const READ_ONLY_TOOLS = new Set(["read", "grep", "glob"]);

function decode(bytes: Uint8Array): string {
	return new TextDecoder("utf-8").decode(bytes);
}

function encode(text: string): Uint8Array {
	return new TextEncoder().encode(text);
}

export function isSkillKind(kind: ManifestAsset["adaptation"]["kind"]): boolean {
	return kind === "skill-namespaced";
}

const INLINE_LINK_PATTERN = /!?\]\(([^()\s]+)/g;

/**
 * Verify every internal relative markdown link in adapted content resolves to a
 * file or directory the plan actually installs (or that the skill ships itself).
 * Throws the naming unmet reference — the closure test asserts this failure.
 */
export function verifyReferenceClosure(
	content: string,
	sourceRel: string,
	index: InstallIndex,
	extraMembers: Iterable<string> = [],
): void {
	const sourceDir = path.posix.dirname(sourceRel);
	const members = new Set(extraMembers);
	const lines = content.split("\n");
	let inFence = false;
	for (let lineNo = 0; lineNo < lines.length; lineNo++) {
		const line = lines[lineNo];
		if (/^\s*(```|~~~)/.test(line)) {
			inFence = !inFence;
			continue;
		}
		if (inFence) continue;
		for (const match of line.matchAll(INLINE_LINK_PATTERN)) {
			const target = match[1];
			const hash = target.indexOf("#");
			const pathPart = hash === -1 ? target : target.slice(0, hash);
			if (
				pathPart === "" ||
				pathPart.startsWith("#") ||
				pathPart.startsWith("/") ||
				pathPart.startsWith("mailto:") ||
				/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(pathPart)
			) {
				continue;
			}
			const resolved = path.posix.normalize(path.posix.join(sourceDir, pathPart));
			if (resolved.startsWith("..") || resolved === "." || resolved === "") {
				throw new Error(`Reference closure error: link '${target}' in ${sourceRel} escapes the mirror root`);
			}
			const trimmed = resolved.endsWith("/") ? resolved.slice(0, -1) : resolved;
			if (members.has(trimmed) || index.byFile.has(trimmed) || index.byDir.has(trimmed)) continue;
			throw new Error(
				`Reference closure error: link '${target}' in ${sourceRel} resolves to uninstalled path '${trimmed}'`,
			);
		}
	}
}

/**
 * Build the link-rewriting index from a manifest's mapped assets so a relative
 * link in one asset resolves to the installed location of its target asset.
 */
export function buildIndexFromManifest(assets: ManifestAsset[]): InstallIndex {
	const mappings = assets.map(asset => {
		let sourceRel = asset.upstream.path;
		let destRel = asset.target.path.replace(/^~\//, "");
		if (isSkillKind(asset.adaptation.kind)) {
			if (!sourceRel.endsWith(".md")) sourceRel = path.posix.join(sourceRel, "SKILL.md");
			if (!destRel.endsWith(".md")) destRel = path.posix.join(destRel, "SKILL.md");
		}
		return { sourceRel, destRel };
	});
	return buildInstallIndex(mappings);
}

/**
 * Emit YAML frontmatter with a fixed key order, so byte output is stable across
 * runs and platforms.
 */
function emitFrontmatter(fields: Record<string, string | string[] | boolean | undefined>, order: string[]): string {
	const lines = ["---"];
	for (const key of order) {
		const value = fields[key];
		if (value === undefined) continue;
		if (typeof value === "boolean") {
			lines.push(`${key}: ${value}`);
			continue;
		}
		if (typeof value === "string") {
			lines.push(value.includes("\n") ? `${key}: ${JSON.stringify(value)}` : `${key}: ${quoteIfNeeded(value)}`);
			continue;
		}
		if (value.length === 0) {
			lines.push(`${key}: []`);
			continue;
		}
		lines.push(`${key}:`);
		for (const item of value) lines.push(`  - ${quoteIfNeeded(item)}`);
	}
	lines.push("---");
	return lines.join("\n");
}

const YAML_AMBIGUOUS = /[*?:#&!|>%@`{}[\],"'\\]|^\s|\s$|^$/;

function quoteIfNeeded(value: string): string {
	return YAML_AMBIGUOUS.test(value) ? JSON.stringify(value) : value;
}

/** Keep only read-only tool names, preserving order and dropping duplicates. */
function mappedReadOnlyTools(frontmatter: Record<string, unknown>): string[] {
	const raw = Array.isArray(frontmatter.tools)
		? frontmatter.tools.map(String)
		: typeof frontmatter.tools === "string"
			? frontmatter.tools.split(",")
			: [];
	const mapped: string[] = [];
	for (const tool of raw) {
		const lower = tool.trim().toLowerCase();
		if (READ_ONLY_TOOLS.has(lower) && !mapped.includes(lower)) mapped.push(lower);
	}
	return mapped;
}

function yamlLiteralBlock(value: string, indent: string): string {
	return value
		.trim()
		.split("\n")
		.map(line => `${indent}${line}`)
		.join("\n");
}

/**
 * The single line in the frontmatter declaring the skill name. The OMP name is
 * always `ecc-<upstream name>` so a pack skill can never claim a bare native
 * skill name; the upstream directory is not consulted because some upstream
 * skills intentionally name themselves differently from their directory.
 */
function rewriteFrontmatterName(record: ManifestAsset, content: string): string {
	const { frontmatter } = parseFrontmatter(content, { source: record.upstream.path });
	if (typeof frontmatter.name !== "string" || frontmatter.name.trim() === "") {
		throw new Error(`Namespace error for '${record.id}': missing frontmatter name`);
	}
	const name = frontmatter.name.trim();
	const block = content.match(/^---\r?\n([\s\S]*?)\r?\n---/);
	if (!block) throw new Error(`Namespace error for '${record.id}': missing frontmatter block`);
	const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
	const pattern = new RegExp(`^name:[ \\t]*${escaped}[ \\t]*$`, "m");
	const matches = block[0].match(new RegExp(`^name:[ \\t]*${escaped}[ \\t]*$`, "mg"));
	if (!matches || matches.length !== 1) {
		throw new Error(`Namespace error for '${record.id}': expected exactly one 'name: ${name}' line in frontmatter`);
	}
	return block[0].replace(pattern, `name: ecc-${name}`) + content.slice(block[0].length);
}

function namespacedSkillContent(record: ManifestAsset, content: string, overlay?: Overlay): string {
	const renamed = rewriteFrontmatterName(record, content);
	return overlay ? applyOverlay(renamed, overlay, sha256Hex(content)) : renamed;
}

/**
 * Transform one mirrored upstream asset into its OMP destination file(s).
 *
 * Kind semantics (ECC-IMPLEMENTATION-SPEC.md sections 4 and 6/WP2):
 *  - rule-metadata: ECC `paths:` frontmatter becomes OMP `globs:`/`description:`;
 *    body preserved and relative links rewritten to installed targets.
 *  - skill-namespaced: `name: <x>` becomes `name: ecc-<x>`; overlays translate
 *    Claude-only paths/tools; helper files are installed verbatim.
 *  - agent-metadata: an OMP read-only task agent (`ecc-` prefix, Bash dropped).
 *  - database-advisor-profile: a project WATCHDOG.yml roster entry with a static
 *    read-only policy and `enabled: false` (explicit opt-in only).
 */
export function transformAsset(
	record: ManifestAsset,
	upstreamBytes: Uint8Array,
	options: {
		index: InstallIndex;
		helperFiles?: Map<string, Uint8Array>;
		overlay?: Overlay;
		advisorPolicy?: string;
	},
): TransformResult {
	const { index, helperFiles, overlay, advisorPolicy } = options;
	const targetRel = record.target.path.replace(/^~\//, "");

	switch (record.adaptation.kind) {
		case "rule-metadata": {
			const content = decode(upstreamBytes);
			const { frontmatter, body } = parseFrontmatter(content, { source: record.upstream.path });
			verifyReferenceClosure(body, record.upstream.path, index);
			const rewritten = rewriteRelativeLinks(body, { sourceRel: record.upstream.path, index });

			let globs = record.activation.globs;
			if (!globs && Array.isArray(frontmatter.paths)) globs = frontmatter.paths.map(String);
			const description =
				record.activation.description ??
				(typeof frontmatter.description === "string" ? frontmatter.description : undefined);

			const bytes = encode(
				`${emitFrontmatter({ description, globs }, ["description", "globs"])}\n\n${rewritten.trimStart()}`,
			);
			return finish(record, [{ relativePath: targetRel, bytes }]);
		}

		case "skill-namespaced": {
			const adapted = namespacedSkillContent(record, decode(upstreamBytes), overlay);
			const skillRel = path.posix.join(targetRel, "SKILL.md");
			const helperPaths = record.dependencies.map(dep =>
				path.posix.join(path.posix.dirname(record.upstream.path), dep),
			);
			verifyReferenceClosure(adapted, record.upstream.path, index, helperPaths);
			const files: Array<{ relativePath: string; bytes: Uint8Array }> = [
				{ relativePath: skillRel, bytes: encode(adapted) },
			];
			for (const dep of record.dependencies) {
				const depBytes = helperFiles?.get(dep);
				if (!depBytes) {
					throw new Error(`Reference closure error: asset '${record.id}' is missing dependency '${dep}'`);
				}
				files.push({ relativePath: path.posix.join(targetRel, dep), bytes: depBytes });
			}
			return finish(record, files);
		}

		case "agent-metadata": {
			const content = decode(upstreamBytes);
			const { frontmatter, body } = parseFrontmatter(content, { source: record.upstream.path });
			const base = (typeof frontmatter.name === "string" && frontmatter.name) || record.id.replace(/^ecc-/, "");
			const description =
				(typeof frontmatter.description === "string" ? frontmatter.description : undefined) ??
				record.activation.description ??
				"";
			const frontmatterText = emitFrontmatter(
				{
					name: `ecc-${base}`,
					description,
					tools: mappedReadOnlyTools(frontmatter),
					enabled: false,
				},
				["name", "description", "tools", "enabled"],
			);
			return finish(record, [
				{ relativePath: targetRel, bytes: encode(`${frontmatterText}\n\n${body.trimStart()}`) },
			]);
		}

		case "database-advisor-profile": {
			const content = decode(upstreamBytes);
			const { frontmatter, body } = parseFrontmatter(content, { source: record.upstream.path });
			const tools = mappedReadOnlyTools(frontmatter);
			if (tools.length === 0) {
				throw new Error(`Advisor profile '${record.id}' has no permitted read-only tools`);
			}
			if (!overlay) throw new Error(`Advisor profile '${record.id}' requires a safety overlay`);
			if (!advisorPolicy) throw new Error(`Advisor profile '${record.id}' requires the read-only policy`);
			const adaptedBody = applyOverlay(body.trim(), overlay, sha256Hex(upstreamBytes));
			const instructions = `${advisorPolicy.trim()}\n\n${adaptedBody}`;
			const lines = [
				"advisors:",
				"  - name: ECC Database Reviewer",
				"    tools:",
				...tools.map(tool => `      - ${tool}`),
				"    enabled: false",
				"    instructions: |-",
				yamlLiteralBlock(instructions, "      "),
			];
			return finish(record, [{ relativePath: targetRel, bytes: encode(`${lines.join("\n")}\n`) }]);
		}

		default: {
			const exhaustive: never = record.adaptation.kind;
			throw new Error(`Unsupported adaptation kind: ${String(exhaustive)}`);
		}
	}
}

function finish(record: ManifestAsset, files: Array<{ relativePath: string; bytes: Uint8Array }>): TransformResult {
	const planned: PlannedFile[] = files.map(file => ({
		path: file.relativePath,
		bytes: file.bytes,
		sha256: sha256Hex(file.bytes),
		mode: "644",
		assetId: record.id,
	}));
	const primary = planned.find(file => file.path.endsWith("SKILL.md")) ?? planned[0];
	return { files: planned, adaptiveFile: primary.path, adaptedSha256: primary.sha256 };
}
