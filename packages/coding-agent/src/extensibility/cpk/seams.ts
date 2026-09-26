/**
 * CPK-3 typed event seams.
 *
 * CPK-3 partitions everything crossing a plugin/extension seam into four typed
 * event classes. Exactly one class — `committed` — carries authority: it
 * records a fact a native owner has already committed. The other three
 * (`proposal`, `signal`, `diagnostic`) are advisory and are the only classes the
 * event bus carries, so a plugin can never change lifecycle state by emitting an
 * event.
 *
 * Lifecycle transitions are therefore not events at all: they are direct typed
 * calls to exactly one native authority (the WorkService or a native session).
 * This module validates that ownership, and the finite single-provider
 * capability RPC that fronts the seam.
 *
 * This module is a pure library: it is not wired into any live execution path.
 */

/** Schema discriminator for CPK-3 seam values. */
export const CPK3_SCHEMA = "cpk3/v1";

/** CPK-3 event classes, ordered from authoritative to advisory. */
export const CPK_EVENT_CLASSES = ["committed", "proposal", "signal", "diagnostic"] as const;

export type CpkEventClass = (typeof CPK_EVENT_CLASSES)[number];

/**
 * Event classes that carry authority. A committed fact is the only class that
 * may be read as truth, and it never travels over the event bus.
 */
export const CPK_AUTHORITATIVE_EVENT_CLASSES = ["committed"] as const;

/**
 * Native transition authorities. Lifecycle state changes only here, by direct
 * typed call — never through an event, bus, or generic dispatch mechanism.
 */
export const CPK_TRANSITION_OWNERS = ["workservice", "session"] as const;

export type CpkTransitionOwner = (typeof CPK_TRANSITION_OWNERS)[number];

export type CpkSeamErrorCode =
	| "not_object"
	| "invalid_schema"
	| "invalid_class"
	| "invalid_field"
	| "authoritative_on_bus"
	| "duplicate_provider"
	| "unknown_capability"
	| "unknown_owner"
	| "event_routed_transition"
	| "unknown_stage"
	| "duplicate_stage"
	| "missing_stage"
	| "disordered_pipeline"
	| "non_monotonic";

export class CpkSeamError extends Error {
	readonly code: CpkSeamErrorCode;

	constructor(code: CpkSeamErrorCode, message: string) {
		super(message);
		this.name = "CpkSeamError";
		this.code = code;
	}
}

/** Type guard for the closed set of CPK-3 event classes. */
export function isCpkEventClass(value: string): value is CpkEventClass {
	return (CPK_EVENT_CLASSES as readonly string[]).includes(value);
}

/** True when the event class carries authority and must bypass the bus. */
export function isAuthoritativeEventClass(value: string): boolean {
	return (CPK_AUTHORITATIVE_EVENT_CLASSES as readonly string[]).includes(value);
}

/** Type guard for the closed set of native transition authorities. */
export function isCpkTransitionOwner(value: string): value is CpkTransitionOwner {
	return (CPK_TRANSITION_OWNERS as readonly string[]).includes(value);
}

export interface CpkSeamEvent<T = unknown> {
	schema: typeof CPK3_SCHEMA;
	class: CpkEventClass;
	/** Capability id that produced the event. */
	capability: string;
	payload: T;
}

function requireNonEmptyString(value: unknown, field: string): string {
	if (typeof value !== "string" || value.length === 0) {
		throw new CpkSeamError("invalid_field", `field "${field}" must be a non-empty string`);
	}
	return value;
}

/**
 * Validate and canonicalize an untrusted seam event. Throws {@link CpkSeamError}
 * with a stable `code` on malformed input.
 */
