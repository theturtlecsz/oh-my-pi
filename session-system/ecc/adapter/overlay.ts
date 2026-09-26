/**
 * Reviewed semantic overlays.
 *
 * An overlay is a small, human-reviewed patch applied after the deterministic
 * metadata transform. It is pinned to both the upstream and the adapted sha256
 * so any drift (upstream edit, or a transform change that shifts the adapted
 * bytes) fails loudly instead of silently shipping an unreviewed edit.
 */
import * as fs from "node:fs/promises";
import * as path from "node:path";
import { sha256Hex } from "./hash";
import type { Overlay, OverlayEdit } from "./types";

export type { Overlay, OverlayEdit } from "./types";

/** Validate overlay schema, containment, and optional identity agreement. */
export function validateOverlay(raw: unknown, expectedAssetId?: string, expectedUpstreamPath?: string): Overlay {
	if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
		throw new Error("Overlay must be a JSON object");
	}
	const obj = raw as Record<string, unknown>;
	if (obj.schemaVersion !== 1) {
		throw new Error(`Overlay schemaVersion must be 1, got ${JSON.stringify(obj.schemaVersion)}`);
	}
	if (typeof obj.assetId !== "string" || obj.assetId.trim() === "") {
		throw new Error("Overlay assetId must be a non-empty string");
	}
	if (expectedAssetId !== undefined && obj.assetId !== expectedAssetId) {
		throw new Error(`Overlay assetId mismatch: expected '${expectedAssetId}', got '${obj.assetId}'`);
	}
	if (typeof obj.upstreamPath !== "string" || obj.upstreamPath.trim() === "") {
		throw new Error("Overlay upstreamPath must be a non-empty string");
	}
	const normalizedPath = path.posix.normalize(obj.upstreamPath);
	if (
		path.isAbsolute(obj.upstreamPath) ||
		obj.upstreamPath.startsWith("/") ||
		obj.upstreamPath.startsWith("\\") ||
		normalizedPath.startsWith("..") ||
		normalizedPath === "."
	) {
		throw new Error(`Overlay upstreamPath must be contained, got '${obj.upstreamPath}'`);
	}
	if (expectedUpstreamPath !== undefined && normalizedPath !== path.posix.normalize(expectedUpstreamPath)) {
		throw new Error(`Overlay upstreamPath mismatch: expected '${path.posix.normalize(expectedUpstreamPath)}'`);
	}
	const upstreamSha256 = obj.upstreamSha256;
	const adaptedSha256 = obj.adaptedSha256;
	for (const [key, value] of [
		["upstreamSha256", upstreamSha256],
		["adaptedSha256", adaptedSha256],
	] as const) {
		if (typeof value !== "string" || !/^[0-9a-f]{64}$/i.test(value)) {
			throw new Error(`Overlay ${key} must be a 64-character hex SHA-256, got ${JSON.stringify(value)}`);
		}
	}
	if (!Array.isArray(obj.edits) || obj.edits.length === 0) {
		throw new Error("Overlay edits must be a non-empty array");
	}
	const edits: OverlayEdit[] = obj.edits.map((entry, index) => {
		if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
			throw new Error(`Overlay edit at index ${index} must be an object`);
		}
		const edit = entry as Record<string, unknown>;
		if (typeof edit.reason !== "string" || edit.reason.trim() === "") {
			throw new Error(`Overlay edit at index ${index} missing non-empty reason`);
		}
		if (!Array.isArray(edit.find) || edit.find.length === 0 || !edit.find.every(l => typeof l === "string")) {
			throw new Error(`Overlay edit '${edit.reason}': find must be a non-empty array of strings`);
		}
		if (!Array.isArray(edit.replace) || !edit.replace.every(l => typeof l === "string")) {
			throw new Error(`Overlay edit '${edit.reason}': replace must be an array of strings`);
		}
		return { reason: edit.reason, find: edit.find as string[], replace: edit.replace as string[] };
	});
	return {
		schemaVersion: 1,
		assetId: obj.assetId,
		upstreamPath: normalizedPath,
		upstreamSha256: (upstreamSha256 as string).toLowerCase(),
		adaptedSha256: (adaptedSha256 as string).toLowerCase(),
		edits,
	};
}

/** Load and validate an overlay JSON file relative to the ECC root. */
export async function loadOverlay(
	relPath: string,
	eccRoot: string,
	expectedAssetId?: string,
	expectedUpstreamPath?: string,
): Promise<Overlay> {
	if (path.isAbsolute(relPath) || relPath.startsWith("/") || relPath.startsWith("\\")) {
		throw new Error(`Overlay path must be relative to the ECC root, got '${relPath}'`);
	}
	const resolvedRoot = await fs.realpath(eccRoot);
	const fullPath = path.resolve(resolvedRoot, relPath);
	const realFile = await fs.realpath(fullPath);
	const relative = path.relative(resolvedRoot, realFile);
	if (relative.startsWith("..") || path.isAbsolute(relative)) {
		throw new Error(`Overlay path '${relPath}' resolves outside the ECC root`);
	}
	let parsed: unknown;
	try {
		parsed = JSON.parse(await Bun.file(realFile).text());
	} catch (err) {
		throw new Error(`Overlay '${relPath}' is not valid JSON: ${err instanceof Error ? err.message : String(err)}`);
	}
	return validateOverlay(parsed, expectedAssetId, expectedUpstreamPath);
}

/**
 * Apply overlay edits in order.
 *
 * Throws on drift when: the upstream sha256 does not match, any find block
 * matches zero or more than one time, or the resulting text hashes to something
 * other than the pinned adapted sha256.
 */
export function applyOverlay(text: string, overlay: Overlay, actualUpstreamSha256: string): string {
	if (overlay.upstreamSha256 !== actualUpstreamSha256) {
		throw new Error(
			`Overlay drift for '${overlay.assetId}': upstream sha256 mismatch (expected ${overlay.upstreamSha256}, got ${actualUpstreamSha256})`,
		);
	}
	let current = text;
	for (let i = 0; i < overlay.edits.length; i++) {
		const edit = overlay.edits[i];
		const findBlock = edit.find.join("\n");
		const replaceBlock = edit.replace.join("\n");
		const first = current.indexOf(findBlock);
		if (first === -1) {
			throw new Error(`Overlay drift for '${overlay.assetId}': edit ${i} (${edit.reason}) find block not found`);
		}
		if (current.indexOf(findBlock, first + findBlock.length) !== -1) {
			throw new Error(
				`Overlay drift for '${overlay.assetId}': edit ${i} (${edit.reason}) find block matches multiple times`,
			);
		}
		current = current.slice(0, first) + replaceBlock + current.slice(first + findBlock.length);
	}
	const computed = sha256Hex(current);
	if (overlay.adaptedSha256 !== computed) {
		throw new Error(
			`Overlay drift for '${overlay.assetId}': adapted sha256 mismatch (expected ${overlay.adaptedSha256}, got ${computed})`,
		);
	}
	return current;
}
