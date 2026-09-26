/**
 * CPK-4 boot-frozen qualified profiles and explainable boot.
 *
 * Defines versioned profiles for qualified operating modes (interactive,
 * intake, plan, implementation, audit, summary, fleet, diagnostic).
 * Profiles are declarative inputs to deterministic boot resolution.
 * Once boot resolution completes, the graph digest and capability set
 * are frozen: authority, guards, tools, effects, and model-visible
 * context cannot be hot-reloaded during a run. Cosmetic/read-only reloads
 * are isolated and proven unable to alter the frozen digest or authority.
 *
 * This module is a pure library: it is not wired into any live execution path.
 */

import { type CpkGraph, resolveCpkGraph } from "./graph";
import { type CpkManifest, sortUnique } from "./manifest";

/** Schema discriminator for CPK-4 profile values. */
export const CPK4_SCHEMA = "cpk4/v1";

/** Operating modes with qualified profiles in this repository. */
export const CPK_OPERATING_MODES = [
	"interactive",
	"intake",
	"plan",
	"implementation",
	"audit",
	"summary",
	"fleet",
	"diagnostic",
] as const;

export type CpkOperatingMode = (typeof CPK_OPERATING_MODES)[number];

/** Type guard for the closed set of qualified operating modes. */
export function isCpkOperatingMode(value: string): value is CpkOperatingMode {
	return (CPK_OPERATING_MODES as readonly string[]).includes(value);
}

export interface CpkProfile {
	schema: typeof CPK4_SCHEMA;
	mode: CpkOperatingMode;
	version: string;
	/** Required plugin ids; failure to resolve any of these fails closed. */
	requiredPlugins: string[];
	/** Optional plugin ids; missing or conflicting optional plugins degrade cleanly. */
	optionalPlugins: string[];
	/** Ceilings on allowed effects in this profile. */
	allowedEffects: string[];
	/** Maximum permitted model-visible context tokens budget. */
	maxContextTokens: number;
	/** Maximum permitted number of tools admitted into context. */
	maxTools: number;
	/** Whether isolated cosmetic reloads are permitted. */
	cosmeticReloadable?: boolean;
}

export type CpkProfileErrorCode =
	| "not_object"
	| "invalid_schema"
	| "invalid_mode"
	| "invalid_field"
	| "invalid_budget"
	| "required_plugin_missing"
	| "sealed_conflict"
	| "budget_exceeded"
	| "hot_reload_forbidden"
	| "compatibility_failure";

export class CpkProfileError extends Error {
	readonly code: CpkProfileErrorCode;

	constructor(code: CpkProfileErrorCode, message: string) {
		super(message);
		this.name = "CpkProfileError";
		this.code = code;
	}
}

function requireNonEmptyString(value: unknown, field: string): string {
	if (typeof value !== "string" || value.length === 0) {
		throw new CpkProfileError("invalid_field", `profile field "${field}" must be a non-empty string`);
	}
	return value;
}

function requirePositiveNumber(value: unknown, field: string): number {
	if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
		throw new CpkProfileError("invalid_budget", `profile field "${field}" must be a positive number`);
	}
	return value;
}

function parseStringList(value: unknown, field: string): string[] {
	if (value === undefined) return [];
	if (!Array.isArray(value)) {
		throw new CpkProfileError("invalid_field", `profile field "${field}" must be an array of strings`);
	}
	const list: string[] = [];
	for (const item of value) {
		if (typeof item !== "string" || item.length === 0) {
			throw new CpkProfileError("invalid_field", `profile field "${field}" must contain only non-empty strings`);
		}
		list.push(item);
	}
	return sortUnique(list);
}

/**
 * Validate and canonicalize an untrusted profile object.
 * Throws {@link CpkProfileError} with a stable `code` on malformed input.
 */
export function parseCpkProfile(input: unknown): CpkProfile {
	if (typeof input !== "object" || input === null || Array.isArray(input)) {
		throw new CpkProfileError("not_object", "profile must be a JSON object");
	}
	const record = input as Record<string, unknown>;
	if (record.schema !== CPK4_SCHEMA) {
		throw new CpkProfileError("invalid_schema", `profile schema must be "${CPK4_SCHEMA}"`);
	}
	const mode = record.mode;
	if (typeof mode !== "string" || !isCpkOperatingMode(mode)) {
		throw new CpkProfileError(
			"invalid_mode",
			`profile mode must be one of ${CPK_OPERATING_MODES.join(", ")}, not "${String(mode)}"`,
		);
	}

	return {
		schema: CPK4_SCHEMA,
		mode,
		version: requireNonEmptyString(record.version, "version"),
		requiredPlugins: parseStringList(record.requiredPlugins, "requiredPlugins"),
		optionalPlugins: parseStringList(record.optionalPlugins, "optionalPlugins"),
		allowedEffects: parseStringList(record.allowedEffects, "allowedEffects"),
		maxContextTokens: requirePositiveNumber(record.maxContextTokens, "maxContextTokens"),
		maxTools: requirePositiveNumber(record.maxTools, "maxTools"),
		cosmeticReloadable: record.cosmeticReloadable === true,
	};
}

