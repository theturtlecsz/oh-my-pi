import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import { sha256Hex } from "../ecc/adapter/hash";
import { applyOverlay, loadOverlay } from "../ecc/adapter/overlay";
import { buildIndexFromManifest, transformAsset } from "../ecc/adapter/transform";
import type { ManifestAsset } from "../ecc/adapter/types";

const eccRoot = path.resolve(import.meta.dir, "../ecc");
const assets: ManifestAsset[] = await Bun.file(path.join(eccRoot, "developer/services.json")).json();
const expectedSources = [
	"rules/python/coding-style.md",
	"rules/python/patterns.md",
	"rules/python/testing.md",
	"rules/python/security.md",
	"skills/postgres-patterns/SKILL.md",
	"skills/database-migrations/SKILL.md",
];

const commonDependencies: ManifestAsset[] = [
	"coding-style",
	"patterns",
	"testing",
	"security",
].map(stem => ({
	id: `ecc-wp3-common-${stem}`,
	pack: "engineering",
	upstream: {
		repository: "https://github.com/affaan-m/ECC.git",
		commit: "8321021c54d670126ce3b2969d5deb880b4b0c2a",
		path: `rules/common/${stem}.md`,
		sha256: "",
	},
	target: { path: `.omp/rules/ecc-wp3-common-${stem}.md`, scope: "project" },
	adaptation: { kind: "rule-metadata", transformVersion: 1 },
	activation: { state: "inactive", mode: "rulebook" },
	dependencies: [],
	disposition: "supported-with-adaptation",
	reason: "",
	qualification: { mechanicalTests: [], behavioralTrial: null, qualifiedRelease: null },
	rollback: { removeOwnedArtifactOnly: true },
}));

const index = buildIndexFromManifest([...assets, ...commonDependencies]);

async function render(asset: ManifestAsset): Promise<string> {
	const original = await Bun.file(path.join(eccRoot, "mirror", asset.upstream.path)).text();
	expect(sha256Hex(original)).toBe(asset.upstream.sha256);
	const edited = asset.adaptation.overlay
		? applyOverlay(
				original,
				await loadOverlay(asset.adaptation.overlay, eccRoot, asset.id, asset.upstream.path),
				sha256Hex(original),
			)
		: original;
	const result = transformAsset(asset, new TextEncoder().encode(edited), { index });
	return new TextDecoder().decode(result.files[0].bytes);
}

async function body(id: string): Promise<string> {
	const asset = assets.find(entry => entry.id === `ecc-wp3-${id}`);
	if (!asset) throw new Error(`Missing service rule ${id}`);
	return render(asset);
}

