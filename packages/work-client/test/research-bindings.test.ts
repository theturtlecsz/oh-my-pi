import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { renderResearchBindings, type SchemaDocument } from "../scripts/generate-research-bindings";

const repoRoot = join(import.meta.dir, "..", "..", "..");
const schema = JSON.parse(
	readFileSync(join(repoRoot, "python/omp-work/src/omp_work/contracts/v1/schema.json"), "utf8"),
) as SchemaDocument;
const apiSchema = JSON.parse(
	readFileSync(join(repoRoot, "python/omp-work/src/omp_work/contracts/v1/api-schema.json"), "utf8"),
) as SchemaDocument;
const generated = readFileSync(join(import.meta.dir, "../src/research.generated.ts"), "utf8");

test("committed research bindings match the canonical schemas", () => {
	expect(renderResearchBindings(schema, apiSchema)).toBe(generated);
});

test("altering one Research enum value is detected as drift", () => {
	const altered = structuredClone(schema);
	const state = altered.models?.ResearchCampaign?.properties?.state;
	if (!state?.enum) throw new Error("ResearchCampaign.state enum missing");
	state.enum = state.enum.map(value => (value === "draft" ? "sketch" : value));
	expect(renderResearchBindings(altered, apiSchema)).not.toBe(renderResearchBindings(schema, apiSchema));
});