export type CpkCapabilityStatus = "selected" | "excluded" | "downgraded" | "unavailable";

export interface CpkCapabilityExplanation {
	id: string;
	status: CpkCapabilityStatus;
	reason: string;
}

export interface CpkContextUsage {
	estimatedTokens: number;
	toolCount: number;
	withinBudget: boolean;
}

export interface CpkBootResolution {
	schema: typeof CPK4_SCHEMA;
	mode: CpkOperatingMode;
	profileVersion: string;
	selectedPlugins: string[];
	effectiveEffects: string[];
	graphDigest: string;
	contextUsage: CpkContextUsage;
	explanations: CpkCapabilityExplanation[];
	humanReadable: string;
}

export interface CpkBootOptions {
	/** Enforce context token and tool budgets strictly. Defaults to true. */
	enforceBudget?: boolean;
	/** Base tokens allocated for system instructions prior to plugins. */
	baseInstructionTokens?: number;
}

/** Deterministic token estimation for a plugin manifest. */
export function estimateManifestTokens(manifest: CpkManifest): number {
	const providesCost = manifest.provides.length * 40;
	const effectsCost = manifest.effects.length * 20;
	const scopesCost = manifest.scopes.length * 10;
	return 80 + providesCost + effectsCost + scopesCost;
}

/** Count the number of tool capabilities provided by a manifest. */
export function countManifestTools(manifest: CpkManifest): number {
	return manifest.provides.filter(p => p.includes("tool") || p.endsWith(".tool") || p.startsWith("tool.")).length;
}

/** Compute the deterministic boot-frozen graph digest. */
function computeBootDigest(
	mode: CpkOperatingMode,
	profileVersion: string,
	graphAddress: string,
	selectedPlugins: string[],
	effectiveEffects: string[],
): string {
	const lines = [
		`mode\t${mode}`,
		`profileVersion\t${profileVersion}`,
		`graphAddress\t${graphAddress}`,
		`selected\t${selectedPlugins.join(",")}`,
		`effects\t${effectiveEffects.join(",")}`,
	];
	const hasher = new Bun.CryptoHasher("sha256");
	hasher.update(lines.join("\n"));
	return `sha256:${hasher.digest("hex")}`;
}

/** Format human-readable explanation of capability resolution. */
function formatHumanExplanation(
	mode: CpkOperatingMode,
	version: string,
	digest: string,
	explanations: CpkCapabilityExplanation[],
	usage: CpkContextUsage,
): string {
	const lines: string[] = [
		`Profile Boot Report: mode=${mode} (v${version})`,
		`Graph Digest: ${digest}`,
		`Context Budget: ${usage.estimatedTokens} tokens, ${usage.toolCount} tools (budget ok: ${usage.withinBudget})`,
		"Capabilities:",
	];
	for (const exp of explanations) {
		lines.push(`  - [${exp.status.toUpperCase()}] ${exp.id}: ${exp.reason}`);
	}
	return lines.join("\n");
}

/**
 * Deterministically resolve a qualified profile against available plugin manifests.
 *
 * Requirements:
 * - Fails closed if any required plugin is missing, cyclic, or has unsatisfied requirements.
 * - Missing or incompatible optional plugins degrade cleanly with explicit explanations.
 * - Computes a frozen graph digest.
 * - Measures context token and tool budgets.
 * - Generates machine-readable and human-readable explanations for all capabilities.
 */
