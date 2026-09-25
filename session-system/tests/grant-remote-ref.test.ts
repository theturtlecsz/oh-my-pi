import { describe, expect, test } from "bun:test";
import { grantRemoteRef } from "./fixtures/grant-remote-ref";

describe("grantRemoteRef", () => {
	test("returns remote_ref for valid execution object", () => {
		const exec = {
			grant: {
				grant_id: "grant-123",
				remote_ref: "refs/heads/execution/omp-1-test",
			},
		};
		expect(grantRemoteRef(exec)).toBe("refs/heads/execution/omp-1-test");
	});

	test("returns remote_ref for valid JSON execution string", () => {
		const exec = {
			grant: {
				remote_ref: "refs/heads/release/omp-180",
			},
		};
		expect(grantRemoteRef(JSON.stringify(exec))).toBe("refs/heads/release/omp-180");
	});

	test("throws on missing grant", () => {
		expect(() => grantRemoteRef({})).toThrow();
		expect(() => grantRemoteRef({ other: "data" })).toThrow();
		expect(() => grantRemoteRef({ grant: null })).toThrow();
		expect(() => grantRemoteRef({ grant: undefined })).toThrow();
		expect(() => grantRemoteRef(null)).toThrow();
		expect(() => grantRemoteRef(undefined)).toThrow();
		expect(() => grantRemoteRef("not-json")).toThrow();
	});

	test("throws when remote_ref is null", () => {
		expect(() => grantRemoteRef({ grant: { remote_ref: null } })).toThrow();
		expect(() => grantRemoteRef({ grant: { remote_ref: undefined } })).toThrow();
		expect(() => grantRemoteRef({ grant: {} })).toThrow();
	});

	test("throws when remote_ref is refs/tags/x", () => {
		expect(() => grantRemoteRef({ grant: { remote_ref: "refs/tags/v1.0.0" } })).toThrow();
		expect(() => grantRemoteRef({ grant: { remote_ref: "refs/tags/x" } })).toThrow();
	});

	test("throws when remote_ref does not start with refs/heads/", () => {
		expect(() => grantRemoteRef({ grant: { remote_ref: "main" } })).toThrow();
		expect(() => grantRemoteRef({ grant: { remote_ref: "refs/remotes/origin/main" } })).toThrow();
		expect(() => grantRemoteRef({ grant: { remote_ref: "" } })).toThrow();
		expect(() => grantRemoteRef({ grant: { remote_ref: 12345 } })).toThrow();
	});
});