describe("WP3 Python and PostgreSQL native rules", () => {
	test("six pinned overlays target project rules with language discovery and explicit native dependencies", async () => {
		expect(assets.map(asset => asset.upstream.path)).toEqual(expectedSources);
		for (const asset of assets) {
			const stem = asset.upstream.path.startsWith("rules/python/")
				? `python-${path.posix.basename(asset.upstream.path, ".md")}`
				: asset.upstream.path.split("/")[1];
			expect(asset.id).toBe(`ecc-wp3-${stem}`);
			expect(asset.upstream.commit).toBe("8321021c54d670126ce3b2969d5deb880b4b0c2a");
			expect(asset.upstream.repository).toBe("https://github.com/affaan-m/ECC.git");
			expect(asset.target).toEqual({ path: `.omp/rules/${asset.id}.md`, scope: "project" });
			expect(asset.adaptation.kind).toBe("rule-metadata");
			expect(asset.disposition).toBe("supported-with-adaptation");
			expect(asset.activation.mode).toBe("rulebook");
			const expectedDependencies = stem.startsWith("python-")
				? [`ecc-wp3-common-${stem.slice("python-".length)}`]
				: [stem === "postgres-patterns" ? "ecc-wp3-database-migrations" : "ecc-wp3-postgres-patterns"];
			expect(asset.dependencies).toEqual(expectedDependencies);
			const text = await render(asset);
			expect(asset.activation.description).toContain(stem.startsWith("python-") ? "Python" : "PostgreSQL");
			expect(asset.activation.globs).toEqual(
				stem.startsWith("python-") ? ["**/*.py", "**/*.pyi"] : ["**/*.sql", "**/*.py", "**/*.pyi"],
			);
			expect(asset.activation).not.toHaveProperty("alwaysApply");
			expect(asset.activation).not.toHaveProperty("ttsrTrigger");
			const links = [...text.matchAll(/\]\(([^)]+)\)/g)].map(match => match[1]);
			expect(links).toEqual(expectedDependencies.map(id => `${id}.md`));
			expect(text).not.toMatch(
				/alwaysApply|TTSR|python-patterns|python-testing|django-security|database-reviewer|clickhouse-io|backend-patterns/,
			);
			expect(text).not.toMatch(
				/\b(?:npx|manage\.py|migrateToLatest|ALTER SYSTEM|pg_reload_conf)\b|\b\d+%|--cov|ALL required|MANDATORY workflow/,
			);
		}
	});

	test("Python rules retain protocols, immutable DTOs, resource management, isolated fixtures, and input security", async () => {
		const style = await body("python-coding-style");
		expect(style).toContain("@dataclass(frozen=True)");
		expect(style).toContain("NamedTuple");
		const patterns = await body("python-patterns");
		expect(patterns).toContain("class Repository(Protocol)");
		expect(patterns).toContain("class CreateUserRequest");
		expect(patterns).toContain("context managers");
		expect(patterns).toContain("generators");
		const testing = await body("python-testing");
		for (const method of [
			"pytest.mark.integration",
			"@pytest.fixture",
			"yield target",
			"tmp_path",
			"monkeypatch",
			"failure paths",
		])
			expect(testing).toContain(method);
		expect(testing).toContain("Existing approved scope and project runners select checks");
		const security = await body("python-security");
		for (const method of [
			'os.environ["OPENAI_API_KEY"]',
			"parameterized SQL",
			"CSRF middleware",
			"autoescaping",
			"secure cookie",
		])
			expect(security).toContain(method);
	});

	test("PostgreSQL rules retain query, isolation, lock, batching, and schema evolution safety without execution recipes", async () => {
		const postgres = await body("postgres-patterns");
		for (const method of [
			"USING gin",
			"INCLUDE (name, created_at)",
			"WHERE deleted_at IS NULL",
			"ON CONFLICT",
			"WHERE id > $1",
			"FOR UPDATE SKIP LOCKED",
			"pg_stat_statements",
			"least-privilege",
			"cross-tenant denial",
			"Bind query values as parameters",
		])
			expect(postgres).toContain(method);
		const migrations = await body("database-migrations");
		for (const method of [
			"forward-only",
			"immutable",
			"CREATE INDEX CONCURRENTLY",
			"cannot run inside a transaction block",
			"batch_size",
			"COMMIT;",
			"SeparateDatabaseAndState",
			"apps.get_model",
			"IrreversibleError",
			"independent commits",
			"lock and statement timeouts",
			"Expand-contract",
		])
			expect(migrations).toContain(method);
		for (const text of [postgres, migrations]) {
			expect(text).toContain("Reading or installing this guidance never runs migrations");
			expect(text).toContain("illustrative");
			expect(text).toContain("not executable instructions");
			expect(text).toContain("Existing approved scope and project runners select checks");
			expect(text).not.toMatch(/```bash|Deploy:|Deploy app|npx |migrate -path|Programmatic Migrator/);
		}
	});

	test("every semantic overlay refuses stale source bytes and spoofed source hashes", async () => {
		for (const asset of assets) {
			if (!asset.adaptation.overlay) continue;
			expect(asset.adaptation.overlay).toBe(`developer/overlays/${asset.id.slice("ecc-wp3-".length)}.json`);
			const original = await Bun.file(path.join(eccRoot, "mirror", asset.upstream.path)).text();
			const overlay = await loadOverlay(asset.adaptation.overlay, eccRoot, asset.id, asset.upstream.path);
			expect(overlay.edits[0].find.join("\n")).toBe(original);
			const stale = `${original}\nchanged upstream`;
			expect(() => applyOverlay(stale, overlay, sha256Hex(stale))).toThrow("upstream sha256 mismatch");
			expect(() => applyOverlay(stale, overlay, asset.upstream.sha256)).toThrow("adapted sha256 mismatch");
		}
	});

	test("reference closure verifies declared dependencies and catches missing targets", async () => {
		const emptyIndex = buildIndexFromManifest(assets);
		const pythonAsset = assets.find(a => a.id === "ecc-wp3-python-coding-style")!;
		const original = await Bun.file(path.join(eccRoot, "mirror", pythonAsset.upstream.path)).text();
		const overlay = await loadOverlay(pythonAsset.adaptation.overlay!, eccRoot, pythonAsset.id, pythonAsset.upstream.path);
		const edited = applyOverlay(original, overlay, sha256Hex(original));
		expect(() => transformAsset(pythonAsset, new TextEncoder().encode(edited), { index: emptyIndex })).toThrow(
			/Reference closure error/,
		);
	});
});
