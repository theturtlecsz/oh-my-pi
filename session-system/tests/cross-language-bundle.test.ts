import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { canonicalJson, sha256Hex } from "@oh-my-pi/pi-work-client";
import {
	CONTEXT_BUNDLE_IDENTITY_ENCODING,
	topLevelValueSpan,
	validateKnowledgeBundlePayload,
	type KnowledgeBudgetActual,
	type KnowledgeBudgetSpec,
	type KnowledgeBundlePayload,
	type KnowledgeExecutionIdentity,
} from "../extensions/workflow/knowledge-bridge";

interface BundleFixtureOutput {
	readonly content_bytes: string;
	readonly bundle_sha256: string;
	readonly legacy_content_sha256?: string;
	readonly bundle_payload: KnowledgeBundlePayload & Record<string, unknown>;
	readonly mandatory: Record<string, unknown>;
	readonly optional: Record<string, unknown>;
	readonly budget_spec?: KnowledgeBudgetSpec;
	readonly identity_encoding?: string;
	readonly identity_canonical_json?: string;
	readonly divergent_content_bytes: string;
	readonly divergent_bundle_payload: KnowledgeBundlePayload & Record<string, unknown>;
	readonly nested_content_bytes: string;
	readonly nested_inner_content_bytes: string;
	readonly nested_bundle_payload: KnowledgeBundlePayload & Record<string, unknown>;
}

function resolveCandidatePythonFixture(): BundleFixtureOutput {
	const repoRoot = path.resolve(import.meta.dir, "../..");
	const scriptPath = path.join(
		repoRoot,
		"session-system/tests/fixtures/bundle-cross-language-fixture.py",
	);

	const pythonSearchPaths: string[] = [
		process.env.PYTHON_BIN ?? "",
		path.join(repoRoot, ".venv/bin/python"),
		path.join(repoRoot, "python/omp-work/.venv/bin/python"),
		"python3",
		"python",
	].filter((candidatePath: string) => candidatePath.length > 0);

	let lastFailure: string = "no candidate python interpreters evaluated";

	for (const pythonBin of pythonSearchPaths) {
		try {
			const proc = Bun.spawnSync([pythonBin, scriptPath], {
				cwd: repoRoot,
				env: {
					...process.env,
					PYTHONPATH: path.join(repoRoot, "python/omp-work/src"),
				},
			});

			if (proc.exitCode === 0) {
				const stdoutText = proc.stdout.toString();
				const parsed = JSON.parse(stdoutText) as BundleFixtureOutput;
				return parsed;
			}

			const stderrText = proc.stderr.toString();
			lastFailure = `${pythonBin} exited with code ${proc.exitCode}: ${stderrText}`;
		} catch (error: unknown) {
			const errorMessage = error instanceof Error ? error.message : String(error);
			lastFailure = `${pythonBin} execution failed: ${errorMessage}`;
		}
	}

	throw new Error(`Failed to generate fixture via candidate Python: ${lastFailure}`);
}