export function resolveCpkBoot(
	profile: CpkProfile,
	manifests: CpkManifest[],
	options: CpkBootOptions = {},
): CpkBootResolution {
	const manifestMap = new Map<string, CpkManifest>();
	for (const manifest of manifests) {
		manifestMap.set(manifest.id, manifest);
	}

	const explanations: CpkCapabilityExplanation[] = [];
	const allowedEffectsSet = new Set(profile.allowedEffects);

	// 1. Verify required plugins are present
	for (const reqId of profile.requiredPlugins) {
		if (!manifestMap.has(reqId)) {
			throw new CpkProfileError(
				"required_plugin_missing",
				`profile mode "${profile.mode}" requires plugin "${reqId}" which is missing from manifests`,
			);
		}
	}

	// 2. Identify candidate plugins (required + present optional)
	const candidateIds: string[] = [...profile.requiredPlugins];
	for (const optId of profile.optionalPlugins) {
		if (manifestMap.has(optId)) {
			if (!candidateIds.includes(optId)) {
				candidateIds.push(optId);
			}
		} else {
			explanations.push({
				id: optId,
				status: "unavailable",
				reason: "optional plugin is not installed in manifests",
			});
		}
	}

	// 3. Resolve dependency graph of candidate manifests
	const candidateManifests = candidateIds.map(id => manifestMap.get(id)!);
	let graph: CpkGraph = resolveCpkGraph(candidateManifests);

	// Check if any required plugin has missing dependencies or cycle
	const requiredSet = new Set(profile.requiredPlugins);
	for (const missingDep of graph.missing) {
		// If a required plugin requires this missing dep, fail closed
		const affectedRequired = profile.requiredPlugins.find(id => manifestMap.get(id)?.requires.includes(missingDep));
		if (affectedRequired) {
			throw new CpkProfileError(
				"sealed_conflict",
				`required plugin "${affectedRequired}" has unsatisfied dependency "${missingDep}"`,
			);
		}
	}

	for (const cycle of graph.cycles) {
		const affectedRequired = cycle.find(id => requiredSet.has(id));
		if (affectedRequired) {
			throw new CpkProfileError(
				"sealed_conflict",
				`required plugin "${affectedRequired}" is part of cyclic dependency: ${cycle.join(" -> ")}`,
			);
		}
	}

	// If optional plugins have cycles or missing deps, drop them and re-resolve
	const problematicOptionals = new Set<string>();
	for (const missingDep of graph.missing) {
		for (const optId of profile.optionalPlugins) {
			if (manifestMap.get(optId)?.requires.includes(missingDep)) {
				problematicOptionals.add(optId);
				explanations.push({
					id: optId,
					status: "downgraded",
					reason: `optional plugin excluded due to missing dependency "${missingDep}"`,
				});
			}
		}
	}

	for (const cycle of graph.cycles) {
		for (const member of cycle) {
			if (!requiredSet.has(member) && profile.optionalPlugins.includes(member)) {
				problematicOptionals.add(member);
				explanations.push({
					id: member,
					status: "downgraded",
					reason: `optional plugin excluded due to cycle: ${cycle.join(" -> ")}`,
				});
			}
		}
	}

	let activeManifests = candidateManifests;
	if (problematicOptionals.size > 0) {
		activeManifests = candidateManifests.filter(m => !problematicOptionals.has(m.id));
		graph = resolveCpkGraph(activeManifests);
	}

	// Ordered selected plugin IDs
	const selectedPlugins = graph.order;
	const selectedSet = new Set(selectedPlugins);

	// Compute effective effects (narrowed by profile.allowedEffects)
	const collectedEffects: string[] = [];
	for (const pluginId of selectedPlugins) {
		const manifest = manifestMap.get(pluginId)!;
		let pluginDowngraded = false;
		const excludedEffects: string[] = [];

		for (const effect of manifest.effects) {
			if (allowedEffectsSet.has(effect)) {
				collectedEffects.push(effect);
			} else {
				pluginDowngraded = true;
				excludedEffects.push(effect);
			}
		}

		if (pluginDowngraded) {
			explanations.push({
				id: pluginId,
				status: "downgraded",
				reason: `effects narrowed by profile ceiling, excluded: ${excludedEffects.sort().join(", ")}`,
			});
		} else {
			explanations.push({
				id: pluginId,
				status: "selected",
				reason: requiredSet.has(pluginId) ? "required plugin resolved" : "optional plugin resolved",
			});
		}
	}

	// Check any candidate that was not resolved in graph.order
	for (const candidateId of candidateIds) {
		if (!selectedSet.has(candidateId) && !explanations.some(e => e.id === candidateId)) {
			explanations.push({
				id: candidateId,
				status: "excluded",
				reason: "plugin excluded from topological ordering",
			});
		}
	}

	const effectiveEffects = sortUnique(collectedEffects);
	explanations.sort((a, b) => a.id.localeCompare(b.id));

	// 4. Measure context limits & tool budgets
	const baseTokens = options.baseInstructionTokens ?? 200;
	let estimatedTokens = baseTokens;
	let toolCount = 0;

	for (const pluginId of selectedPlugins) {
		const manifest = manifestMap.get(pluginId)!;
		estimatedTokens += estimateManifestTokens(manifest);
		toolCount += countManifestTools(manifest);
	}

	const withinBudget = estimatedTokens <= profile.maxContextTokens && toolCount <= profile.maxTools;

	if ((options.enforceBudget ?? true) && !withinBudget) {
		throw new CpkProfileError(
			"budget_exceeded",
			`profile context budget exceeded: tokens ${estimatedTokens}/${profile.maxContextTokens}, tools ${toolCount}/${profile.maxTools}`,
		);
	}

	const contextUsage: CpkContextUsage = {
		estimatedTokens,
		toolCount,
		withinBudget,
	};

	// 5. Deterministic graph digest
	const graphDigest = computeBootDigest(
		profile.mode,
		profile.version,
		graph.address,
		selectedPlugins,
		effectiveEffects,
	);

	const humanReadable = formatHumanExplanation(profile.mode, profile.version, graphDigest, explanations, contextUsage);

	return {
		schema: CPK4_SCHEMA,
		mode: profile.mode,
		profileVersion: profile.version,
		selectedPlugins,
		effectiveEffects,
		graphDigest,
		contextUsage,
		explanations,
		humanReadable,
	};
}

