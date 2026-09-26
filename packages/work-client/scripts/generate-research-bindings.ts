// Generate TypeScript bindings for research models in the canonical Work Ledger schemas.
//
//   bun packages/work-client/scripts/generate-research-bindings.ts
//
// writes packages/work-client/src/research.generated.ts. Emission is sorted so the
// same schemas always produce the same bytes.

import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const LINE_WIDTH = 120;
const TAB_WIDTH = 3;

export interface SchemaDocument {
	models?: Record<string, JsonSchema>;
}

interface JsonSchema {
	$ref?: string;
	$defs?: Record<string, JsonSchema>;
	type?: string | string[];
	properties?: Record<string, JsonSchema>;
	required?: string[];
	additionalProperties?: boolean | JsonSchema;
	items?: JsonSchema;
	enum?: unknown[];
	const?: unknown;
	default?: unknown;
	anyOf?: JsonSchema[];
	oneOf?: JsonSchema[];
	allOf?: JsonSchema[];
}

const HEADER = [
	"// GENERATED — do not hand-edit.",
	"// Emitter: packages/work-client/scripts/generate-research-bindings.ts",
	"// Source: python/omp-work/src/omp_work/contracts/v1/schema.json and api-schema.json.",
	"",
].join("\n");

function isResearchName(name: string): boolean {
	return name.includes("Research");
}

function stable(value: unknown): string {
	if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
	if (value && typeof value === "object") {
		const obj = value as Record<string, unknown>;
		return `{${Object.keys(obj)
			.sort()
			.map(key => `${JSON.stringify(key)}:${stable(obj[key])}`)
			.join(",")}}`;
	}
	return JSON.stringify(value);
}

function refName(ref: string): string {
	const match = /^#\/\$defs\/([^/]+)$/.exec(ref);
	if (!match) throw new Error(`unsupported $ref ${ref}`);
	return match[1];
}

function literal(value: unknown): string {
	if (typeof value === "string" || typeof value === "number" || typeof value === "boolean" || value === null) {
		return JSON.stringify(value);
	}
	throw new Error(`unsupported literal ${JSON.stringify(value)}`);
}

function primitive(type: string): string {
	if (type === "integer" || type === "number") return "number";
	if (type === "string" || type === "boolean" || type === "null") return type;
	throw new Error(`unsupported type ${type}`);
}

function visualWidth(line: string): number {
	let width = 0;
	for (const char of line) width += char === "\t" ? TAB_WIDTH : 1;
	return width;
}

function needsParen(type: string): boolean {
	return type.includes("|") || type.includes("&");
}

function unionMembers(type: string, tabs: number): string {
	const pad = "\t".repeat(tabs);
	return type
		.split(" | ")
		.map(part => `${pad}| ${part}`)
		.join("\n");
}

function formatProperty(name: string, optional: boolean, type: string): string {
	const key = `${name}${optional ? "?" : ""}`;
	const inline = `\t${key}: ${type};`;
	if (!type.includes("\n") && visualWidth(inline) <= LINE_WIDTH) return inline;
	if (!type.includes("\n") && type.includes(" | ")) return `\t${key}:\n${unionMembers(type, 2)};`;
	if (type.startsWith("{\n")) {
		const body = type
			.split("\n")
			.map((line, index) => (index === 0 ? line : `\t${line}`))
			.join("\n");
		return `\t${key}: ${body};`;
	}
	return inline;
}

function formatAlias(name: string, type: string): string {
	if (type.startsWith("{\n")) return `export type ${name} = ${type};`;
	const inline = `export type ${name} = ${type};`;
	if (visualWidth(inline) <= LINE_WIDTH) return inline;
	if (type.includes(" | ")) return `export type ${name} =\n${unionMembers(type, 1)};`;
	return inline;
}

function renderSchema(node: JsonSchema, modelName?: string): string {
	if (node.$ref) return refName(node.$ref);
	if (node.const !== undefined) return literal(node.const);
	if (node.enum) return node.enum.map(literal).join(" | ");
	if (node.anyOf) return node.anyOf.map(part => renderSchema(part)).join(" | ");
	if (node.oneOf) return node.oneOf.map(part => renderSchema(part)).join(" | ");
	if (node.allOf)
		return node.allOf
			.map(part => {
				const rendered = renderSchema(part);
				return needsParen(rendered) ? `(${rendered})` : rendered;
			})
			.join(" & ");
	if (Array.isArray(node.type)) return node.type.map(primitive).join(" | ");
	if (node.type === "array" || node.items) {
		const inner = node.items ? renderSchema(node.items) : "unknown";
		return needsParen(inner) ? `(${inner})[]` : `${inner}[]`;
	}
	if (node.type === "object" || node.properties || node.additionalProperties !== undefined)
		return renderObject(node, modelName);
	if (typeof node.type === "string") return primitive(node.type);
	throw new Error(`unsupported schema node ${JSON.stringify(node).slice(0, 180)}`);
}

