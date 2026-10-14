import { afterEach, expect, test } from "bun:test";
import { $ } from "bun";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { isTestSource } from "./check-test-external-reads.ts";
import { scan } from "./check-test-tmp-roots.ts";

const imports = `import * as os from "node:os";
import * as fs from "node:fs";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
`;

function names(source: string): string[] {
	return scan(imports + source, "sample.test.ts").map(hit => hit.name);
}

test("reports transitive aliases and deferred assignments at write lines", () => {
	expect(
		scan(
			imports +
				`let base; let alias; let root; let child;
base = os.tmpdir(); alias = base;
root = path.join(alias, "shared"); child = path.resolve(root, "nested", crypto.randomUUID());
fs.mkdir(child);
fs.writeFile(path.join(root, "file"), "data");`,
			"sample.test.ts",
		),
	).toEqual([
		{ line: 8, name: "shared" },
		{ line: 9, name: "shared" },
	]);
});

test("recognizes aliased os/path/fs imports and constant template/concat components", () => {
	expect(
		names(`const component = "shared";
const base = os.tmpdir();
fs.appendFile(base + "/" + component + "/file", "data");
fs.mkdir(\`\${base}/\${component}/child\`);
fs.mkdir(path.join(base, "fixed", crypto.randomUUID()));
`),
	).toEqual(["shared", "shared", "fixed"]);
	expect(
		scan(
			`import { tmpdir as tmp } from "os";
import { join as combine } from "path";
import { mkdir as make } from "fs/promises";
const joinAlias = combine;
make(joinAlias(tmp(), "aliased"));`,
			"sample.spec.js",
		),
	).toEqual([{ line: 5, name: "aliased" }]);
});

test("open flags distinguish reads, creation, numeric aliases and unknown flags", () => {
	expect(
		names(`const p = path.join(os.tmpdir(), "open-root");
const create = fs.constants.O_CREAT;
const flags = create | fs.constants.O_WRONLY;
fs.open(p, "w"); fs.openSync(p, "ax+"); fs.promises.open(p, "as");
fs.open(p, flags); fs.open(p, unknownFlags);
fs.open(p, "r"); fs.openSync(p, "r+"); fs.promises.open(p, 0);
`),
	).toEqual(Array(5).fill("open-root"));
});

test("covers write sinks and destination argument of transfer operations", () => {
	expect(
		names(`const fixed = path.join(os.tmpdir(), "destination");
const safe = path.join(await fs.promises.mkdtemp(path.join(os.tmpdir(), "private-")), "file");
fs.mkdirSync(fixed); fs.writeFileSync(fixed, ""); fs.appendFileSync(fixed, "");
fs.createWriteStream(fixed); Bun.write(fixed, "");
Bun.file(fixed).write(""); const file = Bun.file(fixed); file.writer();
fs.copyFile(safe, fixed); fs.cpSync(safe, fixed); fs.link(safe, fixed);
fs.symlinkSync(safe, fixed); fs.rename(safe, fixed);
fs.copyFile(fixed, safe); fs.cp(fixed, safe); fs.linkSync(fixed, safe);
fs.symlink(fixed, safe); fs.renameSync(fixed, safe);
`),
	).toEqual(Array(12).fill("destination"));
});

test("ignores reads, unused paths, fixtures, dynamic roots and unique-root writes", () => {
	expect(
		names(`const unused = path.join(os.tmpdir(), "unused");
fs.readFile(unused); Bun.file(unused);
// fs.mkdir(path.join(os.tmpdir(), "comment"));
const fixture = 'fs.mkdir(path.join(os.tmpdir(), "fixture"))';
fs.mkdir(path.join(os.tmpdir(), crypto.randomUUID(), "fixed-child"));
fs.mkdir(\`\${os.tmpdir()}/\${crypto.randomUUID()}-dynamic/child\`);
const root = await fs.promises.mkdtemp(path.join(os.tmpdir(), "prefix-"));
const alias = root; fs.writeFile(path.join(alias, "child"), "");
const temp = await TempDir.create(); fs.mkdir(path.join(temp.path(), "child"));
const sync = TempDir.createSync(); Bun.write(path.join(sync.absolute(), "file"), "");
function scoped() { const unused = "local"; fs.mkdir(unused); }
`),
	).toEqual([]);
});

test("resolves deferred setup and imported creation constants without leaking shadowed bindings", () => {
	expect(
		names(`let base; let alias;
function setup() { base = os.tmpdir(); alias = base; }
const root = path.join(alias, "deferred"); fs.mkdir(root);
function scoped() { const alias = "local"; fs.mkdir(path.join(alias, "child")); }
let later = ""; function writeLater() { fs.mkdir(later); }
later = path.join(os.tmpdir(), "callback"); writeLater();
`),
	).toEqual(["deferred", "callback"]);
	expect(
		scan(
			`import { O_CREAT as create } from "node:fs/constants";
import { open } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
open(join(tmpdir(), "numeric"), create | 1);`,
			"sample.test.ts",
		),
	).toEqual([{ line: 5, name: "numeric" }]);
});

test("selects TS/JS test/spec variants and test(s) helpers", () => {
	for (const file of ["a.test.js", "a.spec.tsx", "a.spec.mjs", "a.test.cjs", "test/helper.ts", "tests/helper.jsx"]) {
		expect(isTestSource(file)).toBe(true);
	}
	for (const file of ["src/helper.ts", "test/data.json", "a.spec.txt"]) expect(isTestSource(file)).toBe(false);
});

const repos: string[] = [];
afterEach(async () => {
	await Promise.all(repos.splice(0).map(dir => fs.rm(dir, { recursive: true, force: true })));
});

async function repository(source: string): Promise<string> {
	const dir = await fs.mkdtemp(path.join(os.tmpdir(), "tmp-roots-cli-"));
	repos.push(dir);
	expect((await $`git -c init.defaultBranch=main init`.cwd(dir).quiet()).exitCode).toBe(0);
	await Bun.write(path.join(dir, "test/helper.js"), source);
	await Bun.write(path.join(dir, "untracked.test.ts"), imports + `fs.mkdir(path.join(os.tmpdir(), "untracked"));`);
	await Bun.write(path.join(dir, "src/production.ts"), imports + `fs.mkdir(path.join(os.tmpdir(), "production"));`);
	expect((await $`git add test/helper.js src/production.ts`.cwd(dir).quiet()).exitCode).toBe(0);
	return dir;
}

test("CLI reports sink line, fixed component, remedy and exit 1", async () => {
	const dir = await repository(imports + `const root = path.join(os.tmpdir(), "collision");\nfs.mkdir(root);`);
	const result = await $`bun ${path.join(import.meta.dir, "check-test-tmp-roots.ts")}`.cwd(dir).quiet().nothrow();
	expect(result.exitCode).toBe(1);
	expect(result.text()).toContain('test/helper.js:6: fixed tmp component "collision"');
	expect(result.text()).toContain("mkdtemp()");
	expect(result.stderr.toString()).toContain("FAIL");
});

test("CLI passes unique-root writes and excludes untracked/production sources", async () => {
	const dir = await repository(
		imports + `const root = fs.mkdtempSync(path.join(os.tmpdir(), "private-")); fs.mkdir(path.join(root, "child"));`,
	);
	const result = await $`bun ${path.join(import.meta.dir, "check-test-tmp-roots.ts")}`.cwd(dir).quiet().nothrow();
	expect(result.exitCode).toBe(0);
	expect(result.text()).toContain("PASS");
});