export function parseCpkSeamEvent(input: unknown): CpkSeamEvent {
	if (typeof input !== "object" || input === null || Array.isArray(input)) {
		throw new CpkSeamError("not_object", "seam event must be a JSON object");
	}
	const record = input as Record<string, unknown>;
	if (record.schema !== CPK3_SCHEMA) {
		throw new CpkSeamError("invalid_schema", `seam event schema must be "${CPK3_SCHEMA}"`);
	}
	if (typeof record.class !== "string" || !isCpkEventClass(record.class)) {
		throw new CpkSeamError("invalid_class", `seam event class must be one of ${CPK_EVENT_CLASSES.join(", ")}`);
	}
	return {
		schema: CPK3_SCHEMA,
		class: record.class,
		capability: requireNonEmptyString(record.capability, "capability"),
		payload: record.payload,
	};
}

/**
 * Refuse to route an authoritative event over the bus. A bus that accepted
 * `committed` facts would let a plugin manufacture authority, so emission is
 * rejected outright rather than filtered after the fact.
 */
export function assertNonAuthoritativeOnBus(event: CpkSeamEvent): void {
	if (isAuthoritativeEventClass(event.class)) {
		throw new CpkSeamError(
			"authoritative_on_bus",
			`event class "${event.class}" carries authority and cannot be routed through the bus`,
		);
	}
}

/**
 * Typed event bus restricted to non-authoritative event classes. It is the only
 * generic routing surface plugins get; native authorities are reached by direct
 * call, not through here.
 */
export class CpkTypedEventBus {
	readonly #handlers = new Map<CpkEventClass, Set<(event: CpkSeamEvent) => void>>();

	emit(event: CpkSeamEvent): void {
		assertNonAuthoritativeOnBus(event);
		const handlers = this.#handlers.get(event.class);
		if (!handlers) return;
		for (const handler of handlers) handler(event);
	}

	on(cls: CpkEventClass, handler: (event: CpkSeamEvent) => void): () => void {
		if (isAuthoritativeEventClass(cls)) {
			throw new CpkSeamError(
				"authoritative_on_bus",
				`event class "${cls}" carries authority and cannot be subscribed on the bus`,
			);
		}
		let handlers = this.#handlers.get(cls);
		if (!handlers) {
			handlers = new Set();
			this.#handlers.set(cls, handlers);
		}
		handlers.add(handler);
		return () => handlers.delete(handler);
	}
}

export interface CpkCapabilityRegistration {
	capability: string;
	/** The single owner registered to provide this capability. */
	provider: string;
}

export type CpkCapabilityViolationCode = "unknown_capability" | "duplicate_provider";

export interface CpkCapabilityViolation {
	code: CpkCapabilityViolationCode;
	capability: string;
	message: string;
}

/**
 * Validate a finite capability table. Every registration must target a declared
 * capability, and a capability may have at most one provider.
 *
 * Returns one violation per offending registration, sorted by capability. An
 * empty array means the table is a valid single-provider surface.
 */
export function validateCapabilityTable(
	declared: readonly string[],
	registrations: readonly CpkCapabilityRegistration[],
): CpkCapabilityViolation[] {
	const declarable = new Set(declared);
	const providers = new Map<string, string>();
	const violations: CpkCapabilityViolation[] = [];

	for (const registration of registrations) {
		if (!declarable.has(registration.capability)) {
			violations.push({
				code: "unknown_capability",
				capability: registration.capability,
				message: `capability "${registration.capability}" is not declared on this seam`,
			});
			continue;
		}
		const existing = providers.get(registration.capability);
		if (existing === undefined) {
			providers.set(registration.capability, registration.provider);
			continue;
		}
		if (existing !== registration.provider) {
			violations.push({
				code: "duplicate_provider",
				capability: registration.capability,
				message: `capability "${registration.capability}" already has provider "${existing}", not "${registration.provider}"`,
			});
		}
	}

	return violations.sort((a, b) =>
		a.capability === b.capability ? a.code.localeCompare(b.code) : a.capability.localeCompare(b.capability),
	);
}

/**
 * Finite single-provider capability RPC registry. Registration refuses a
 * capability outside the declared set or a second provider for the same
 * capability; resolution refuses a capability with no provider.
 */
export class CpkCapabilityRegistry {
	readonly #declared: Set<string>;
	readonly #providers = new Map<string, string>();