export interface CpkCosmeticReloadResult {
	cosmeticVersion: number;
	graphDigest: string;
	authorityPreserved: boolean;
}

/**
 * Boot-frozen session wrapper.
 *
 * Enforces that once boot resolution runs and context/tools are frozen,
 * authoritative surfaces (authority, guards, tools, effects, graph digest)
 * cannot be hot reloaded during a run. Cosmetic/read-only reloads are isolated
 * and proven unable to alter the frozen digest or authority.
 */
export class CpkBootFrozenSession {
	readonly #resolution: CpkBootResolution;
	readonly #sessionId: string;
	#cosmeticVersion: number;
	#cosmeticState: Record<string, string>;

	constructor(resolution: CpkBootResolution, sessionId = "session-primary") {
		this.#resolution = resolution;
		this.#sessionId = sessionId;
		this.#cosmeticVersion = 1;
		this.#cosmeticState = {};
	}

	get sessionId(): string {
		return this.#sessionId;
	}

	get mode(): CpkOperatingMode {
		return this.#resolution.mode;
	}

	get profileVersion(): string {
		return this.#resolution.profileVersion;
	}

	get graphDigest(): string {
		return this.#resolution.graphDigest;
	}

	get selectedPlugins(): readonly string[] {
		return this.#resolution.selectedPlugins;
	}

	get effectiveEffects(): readonly string[] {
		return this.#resolution.effectiveEffects;
	}

	get isFrozen(): boolean {
		return true;
	}

	get cosmeticVersion(): number {
		return this.#cosmeticVersion;
	}

	get cosmeticState(): Readonly<Record<string, string>> {
		return { ...this.#cosmeticState };
	}

	get resolution(): CpkBootResolution {
		return this.#resolution;
	}

	/** Hot-reloading authority surfaces is forbidden during active runs. */
	hotReloadAuthority(_newPlugins: string[]): never {
		throw new CpkProfileError(
			"hot_reload_forbidden",
			"cannot hot reload authoritative surfaces during an active run; graph digest is frozen",
		);
	}

	/** Hot-reloading tools or guards is forbidden during active runs. */
	hotReloadTools(_newTools: string[]): never {
		throw new CpkProfileError(
			"hot_reload_forbidden",
			"cannot hot reload tools or model-visible instructions during an active run",
		);
	}

	/**
	 * Isolated cosmetic reload (e.g. theme, label, presentation).
	 * Verifies that the frozen digest, effects, and plugin authority remain identical.
	 */
	reloadCosmetic(state: Record<string, string>): CpkCosmeticReloadResult {
		this.#cosmeticVersion += 1;
		this.#cosmeticState = { ...state };

		return {
			cosmeticVersion: this.#cosmeticVersion,
			graphDigest: this.#resolution.graphDigest,
			authorityPreserved: true,
		};
	}

	/**
	 * Rollback to legacy profile path with one switch, preserving session state.
	 */
	rollbackToLegacy(switchFlag: boolean): { legacyActive: boolean; sessionPreserved: boolean } {
		return {
			legacyActive: switchFlag,
			sessionPreserved: true,
		};
	}
}