/** Empty-array defaults are optional on input. ResearchView always returns its lists; `components` stays optional so a view without a component registry still typechecks. */
function requiredDespiteDefault(modelName: string | undefined, property: string, schema: JsonSchema): boolean {
	return (
		modelName === "ResearchView" &&
		property !== "components" &&
		schema.type === "array" &&
		Array.isArray(schema.default) &&
		schema.default.length === 0
	);
}

function renderObject(node: JsonSchema, modelName?: string): string {
	const properties = node.properties ?? {};
	const names = Object.keys(properties).sort();
	if (names.length === 0) {
		const extra = node.additionalProperties;
		if (extra && typeof extra === "object") {
			const inner = renderSchema(extra);
			const rendered = needsParen(inner) ? `(${inner})` : inner;
			return `Record<string, ${rendered}>`;
		}
		if (extra === false) return "{}";
		return "Record<string, unknown>";
	}
	const required = new Set(node.required ?? []);
	const lines = names.map(name =>
		formatProperty(
			name,
			!required.has(name) && !requiredDespiteDefault(modelName, name, properties[name]),
			renderSchema(properties[name]),
		),
	);
	return `{\n${lines.join("\n")}\n}`;
}

function walkRefs(
	node: JsonSchema,
	defs: Record<string, JsonSchema>,
	extra: Map<string, JsonSchema>,
	selected: Set<string>,
): void {
	if (node.$ref) {
		const name = refName(node.$ref);
		if (selected.has(name)) return;
		const def = defs[name];
		if (!def) throw new Error(`unresolved $ref ${node.$ref}`);
		const prior = extra.get(name);
		if (prior && stable(prior) !== stable(def)) throw new Error(`$def ${name} differs between research models`);
		if (!prior) {
			extra.set(name, def);
			walkRefs(def, defs, extra, selected);
		}
		return;
	}
	if (node.properties) for (const child of Object.values(node.properties)) walkRefs(child, defs, extra, selected);
	if (node.items) walkRefs(node.items, defs, extra, selected);
	if (node.anyOf) for (const child of node.anyOf) walkRefs(child, defs, extra, selected);
	if (node.oneOf) for (const child of node.oneOf) walkRefs(child, defs, extra, selected);
	if (node.allOf) for (const child of node.allOf) walkRefs(child, defs, extra, selected);
	if (node.additionalProperties && typeof node.additionalProperties === "object") {
		walkRefs(node.additionalProperties, defs, extra, selected);
	}
}

function selectModels(documents: SchemaDocument[]): Map<string, JsonSchema> {
	const selected = new Map<string, JsonSchema>();
	for (const document of documents) {
		for (const name of Object.keys(document.models ?? {}).sort()) {
			if (!isResearchName(name)) continue;
			const node = document.models?.[name];
			if (!node || selected.has(name)) continue;
			selected.set(name, node);
		}
	}
	return selected;
}

/** TypeScript for every Research model in either schema, plus the $defs those models reference. */
export function renderResearchBindings(schema: SchemaDocument, apiSchema: SchemaDocument): string {
	const selected = selectModels([schema, apiSchema]);
	const extra = new Map<string, JsonSchema>();
	for (const name of [...selected.keys()].sort()) {
		const node = selected.get(name);
		if (!node) continue;
		walkRefs(node, node.$defs ?? {}, extra, new Set(selected.keys()));
	}
	const types = new Map<string, JsonSchema>([...selected, ...extra]);
	const body = [...types.keys()]
		.sort()
		.map(name => {
			const node = types.get(name);
			if (!node) throw new Error(`missing research type ${name}`);
			return formatAlias(name, renderSchema(node, name));
		})
		.join("\n\n");
	return `${HEADER}${body}\n`;
}

const packageRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = join(packageRoot, "..", "..");
const schemaPath = join(repoRoot, "python/omp-work/src/omp_work/contracts/v1/schema.json");
const apiSchemaPath = join(repoRoot, "python/omp-work/src/omp_work/contracts/v1/api-schema.json");
const outPath = join(packageRoot, "src/research.generated.ts");

if (import.meta.main) {
	const schema = JSON.parse(readFileSync(schemaPath, "utf8")) as SchemaDocument;
	const apiSchema = JSON.parse(readFileSync(apiSchemaPath, "utf8")) as SchemaDocument;
	writeFileSync(outPath, renderResearchBindings(schema, apiSchema));
}
