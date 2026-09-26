import { describe, expect, it } from "bun:test";
import {
	CPK3_SCHEMA,
	CpkCapabilityRegistry,
	CpkSeamError,
	type CpkSeamErrorCode,
	CpkTypedEventBus,
	type CpkSeamEvent,
	assertNonAuthoritativeOnBus,
	parseCpkSeamEvent,
	parseCpkTransition,
	validateCapabilityTable,
	validateTransitionOwnership,
} from "../src/extensibility/cpk/seams";

function event(cls: CpkSeamEvent["class"], capability = "demo"): CpkSeamEvent {
	return { schema: CPK3_SCHEMA, class: cls, capability, payload: null };
}

function captureSeamError(fn: () => unknown): CpkSeamError {
	try {
		fn();
	} catch (err) {
		if (err instanceof CpkSeamError) return err;
		throw err;
	}
	throw new Error("expected CpkSeamError");
}

function expectCode(fn: () => unknown, code: CpkSeamErrorCode): void {
	expect(captureSeamError(fn).code).toBe(code);
}

describe("CPK-3 typed event seams (OMP-206)", () => {
	it("canonicalizes an untrusted event and rejects malformed shapes with a stable code", () => {
		expect(parseCpkSeamEvent({ schema: CPK3_SCHEMA, class: "signal", capability: "demo", payload: 1 })).toEqual({
			schema: CPK3_SCHEMA,
			class: "signal",
			capability: "demo",
			payload: 1,
		});
		expectCode(() => parseCpkSeamEvent(null), "not_object");
		expectCode(() => parseCpkSeamEvent([]), "not_object");
		expectCode(() => parseCpkSeamEvent({ schema: "cpk3/v2", class: "signal", capability: "demo" }), "invalid_schema");
		expectCode(() => parseCpkSeamEvent({ schema: CPK3_SCHEMA, class: "fact", capability: "demo" }), "invalid_class");
		expectCode(() => parseCpkSeamEvent({ schema: CPK3_SCHEMA, class: "signal", capability: "" }), "invalid_field");
	});

	it("refuses to route an authoritative committed fact over the bus", () => {
		expectCode(() => assertNonAuthoritativeOnBus(event("committed")), "authoritative_on_bus");
		expect(() => assertNonAuthoritativeOnBus(event("proposal"))).not.toThrow();
		expect(() => assertNonAuthoritativeOnBus(event("diagnostic"))).not.toThrow();
	});

	it("delivers only non-authoritative events and never lets a committed fact reach a handler", () => {
		const bus = new CpkTypedEventBus();
		const seen: string[] = [];
		bus.on("proposal", received => seen.push(`proposal:${received.capability}`));

		bus.emit(event("proposal", "search"));
		bus.emit(event("signal", "audit"));
		expect(seen).toEqual(["proposal:search"]);

		expectCode(() => bus.emit(event("committed")), "authoritative_on_bus");
		expectCode(() => bus.on("committed", () => undefined), "authoritative_on_bus");
		expect(seen).toEqual(["proposal:search"]);
	});

	it("unsubscribes a bus handler with the returned disposer", () => {
		const bus = new CpkTypedEventBus();
		let count = 0;
		const off = bus.on("proposal", () => {
			count += 1;
		});
		bus.emit(event("proposal"));
		off();
		bus.emit(event("proposal"));
		expect(count).toBe(1);
	});

	it("keeps a capability table finite and single-provider", () => {
		expect(
			validateCapabilityTable(
				["search", "audit"],
				[
					{ capability: "search", provider: "a" },
					{ capability: "search", provider: "a" },
				],
			),
		).toEqual([]);
		expect(
			validateCapabilityTable(
				["search"],
				[
					{ capability: "search", provider: "a" },
					{ capability: "search", provider: "b" },
				],
			).map(violation => violation.code),
		).toEqual(["duplicate_provider"]);
		expect(
			validateCapabilityTable(["search"], [{ capability: "missing", provider: "a" }]).map(
				violation => violation.code,
			),
		).toEqual(["unknown_capability"]);
	});

	it("refuses a second provider and an unregistered RPC in the capability registry", () => {
		const registry = new CpkCapabilityRegistry(["search"]);
		registry.register("search", "a");
		registry.register("search", "a");
		expect(registry.provider("search")).toBe("a");
		expectCode(() => registry.register("search", "b"), "duplicate_provider");
		expectCode(() => registry.register("missing", "a"), "unknown_capability");
		expectCode(() => registry.provider("missing"), "unknown_capability");
	});

	it("accepts only a direct typed transition to a native authority", () => {
		expect(parseCpkTransition({ owner: "workservice", field: "state", target: "in_progress" })).toEqual({
			owner: "workservice",
			field: "state",
			target: "in_progress",
		});
		expectCode(() => parseCpkTransition(null), "not_object");
		expectCode(() => parseCpkTransition({ owner: "workservice", target: "in_progress" }), "invalid_field");
		expectCode(() => parseCpkTransition({ owner: "bus", field: "state", target: "in_progress" }), "unknown_owner");
	});

	it("refuses a lifecycle transition routed through an event class", () => {
		expectCode(
			() => parseCpkTransition({ owner: "committed", field: "state", target: "done" }),
			"event_routed_transition",
		);
		expectCode(
			() => parseCpkTransition({ owner: "proposal", field: "state", target: "done" }),
			"event_routed_transition",
		);
	});

	it("reports every offending transition in a batch, keyed by owner", () => {
		const violations = validateTransitionOwnership([
			{ owner: "session", field: "state", target: "idle" },
			{ owner: "signal", field: "state", target: "idle" },
			{ owner: "bus", field: "state", target: "idle" },
		]);
		expect(violations.map(violation => [violation.subject, violation.code])).toEqual([
			["bus", "unknown_owner"],
			["signal", "event_routed_transition"],
		]);
		expect(validateTransitionOwnership([{ owner: "workservice", field: "state", target: "done" }])).toEqual([]);
	});
});
