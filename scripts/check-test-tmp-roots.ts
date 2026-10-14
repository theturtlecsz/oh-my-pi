#!/usr/bin/env bun
/** Reject writes into shared, fixed-name tmp roots in tracked test sources. */
import * as os from "node:os";
import * as fs from "node:fs";
import { Node, Project, SyntaxKind } from "ts-morph";
import { listTestSources } from "./check-test-external-reads.ts";

export interface TmpRootHit {
	/** Line of the write sink, not the path declaration. */
	line: number;
	/** Fixed first component beneath tmpdir(). */
	name: string;
}

const tmp = Symbol("tmpdir");
type Part = string | typeof tmp | null;
interface Value {
	parts: Part[];
	unique?: boolean;
	number?: number;
}
const unknown: Value = { parts: [null] };
const writes = new Set(["mkdir", "writeFile", "appendFile", "createWriteStream"]);
const destinations = new Set(["copyFile", "cp", "link", "symlink", "rename"]);

export function scan(source: string, path: string): TmpRootHit[] {
	const project = new Project({
		useInMemoryFileSystem: true,
		skipAddingFilesFromTsConfig: true,
		compilerOptions: { allowJs: true },
	});
	const file = project.createSourceFile(path, source);
	const imports = new Map<string, string>();
	for (const declaration of file.getImportDeclarations()) {
		const module = declaration.getModuleSpecifierValue().replace(/^node:/, "");
		const prefix = module === "fs/promises" ? "fs" : module === "fs/constants" ? "fs.constants" : module;
		for (const binding of [declaration.getDefaultImport(), declaration.getNamespaceImport()]) {
			if (binding) imports.set(binding.getText(), prefix);
		}
		for (const binding of declaration.getNamedImports()) {
			imports.set(binding.getAliasNode()?.getText() ?? binding.getName(), `${prefix}.${binding.getName()}`);
		}
	}
	// Symbol declarations keep same-named locals in separate scopes distinct.
	const assignments = new Map<Node, Node[]>();
	function key(node: Node): Node | undefined {
		return node.getSymbol()?.getDeclarations()[0];
	}
	for (const expression of file.getDescendantsOfKind(SyntaxKind.BinaryExpression)) {
		if (expression.getOperatorToken().getKind() !== SyntaxKind.EqualsToken) continue;
		const declaration = key(expression.getLeft());
		if (!declaration) continue;
		const values = assignments.get(declaration) ?? [];
		values.push(expression.getRight());
		assignments.set(declaration, values);
	}
	function binding(node: Node): Node | undefined {
		const declaration = key(node);
		if (!declaration) return undefined;
		const values = assignments.get(declaration) ?? [];
		const earlier = values.filter(value => value.getStart() < node.getStart());
		// A callback can run after setup assignments appearing later in the file.
		const callback = node.getFirstAncestor(
			ancestor =>
				Node.isArrowFunction(ancestor) ||
				Node.isFunctionExpression(ancestor) ||
				Node.isFunctionDeclaration(ancestor),
		);
		if (
			callback &&
			!declaration.getAncestors().includes(callback) &&
			(values.at(-1)?.getStart() ?? -1) > node.getStart()
		) {
			return values.at(-1);
		}
		return (
			earlier.at(-1) ??
			(Node.isVariableDeclaration(declaration) ? declaration.getInitializer() : undefined) ??
			values.at(-1)
		);
	}
	function label(node: Node, seen = new Set<Node>()): string {
		if (seen.has(node)) return "";
		seen.add(node);
		if (Node.isIdentifier(node)) {
			const declaration = key(node);
			if (
				declaration &&
				(Node.isImportSpecifier(declaration) ||
					Node.isNamespaceImport(declaration) ||
					Node.isImportClause(declaration))
			) {
				return imports.get(node.getText()) ?? "";
			}
			const value = binding(node);
			return value ? label(value, seen) : node.getText() === "Bun" ? "Bun" : "";
		}
		if (Node.isPropertyAccessExpression(node)) return `${label(node.getExpression(), seen)}.${node.getName()}`;
		return "";
	}
	function evaluate(node: Node | undefined, seen = new Set<Node>()): Value {
		if (!node || seen.has(node)) return unknown;
		seen = new Set(seen).add(node);
		if (Node.isStringLiteral(node) || Node.isNoSubstitutionTemplateLiteral(node))
			return { parts: [node.getLiteralText()] };
		if (Node.isNumericLiteral(node)) return { parts: [], number: node.getLiteralValue() };
		if (Node.isIdentifier(node)) {
			if (label(node) === "fs.constants.O_CREAT") return { parts: [], number: fs.constants.O_CREAT };
			return evaluate(binding(node), seen);
		}
		if (
			Node.isAwaitExpression(node) ||
			Node.isParenthesizedExpression(node) ||
			Node.isAsExpression(node) ||
			Node.isNonNullExpression(node)
		) {
			return evaluate(node.getExpression(), seen);
		}
		if (Node.isPropertyAccessExpression(node)) {
			if (label(node) === "fs.constants.O_CREAT") return { parts: [], number: fs.constants.O_CREAT };
			if (label(node) === "path.sep") return { parts: ["/"] };
			if (node.getName() === "path") return evaluate(node.getExpression(), seen);
		}
		if (Node.isTemplateExpression(node)) {
			const parts: Part[] = [node.getHead().getLiteralText()];
			for (const span of node.getTemplateSpans())
				parts.push(...evaluate(span.getExpression(), seen).parts, span.getLiteral().getLiteralText());
			return { parts };
		}
		if (Node.isBinaryExpression(node)) {
			const left = evaluate(node.getLeft(), seen);
			const right = evaluate(node.getRight(), seen);
			const operator = node.getOperatorToken().getKind();
			if (operator === SyntaxKind.PlusToken) return { parts: [...left.parts, ...right.parts] };
			if (operator === SyntaxKind.BarToken && left.number !== undefined && right.number !== undefined) {
				return { parts: [], number: left.number | right.number };
			}
			// An O_CREAT term establishes creation even when other numeric flags are unknown.
			if (
				operator === SyntaxKind.BarToken &&
				((left.number ?? 0) & fs.constants.O_CREAT || (right.number ?? 0) & fs.constants.O_CREAT)
			)
				return { parts: [], number: fs.constants.O_CREAT };
		}
		if (Node.isCallExpression(node)) {
			const callee = label(node.getExpression()).replace("fs.promises.", "fs.");
			if (callee === "os.tmpdir") return { parts: [tmp] };
			if (/^fs\.mkdtemp(?:Sync)?$/.test(callee) || /\.TempDir\.create(?:Sync)?$/.test(callee))
				return { parts: [null], unique: true };
			if (callee === "Bun.file") return evaluate(node.getArguments()[0], seen);
			const receiver = node.getExpression();
			if (Node.isPropertyAccessExpression(receiver) && ["path", "absolute"].includes(receiver.getName())) {
				const value = evaluate(receiver.getExpression(), seen);
				if (value.unique) return value;
			}
			if (callee === "path.join" || callee === "path.resolve") {
				const args = node.getArguments().map(arg => evaluate(arg, seen));
				if (args[0]?.unique) return args[0];
				return { parts: args.flatMap((arg, index) => (index === 0 ? arg.parts : ["/", ...arg.parts])) };
			}
		}
		return unknown;
	}
	function fixed(value: Value): string | undefined {
		if (value.unique) return undefined;
		let parts: Part[] = [];
		for (const part of value.parts) {
			if (part === "") continue;
			const previous = parts.at(-1);
			if (typeof part === "string" && typeof previous === "string") parts[parts.length - 1] = previous + part;
			else parts.push(part);
		}
		if (parts[0] !== tmp) {
			// Also cover constant absolute paths under this machine's tmp directory.
			const text = parts[0];
			const prefix = `${os.tmpdir().replace(/\/$/, "")}/`;
			if (typeof text !== "string" || !text.startsWith(prefix)) return undefined;
			parts = [tmp, `/${text.slice(prefix.length)}`, ...parts.slice(1)];
		}
		let suffix = "";
		for (const part of parts.slice(1)) {
			if (part === null || part === tmp) break;
			suffix += part;
			const match = /^[/\\]+([^/\\]+)[/\\]/.exec(suffix);
			if (match) return match[1];
		}
		if (parts.includes(null)) return undefined;
		return /^[/\\]+([^/\\]+)/.exec(suffix)?.[1];
	}
	const hits: TmpRootHit[] = [];
	for (const call of file.getDescendantsOfKind(SyntaxKind.CallExpression)) {
		const callee = label(call.getExpression()).replace("fs.promises.", "fs.");
		const name = callee.replace(/^fs\./, "").replace(/Sync$/, "");
		const args = call.getArguments();
		let target: Node | undefined;
		if ((callee.startsWith("fs.") && writes.has(name)) || callee === "Bun.write") target = args[0];
		else if (callee.startsWith("fs.") && destinations.has(name)) target = args[1];
		else if (callee.startsWith("fs.") && name === "open") {
			const flags = evaluate(args[1]);
			const text = flags.parts.every(part => typeof part === "string") ? flags.parts.join("") : undefined;
			if (text === "r" || text === "r+" || (flags.number !== undefined && !(flags.number & fs.constants.O_CREAT)))
				continue;
			target = args[0];
		} else if (Node.isPropertyAccessExpression(call.getExpression())) {
			const expression = call.getExpression();
			if (Node.isPropertyAccessExpression(expression) && ["write", "writer"].includes(expression.getName()))
				target = expression.getExpression();
		}
		const component = fixed(evaluate(target));
		if (component) hits.push({ line: call.getStartLineNumber(), name: component });
	}
	return hits;
}

export async function scanRepository(): Promise<Array<TmpRootHit & { file: string }>> {
	const hits: Array<TmpRootHit & { file: string }> = [];
	for (const file of await listTestSources()) {
		if (file.endsWith(".py")) continue;
		for (const hit of scan(await Bun.file(file).text(), file)) hits.push({ file, ...hit });
	}
	return hits.sort((a, b) => a.file.localeCompare(b.file) || a.line - b.line);
}

if (import.meta.main) {
	const hits = await scanRepository();
	for (const hit of hits)
		console.log(
			`${hit.file}:${hit.line}: fixed tmp component "${hit.name}"; allocate root with mkdtemp() or TempDir.create()`,
		);
	if (hits.length) {
		console.error(`FAIL: ${hits.length} write(s) into shared fixed tmp roots`);
		process.exitCode = 1;
	} else console.log("PASS: no tracked tests write into fixed tmp roots");
}