	constructor(declared: readonly string[]) {
		this.#declared = new Set(declared);
	}

	register(capability: string, provider: string): void {
		if (!this.#declared.has(capability)) {
			throw new CpkSeamError("unknown_capability", `capability "${capability}" is not declared on this seam`);
		}
		const existing = this.#providers.get(capability);
		if (existing !== undefined && existing !== provider) {
			throw new CpkSeamError(
				"duplicate_provider",
				`capability "${capability}" already has provider "${existing}", not "${provider}"`,
			);
		}
		this.#providers.set(capability, provider);
	}

	/** Resolve the single provider for a capability, refusing an unregistered RPC. */
	provider(capability: string): string {
		const provider = this.#providers.get(capability);
		if (provider === undefined) {
			throw new CpkSeamError("unknown_capability", `capability "${capability}" has no registered provider`);
		}
		return provider;
	}
}

export interface CpkTransition {
	/** The single native authority that must perform the transition. */
	owner: CpkTransitionOwner;
	/** Lifecycle field the transition targets, e.g. `state`. */
	field: string;
	/** The committed target value. */
	target: string;
}

/**
 * Validate and canonicalize an untrusted transition. Throws {@link CpkSeamError}
 * with a stable `code` on malformed input, so a parsed transition always names a
 * native owner reached by direct typed call.
 */
export function parseCpkTransition(input: unknown): CpkTransition {
	if (typeof input !== "object" || input === null || Array.isArray(input)) {
		throw new CpkSeamError("not_object", "transition must be a JSON object");
	}
	const record = input as Record<string, unknown>;
	const owner = record.owner;
	if (typeof owner !== "string" || owner.length === 0) {
		throw new CpkSeamError("invalid_field", 'transition field "owner" must be a non-empty string');
	}
	if (isCpkEventClass(owner)) {
		throw new CpkSeamError(
			"event_routed_transition",
			`transition owner "${owner}" is an event class; lifecycle transitions must call a native authority directly`,
		);
	}
	if (!isCpkTransitionOwner(owner)) {
		throw new CpkSeamError(
			"unknown_owner",
			`transition owner must be one of ${CPK_TRANSITION_OWNERS.join(", ")}, not "${owner}"`,
		);
	}
	return {
		owner,
		field: requireNonEmptyString(record.field, "field"),
		target: requireNonEmptyString(record.target, "target"),
	};
}

export type CpkTransitionViolationCode = "not_object" | "invalid_field" | "unknown_owner" | "event_routed_transition";

export interface CpkTransitionViolation {
	code: CpkTransitionViolationCode;
	/** Offending owner value, or `#<index>` when the input is not an object. */
	subject: string;
	message: string;
}

function transitionSubject(input: unknown, index: number): string {
	if (typeof input === "object" && input !== null && !Array.isArray(input)) {
		const owner = (input as Record<string, unknown>).owner;
		if (typeof owner === "string" && owner.length > 0) return owner;
	}
	return `#${index}`;
}

/**
 * Validate a batch of untrusted transitions, returning one violation per
 * offending input (sorted by subject). An empty array means every transition is
 * a direct typed call to a native authority.
 */
export function validateTransitionOwnership(inputs: readonly unknown[]): CpkTransitionViolation[] {
	const violations: CpkTransitionViolation[] = [];
	inputs.forEach((input, index) => {
		try {
			parseCpkTransition(input);
		} catch (err) {
			if (!(err instanceof CpkSeamError)) throw err;
			if (
				err.code !== "not_object" &&
				err.code !== "invalid_field" &&
				err.code !== "unknown_owner" &&
				err.code !== "event_routed_transition"
			) {
				throw err;
			}
			violations.push({ code: err.code, subject: transitionSubject(input, index), message: err.message });
		}
	});
	return violations.sort((a, b) =>
		a.subject === b.subject ? a.code.localeCompare(b.code) : a.subject.localeCompare(b.subject),
	);
}