describe("cross-language bundle fixture test", () => {
	const fixture = resolveCandidatePythonFixture();

	const expectedIdentity: KnowledgeExecutionIdentity = {
		workspaceId: fixture.bundle_payload.workspace_id,
		repositoryId: fixture.bundle_payload.repository_id,
		workId: fixture.bundle_payload.work_id,
		revisionId: fixture.bundle_payload.revision_id,
		candidateId: fixture.bundle_payload.candidate_id,
		stage: fixture.bundle_payload.stage,
	};

	const expectedBudget: KnowledgeBudgetSpec = {
		method: "utf8_bytes",
		limit: fixture.bundle_payload.budget.limit,
		tokenizer_id: null,
	};

	describe("acceptance of exact Python canonical JSON and SHA-256", () => {
		it("accepts candidate Python generated bundle payload with exact hash", () => {
			const result = validateKnowledgeBundlePayload(fixture.bundle_payload, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(true);

			if (!result.ok) {
				throw new Error(`Expected valid bundle payload, got failure: ${result.reason}`);
			}

			expect(result.bundle.bundle_sha256).toBe(fixture.bundle_sha256);
			expect(result.contentBytes).toBe(fixture.content_bytes);
			expect(result.bundle.bundle_id).toBe(fixture.bundle_payload.bundle_id);
			expect(result.bundle.workspace_id).toBe(fixture.bundle_payload.workspace_id);
			expect(result.bundle.repository_id).toBe(fixture.bundle_payload.repository_id);
			expect(result.bundle.work_id).toBe(fixture.bundle_payload.work_id);
			expect(result.bundle.revision_id).toBe(fixture.bundle_payload.revision_id);
			expect(result.bundle.candidate_id).toBe(null);
			expect(result.bundle.stage).toBe("execute");
			expect(result.bundle.snapshot_id).toBe(null);
			// Python BudgetActual has no tokenizer_id; the served budget mirrors it exactly
			const expectedServedBudget: KnowledgeBudgetActual = {
				method: "utf8_bytes",
				limit: fixture.bundle_payload.budget.limit,
				used: fixture.bundle_payload.budget.used,
				mandatory_used: fixture.bundle_payload.budget.mandatory_used,
				dropped_optional: [],
			};
			expect(result.bundle.budget).toEqual(expectedServedBudget);
			expect("tokenizer_id" in result.bundle.budget).toBe(false);
			expect(new TextEncoder().encode(fixture.content_bytes).length).toBe(result.bundle.budget.used);
			expect(result.bundle.enrichment_status).toBe("applied");
			expect(result.bundle.proposal_lineage).toEqual(["00000000-0000-0000-0000-000000000051"]);
			expect(result.bundle.receipt_lineage).toEqual(["00000000-0000-0000-0000-000000000061"]);
			expect(result.bundle.identity_encoding).toBe(CONTEXT_BUNDLE_IDENTITY_ENCODING);
			expect(result.bundle.identity_canonical_json).toBe(
				fixture.identity_canonical_json ?? fixture.bundle_payload.identity_canonical_json,
			);
		});

		it("verifies fixture preserves Unicode, numeric values, and nested key ordering", () => {
			const mandatory = fixture.bundle_payload.mandatory;
			expect(mandatory).toBeDefined();
			expect(mandatory?.z_unicode).toBe("éléphant 🚀 漢字 — ñandú 👾");
			expect(mandatory?.m_numeric).toBe(42);
			expect(mandatory?.a_float).toBe(3.14159);
			expect(mandatory?.d_negative).toBe(-17);
			expect(mandatory?.k_zero).toBe(0);

			const optional = fixture.bundle_payload.optional;
			expect(optional).toBeDefined();
			expect(optional?.z_notes).toBe("✨ cross-language canonical json verification");
			expect(optional?.a_count).toBe(100);
			expect(optional?.p_priority).toBe(-1);
			expect(optional?.b_ratio).toBe(0.125);
		});

		it("accepts non-null candidate_id and snapshot_id when hash and identity match", () => {
			const nonNullBudget: KnowledgeBudgetSpec = { method: "utf8_bytes", limit: 32768, tokenizer_id: null };
			const nonNullMandatory = { item: "mandatory facts" };
			const nonNullOptional = { note: "optional note" };
			const candidateId = "00000000-0000-0000-0000-000000000040";
			const snapshotId = "snapshot-001";

			// Test-only stand-in for Python identity_canonical_json; production TS never rebuilds it.
			// ASCII/int-only vector, so TS canonicalJson equals the Python bytes here.
			const identityCanonicalJson = canonicalJson({
				identity_encoding: CONTEXT_BUNDLE_IDENTITY_ENCODING,
				workspace_id: fixture.bundle_payload.workspace_id,
				repository_id: fixture.bundle_payload.repository_id,
				work_id: fixture.bundle_payload.work_id,
				revision_id: fixture.bundle_payload.revision_id,
				candidate_id: candidateId,
				stage: "execute",
				snapshot_id: snapshotId,
				budget_spec: nonNullBudget,
				content: { mandatory: nonNullMandatory, optional: nonNullOptional },
			});
			const bundleSha = sha256Hex(identityCanonicalJson);
			// ASCII/int-only content: TS and Python canonical bytes coincide for this vector
			const nonNullContentBytes = canonicalJson({ mandatory: nonNullMandatory, optional: nonNullOptional });

			const payload: KnowledgeBundlePayload = {
				...fixture.bundle_payload,
				candidate_id: candidateId,
				snapshot_id: snapshotId,
				identity_encoding: CONTEXT_BUNDLE_IDENTITY_ENCODING,
				identity_canonical_json: identityCanonicalJson,
				bundle_sha256: bundleSha,
				budget: {
					method: "utf8_bytes",
					limit: nonNullBudget.limit,
					used: new TextEncoder().encode(nonNullContentBytes).length,
					mandatory_used: new TextEncoder().encode(canonicalJson(nonNullMandatory)).length,
					dropped_optional: [],
				},
				mandatory: nonNullMandatory,
				optional: nonNullOptional,
				content_canonical_json: nonNullContentBytes,
			};

			const identity: KnowledgeExecutionIdentity = {
				...expectedIdentity,
				candidateId,
			};

			const result = validateKnowledgeBundlePayload(payload, identity, nonNullBudget);
			expect(result.ok).toBe(true);
			if (result.ok) {
				expect(result.bundle.candidate_id).toBe(candidateId);
				expect(result.bundle.snapshot_id).toBe(snapshotId);
				expect(result.bundle.identity_encoding).toBe(CONTEXT_BUNDLE_IDENTITY_ENCODING);
				expect(result.bundle.identity_canonical_json).toBe(identityCanonicalJson);
				expect(result.bundle.bundle_sha256).toBe(bundleSha);
			}
		});
	});

	describe("legacy content-only hash refusal", () => {
		it("refuses legacy bundle where bundle_sha256 was hashed over content only", () => {
			const legacySha = fixture.legacy_content_sha256 ?? "legacy-sha-not-found";
			const legacyPayload = {
				...fixture.bundle_payload,
				bundle_sha256: legacySha,
			};

			const result = validateKnowledgeBundlePayload(legacyPayload, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("bundle_sha256 mismatch");
			}
		});
	});

	describe("identity, budget, and snapshot mismatch detection", () => {
		it("rejects workspace_id mismatch against expected identity", () => {
			const result = validateKnowledgeBundlePayload(
				fixture.bundle_payload,
				{ ...expectedIdentity, workspaceId: "00000000-0000-0000-0000-000000000099" },
				expectedBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("workspace_id mismatch");
			}
		});

		it("rejects stage mismatch against expected identity", () => {
			const result = validateKnowledgeBundlePayload(
				fixture.bundle_payload,
				{ ...expectedIdentity, stage: "subagent" },
				expectedBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("stage mismatch");
			}
		});

		it("rejects candidate_id mismatch against expected identity", () => {
			const result = validateKnowledgeBundlePayload(
				fixture.bundle_payload,
				{ ...expectedIdentity, candidateId: "00000000-0000-0000-0000-000000000040" },
				expectedBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("candidate_id mismatch");
			}
		});

		it("rejects budget limit mismatch against sent budget", () => {
			const differentBudget: KnowledgeBudgetSpec = {
				method: "utf8_bytes",
				limit: 16384,
				tokenizer_id: null,
			};
			const result = validateKnowledgeBundlePayload(
				fixture.bundle_payload,
				expectedIdentity,
				differentBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("budget mismatch");
			}
		});

		it("rejects budget tokenizer_id mismatch against sent budget in identity hash", () => {
			const differentTokenizerBudget: KnowledgeBudgetSpec = {
				method: "utf8_bytes",
				limit: fixture.bundle_payload.budget.limit,
				tokenizer_id: "custom-tokenizer",
			};
			const result = validateKnowledgeBundlePayload(
				fixture.bundle_payload,
				expectedIdentity,
				differentTokenizerBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("budget tokenizer_id mismatch in identity payload");
			}
		});

		it("rejects payload with invalid budget.method", () => {
			const invalidMethodPayload = {
				...fixture.bundle_payload,
				budget: { method: "characters", limit: 32768 },
			};
			const result = validateKnowledgeBundlePayload(invalidMethodPayload, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toBe('budget.method must be "utf8_bytes"');
			}
		});

		it("rejects invalid budget.tokenizer_id type", () => {
			const invalidTokenizerPayload = {
				...fixture.bundle_payload,
				budget: { method: "utf8_bytes", limit: 32768, tokenizer_id: 123 },
			};
			const result = validateKnowledgeBundlePayload(invalidTokenizerPayload, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toBe("budget.tokenizer_id must be a string or null");
			}
		});

		it("rejects payload with non-positive or non-integer budget.limit", () => {
			const zeroLimitPayload = {
				...fixture.bundle_payload,
				budget: { method: "utf8_bytes", limit: 0 },
			};
			expect(validateKnowledgeBundlePayload(zeroLimitPayload, expectedIdentity, expectedBudget)).toEqual({
				ok: false,
				reason: "budget.limit must be a positive safe integer",
			});

			const floatLimitPayload = {
				...fixture.bundle_payload,
				budget: { method: "utf8_bytes", limit: 12.5 },
			};
			expect(validateKnowledgeBundlePayload(floatLimitPayload, expectedIdentity, expectedBudget)).toEqual({
				ok: false,
				reason: "budget.limit must be a positive safe integer",
			});
		});

		it("rejects snapshot_id drift in payload without hash recomputation", () => {
			const driftedSnapshotPayload = {
				...fixture.bundle_payload,
				snapshot_id: "tampered-snapshot",
			};
			const result = validateKnowledgeBundlePayload(driftedSnapshotPayload, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("snapshot_id mismatch between identity and bundle");
			}
		});

		it("rejects candidate_id null drift in payload without hash recomputation", () => {
			const driftedCandidatePayload = {
				...fixture.bundle_payload,
				candidate_id: "tampered-candidate",
			};
			const result = validateKnowledgeBundlePayload(driftedCandidatePayload, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("candidate_id mismatch between identity and bundle");
			}
		});
	});

	describe("rejection of one-byte / hash drift", () => {
		it("rejects one-byte drift in bundle_sha256", () => {
			const originalSha = fixture.bundle_sha256;
			const driftedSha = originalSha.endsWith("a")
				? `${originalSha.slice(0, -1)}b`
				: `${originalSha.slice(0, -1)}a`;

			const tamperedPayload = {
				...fixture.bundle_payload,
				bundle_sha256: driftedSha,
			};

			const result = validateKnowledgeBundlePayload(tamperedPayload);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("bundle_sha256 mismatch");
			}
		});

		it("rejects one-byte drift in Unicode mandatory content", () => {
			const tamperedMandatory = {
				...fixture.bundle_payload.mandatory,
				z_unicode: `${String(fixture.bundle_payload.mandatory?.z_unicode)}!`,
			};

			const tamperedPayload = {
				...fixture.bundle_payload,
				mandatory: tamperedMandatory,
			};

			const result = validateKnowledgeBundlePayload(tamperedPayload);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("mandatory content mismatch between identity and bundle");
			}
		});

		it("rejects numeric drift in mandatory integer value", () => {
			const tamperedMandatory = {
				...fixture.bundle_payload.mandatory,
				m_numeric: 43, // original was 42
			};

			const tamperedPayload = {
				...fixture.bundle_payload,
				mandatory: tamperedMandatory,
			};

			const result = validateKnowledgeBundlePayload(tamperedPayload);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("mandatory content mismatch between identity and bundle");
			}
		});

		it("rejects numeric drift in mandatory floating point value", () => {
			const tamperedMandatory = {
				...fixture.bundle_payload.mandatory,
				a_float: 3.14158, // original was 3.14159
			};

			const tamperedPayload = {
				...fixture.bundle_payload,
				mandatory: tamperedMandatory,
			};

			const result = validateKnowledgeBundlePayload(tamperedPayload);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("mandatory content mismatch between identity and bundle");
			}
		});

		it("rejects key drift in optional content", () => {
			const tamperedOptional = {
				...fixture.bundle_payload.optional,
				extra_key: "unexpected",
			};

			const tamperedPayload = {
				...fixture.bundle_payload,
				optional: tamperedOptional,
			};

			const result = validateKnowledgeBundlePayload(tamperedPayload);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("optional content mismatch between identity and bundle");
			}
		});

		it("rejects completely drifted / zeroed hash", () => {
			const zeroShaPayload = {
				...fixture.bundle_payload,
				bundle_sha256: "0".repeat(64),
			};

			const result = validateKnowledgeBundlePayload(zeroShaPayload);
			expect(result.ok).toBe(false);
			if (!result.ok) {
				expect(result.reason).toContain("bundle_sha256 mismatch");
			}
		});
	});

	describe("strict validator structural defenses", () => {
		it("rejects non-object payloads", () => {
			expect(validateKnowledgeBundlePayload(null)).toEqual({
				ok: false,
				reason: "payload is not an object",
			});
			expect(validateKnowledgeBundlePayload([])).toEqual({
				ok: false,
				reason: "payload is not an object",
			});
			expect(validateKnowledgeBundlePayload("payload")).toEqual({
				ok: false,
				reason: "payload is not an object",
			});
		});

		it("rejects missing or empty bundle_id", () => {
			const noId = { ...fixture.bundle_payload, bundle_id: "" };
			expect(validateKnowledgeBundlePayload(noId)).toEqual({
				ok: false,
				reason: "bundle_id must be a non-empty string",
			});

			const whitespaceId = { ...fixture.bundle_payload, bundle_id: "   " };
			expect(validateKnowledgeBundlePayload(whitespaceId)).toEqual({
				ok: false,
				reason: "bundle_id must be a non-empty string",
			});
		});

		it("rejects missing or empty bundle_sha256", () => {
			const noSha = { ...fixture.bundle_payload, bundle_sha256: "" };
			expect(validateKnowledgeBundlePayload(noSha)).toEqual({
				ok: false,
				reason: "bundle_sha256 must be a non-empty string",
			});

			const whitespaceSha = { ...fixture.bundle_payload, bundle_sha256: "   " };
			expect(validateKnowledgeBundlePayload(whitespaceSha)).toEqual({
				ok: false,
				reason: "bundle_sha256 must be a non-empty string",
			});
		});

		it("rejects invalid proposal_lineage", () => {
			const nonArray = { ...fixture.bundle_payload, proposal_lineage: "not-an-array" };
			expect(validateKnowledgeBundlePayload(nonArray)).toEqual({
				ok: false,
				reason: "proposal_lineage must be an array of strings",
			});

			const nonStringElements = {
				...fixture.bundle_payload,
				proposal_lineage: [123],
			};
			expect(validateKnowledgeBundlePayload(nonStringElements)).toEqual({
				ok: false,
				reason: "proposal_lineage must be an array of strings",
			});
		});

		it("rejects invalid receipt_lineage", () => {
			const nonArray = { ...fixture.bundle_payload, receipt_lineage: 123 };
			expect(validateKnowledgeBundlePayload(nonArray)).toEqual({
				ok: false,
				reason: "receipt_lineage must be an array of strings",
			});

			const nonStringElements = {
				...fixture.bundle_payload,
				receipt_lineage: [true],
			};
			expect(validateKnowledgeBundlePayload(nonStringElements)).toEqual({
				ok: false,
				reason: "receipt_lineage must be an array of strings",
			});
		});

		it("rejects array or primitive mandatory content", () => {
			const arrayMandatory = { ...fixture.bundle_payload, mandatory: ["not", "an", "object"] };
			expect(validateKnowledgeBundlePayload(arrayMandatory)).toEqual({
				ok: false,
				reason: "mandatory must be an object if provided",
			});

			const primitiveMandatory = { ...fixture.bundle_payload, mandatory: 42 };
			expect(validateKnowledgeBundlePayload(primitiveMandatory)).toEqual({
				ok: false,
				reason: "mandatory must be an object if provided",
			});
		});

		it("rejects array or primitive optional content", () => {
			const arrayOptional = { ...fixture.bundle_payload, optional: ["not", "an", "object"] };
			expect(validateKnowledgeBundlePayload(arrayOptional)).toEqual({
				ok: false,
				reason: "optional must be an object if provided",
			});

			const primitiveOptional = { ...fixture.bundle_payload, optional: true };
			expect(validateKnowledgeBundlePayload(primitiveOptional)).toEqual({
				ok: false,
				reason: "optional must be an object if provided",
			});
		});

		it("rejects missing or invalid identity fields", () => {
			const noWorkspace = { ...fixture.bundle_payload, workspace_id: "" };
			expect(validateKnowledgeBundlePayload(noWorkspace)).toEqual({
				ok: false,
				reason: "workspace_id must be a non-empty string",
			});

			const noRepo = { ...fixture.bundle_payload, repository_id: "  " };
			expect(validateKnowledgeBundlePayload(noRepo)).toEqual({
				ok: false,
				reason: "repository_id must be a non-empty string",
			});

			const noWork = { ...fixture.bundle_payload, work_id: "" };
			expect(validateKnowledgeBundlePayload(noWork)).toEqual({
				ok: false,
				reason: "work_id must be a non-empty string",
			});

			const noRevision = { ...fixture.bundle_payload, revision_id: "" };
			expect(validateKnowledgeBundlePayload(noRevision)).toEqual({
				ok: false,
				reason: "revision_id must be a non-empty string",
			});

			const noStage = { ...fixture.bundle_payload, stage: "" };
			expect(validateKnowledgeBundlePayload(noStage)).toEqual({
				ok: false,
				reason: "stage must be a non-empty string",
			});

			const invalidCandidate = { ...fixture.bundle_payload, candidate_id: 123 };
			expect(validateKnowledgeBundlePayload(invalidCandidate)).toEqual({
				ok: false,
				reason: "candidate_id must be a string or null",
			});

			const invalidSnapshot = { ...fixture.bundle_payload, snapshot_id: 456 };
			expect(validateKnowledgeBundlePayload(invalidSnapshot)).toEqual({
				ok: false,
				reason: "snapshot_id must be a string or null",
			});
		});

		it("rejects missing or invalid identity_encoding", () => {
			const noEncoding = { ...fixture.bundle_payload, identity_encoding: "" };
			expect(validateKnowledgeBundlePayload(noEncoding)).toEqual({
				ok: false,
				reason: `identity_encoding must be "${CONTEXT_BUNDLE_IDENTITY_ENCODING}"`,
			});

			const wrongEncoding = { ...fixture.bundle_payload, identity_encoding: "wrong-encoding" };
			expect(validateKnowledgeBundlePayload(wrongEncoding)).toEqual({
				ok: false,
				reason: `identity_encoding must be "${CONTEXT_BUNDLE_IDENTITY_ENCODING}"`,
			});
		});

		it("rejects missing or empty identity_canonical_json", () => {
			const noJson = { ...fixture.bundle_payload, identity_canonical_json: "" };
			expect(validateKnowledgeBundlePayload(noJson)).toEqual({
				ok: false,
				reason: "identity_canonical_json must be a non-empty string",
			});

			const whitespaceJson = { ...fixture.bundle_payload, identity_canonical_json: "   " };
			expect(validateKnowledgeBundlePayload(whitespaceJson)).toEqual({
				ok: false,
				reason: "identity_canonical_json must be a non-empty string",
			});
		});

		it("rejects malformed identity_canonical_json", () => {
			const badJson = "{invalid json";
			const malformedPayload = {
				...fixture.bundle_payload,
				identity_canonical_json: badJson,
				bundle_sha256: sha256Hex(badJson),
			};
			expect(validateKnowledgeBundlePayload(malformedPayload)).toEqual({
				ok: false,
				reason: "malformed identity_canonical_json",
			});
		});

		it("rejects non-object parsed identity_canonical_json", () => {
			const arrayJson = '["not", "an", "object"]';
			const arrayPayload = {
				...fixture.bundle_payload,
				identity_canonical_json: arrayJson,
				bundle_sha256: sha256Hex(arrayJson),
			};
			expect(validateKnowledgeBundlePayload(arrayPayload)).toEqual({
				ok: false,
				reason: "identity payload is not an object",
			});
		});
	});

	describe("exact Python content bytes (content_canonical_json) are the sole injected bytes", () => {
		const divergent = fixture.divergent_bundle_payload;

		it("Python vectors diverge from TS re-serialization, so a TS rebuild would inject wrong bytes", () => {
			const tsRebuild = canonicalJson({ mandatory: divergent.mandatory, optional: divergent.optional });
			expect(tsRebuild).not.toBe(fixture.divergent_content_bytes);
			expect(fixture.divergent_content_bytes).toContain('"ratio":1.0');
			expect(fixture.divergent_content_bytes).toContain('"tiny":1e-07');
			expect(fixture.divergent_content_bytes).toContain('"huge":1e+16');
			expect(fixture.divergent_content_bytes).toContain("\u{1D518}");
			// Python sorts keys by code point (U+FFFD before U+1F600); JS sorts by UTF-16 unit
			expect(fixture.divergent_content_bytes.indexOf('"�"')).toBeLessThan(
				fixture.divergent_content_bytes.indexOf('"\u{1F600}"'),
			);
			expect(tsRebuild.indexOf('"\u{1F600}"')).toBeLessThan(tsRebuild.indexOf('"�"'));
		});

		it("accepts the divergent bundle and returns exactly the retained Python bytes", () => {
			const result = validateKnowledgeBundlePayload(divergent, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(true);
			if (!result.ok) return;
			expect(result.contentBytes).toBe(fixture.divergent_content_bytes);
			expect(result.bundle.content_canonical_json).toBe(fixture.divergent_content_bytes);
			expect(result.contentBytes).not.toBe(canonicalJson({ mandatory: divergent.mandatory, optional: divergent.optional }));
			expect(new TextEncoder().encode(result.contentBytes).length).toBe((divergent.budget as KnowledgeBudgetActual).used!);
		});

		it("replay of the same wire payload yields byte-identical injection bytes", () => {
			const replayed = JSON.parse(JSON.stringify(divergent)) as typeof divergent;
			const first = validateKnowledgeBundlePayload(divergent, expectedIdentity, expectedBudget);
			const second = validateKnowledgeBundlePayload(replayed, expectedIdentity, expectedBudget);
			expect(first.ok && second.ok).toBe(true);
			if (first.ok && second.ok) {
				expect(second.contentBytes).toBe(first.contentBytes);
				expect(sha256Hex(second.contentBytes)).toBe(sha256Hex(fixture.divergent_content_bytes));
			}
		});

		it("rejects missing, null, or empty content_canonical_json (legacy row fails closed)", () => {
			const { content_canonical_json: _dropped, ...missing } = divergent;
			for (const payload of [missing, { ...divergent, content_canonical_json: null }, { ...divergent, content_canonical_json: "  " }]) {
				const result = validateKnowledgeBundlePayload(payload, expectedIdentity, expectedBudget);
				expect(result.ok).toBe(false);
				if (!result.ok) expect(result.reason).toContain("content_canonical_json must be a non-empty string");
			}
		});

		it("rejects content_canonical_json that parses equal but is not the hashed byte range (1.0 -> 1)", () => {
			const tampered = fixture.divergent_content_bytes.replace('"ratio":1.0', '"ratio":1');
			expect(tampered).not.toBe(fixture.divergent_content_bytes);
			const result = validateKnowledgeBundlePayload(
				{ ...divergent, content_canonical_json: tampered },
				expectedIdentity,
				expectedBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) expect(result.reason).toContain("not the exact content byte range");
		});

		it("rejects a TS re-serialization substituted for the Python bytes", () => {
			const result = validateKnowledgeBundlePayload(
				{ ...divergent, content_canonical_json: canonicalJson({ mandatory: divergent.mandatory, optional: divergent.optional }) },
				expectedIdentity,
				expectedBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) expect(result.reason).toContain("not the exact content byte range");
		});

		it("rejects served mandatory drift that the retained bytes do not carry", () => {
			const result = validateKnowledgeBundlePayload(
				{ ...divergent, mandatory: { ...divergent.mandatory, ratio: 2 } },
				expectedIdentity,
				expectedBudget,
			);
			expect(result.ok).toBe(false);
		});

		it("rejects budget.used that disagrees with the retained byte length", () => {
			const used = (divergent.budget as KnowledgeBudgetActual).used!;
			const result = validateKnowledgeBundlePayload(
				{ ...divergent, budget: { ...divergent.budget, used: used + 1 } },
				expectedIdentity,
				expectedBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) expect(result.reason).toContain("budget.used mismatch");
		});

		it("rejects content bytes from a different bundle even when they parse", () => {
			const result = validateKnowledgeBundlePayload(
				{ ...divergent, content_canonical_json: fixture.content_bytes },
				expectedIdentity,
				expectedBudget,
			);
			expect(result.ok).toBe(false);
		});
	});

	describe("content range is bound positionally to the top-level identity key, not by substring", () => {
		const nested = fixture.nested_bundle_payload;

		it("Python vectors: nested content/identity_encoding/repository_id decoys, escaped braces, Unicode, floats", () => {
			const text = nested.identity_canonical_json;
			// The decoy layout `,"content":<inner>,"identity_encoding":` appears nested inside mandatory,
			// i.e. inside the value of the real top-level `content` key
			expect(text).toContain(`,"content":${fixture.nested_inner_content_bytes},"identity_encoding":`);
			const topLevel = topLevelValueSpan(text, "content")!;
			const decoyAt = text.indexOf(`"content":${fixture.nested_inner_content_bytes}`);
			expect(decoyAt).toBeGreaterThan(topLevel.start);
			expect(decoyAt).toBeLessThan(topLevel.end);
			expect(fixture.nested_content_bytes).toContain('"ratio":1.0');
			expect(fixture.nested_content_bytes).toContain('"tiny":1e-07');
			expect(fixture.nested_content_bytes).toContain("éléphant 🚀 漢字");
			expect(fixture.nested_content_bytes).toContain('\\"content\\"');
		});

		it("accepts the nested-decoy bundle and returns exactly the top-level Python bytes", () => {
			const result = validateKnowledgeBundlePayload(nested, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(true);
			if (!result.ok) return;
			expect(result.contentBytes).toBe(fixture.nested_content_bytes);
			expect(result.bundle.budget.used).toBe(new TextEncoder().encode(fixture.nested_content_bytes).length);
		});

		it("rejects the nested decoy range even though the substring pattern matches inside mandatory", () => {
			const decoy = {
				...nested,
				content_canonical_json: fixture.nested_inner_content_bytes,
				budget: { ...nested.budget, used: new TextEncoder().encode(fixture.nested_inner_content_bytes).length },
			};
			const result = validateKnowledgeBundlePayload(decoy, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(false);
			if (!result.ok) expect(result.reason).toContain("not the exact content byte range");
		});

		it("rejects an identity payload whose hashed identity_encoding is not the current version", () => {
			const stale = nested.identity_canonical_json.replace(
				`"identity_encoding":"${CONTEXT_BUNDLE_IDENTITY_ENCODING}","repository_id"`,
				'"identity_encoding":"omp-context-bundle-identity/v1","repository_id"',
			);
			expect(stale).not.toBe(nested.identity_canonical_json);
			const result = validateKnowledgeBundlePayload(
				{ ...nested, identity_canonical_json: stale, bundle_sha256: sha256Hex(stale) },
				expectedIdentity,
				expectedBudget,
			);
			expect(result.ok).toBe(false);
			if (!result.ok) expect(result.reason).toContain("identity payload identity_encoding must be");
		});

		it("topLevelValueSpan skips strings with escapes and nested containers", () => {
			const text = '{"a":"}{\\"content\\":","content":{"x":[1,{"content":2}],"y":"\\\\"},"z":null}';
			const span = topLevelValueSpan(text, "content");
			expect(span).toBeDefined();
			expect(text.slice(span!.start, span!.end)).toBe('{"x":[1,{"content":2}],"y":"\\\\"}');
			expect(topLevelValueSpan(text, "z")).toEqual({ start: text.length - 5, end: text.length - 1 });
			expect(topLevelValueSpan(text, "x")).toBeUndefined();
			expect(topLevelValueSpan('["content"]', "content")).toBeUndefined();
		});
	});

	describe("BudgetActual fields Python always emits are required", () => {
		const cases: Array<[string, Record<string, unknown>, string]> = [
			["missing used", { used: undefined }, "budget.used must be a non-negative safe integer"],
			["float used", { used: 12.5 }, "budget.used must be a non-negative safe integer"],
			["negative used", { used: -1 }, "budget.used must be a non-negative safe integer"],
			["missing mandatory_used", { mandatory_used: undefined }, "budget.mandatory_used must be a non-negative safe integer"],
			["string mandatory_used", { mandatory_used: "12" }, "budget.mandatory_used must be a non-negative safe integer"],
			["missing dropped_optional", { dropped_optional: undefined }, "budget.dropped_optional must be an array of strings"],
			["non-string dropped_optional", { dropped_optional: [1] }, "budget.dropped_optional must be an array of strings"],
		];
		for (const [name, patch, reason] of cases) {
			it(`rejects ${name}`, () => {
				const budget = { ...fixture.bundle_payload.budget, ...patch };
				for (const key of Object.keys(patch)) if (patch[key] === undefined) delete (budget as Record<string, unknown>)[key];
				const result = validateKnowledgeBundlePayload({ ...fixture.bundle_payload, budget }, expectedIdentity, expectedBudget);
				expect(result).toEqual({ ok: false, reason });
			});
		}

		it("rejects a spec-only budget echoed back in place of BudgetActual", () => {
			const result = validateKnowledgeBundlePayload(
				{ ...fixture.bundle_payload, budget: fixture.budget_spec },
				expectedIdentity,
				expectedBudget,
			);
			expect(result).toEqual({ ok: false, reason: "budget.used must be a non-negative safe integer" });
		});

		it("accepts tokenizer_id absent (Python BudgetActual) or explicitly null", () => {
			expect(validateKnowledgeBundlePayload(fixture.bundle_payload, expectedIdentity, expectedBudget).ok).toBe(true);
			const withNull = { ...fixture.bundle_payload, budget: { ...fixture.bundle_payload.budget, tokenizer_id: null } };
			const result = validateKnowledgeBundlePayload(withNull, expectedIdentity, expectedBudget);
			expect(result.ok).toBe(true);
			if (result.ok) expect(result.bundle.budget.tokenizer_id).toBe(null);
		});
	});
});
