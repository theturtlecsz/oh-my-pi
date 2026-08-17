/**
 * linear-freeze.test.ts — HOME-148 freeze fence: once ~/.config/omp-work/linear-frozen.json
 * exists, every GraphQL mutation fails closed with `linear_frozen` before any fetch starts;
 * reads pass through. Runs the adapter in a subprocess with a fixture HOME because Bun
 * caches node:os.homedir() at process start.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterAll, describe, expect, test } from "bun:test";

const fixture = path.resolve(import.meta.dir, "fixtures/linear-freeze-check.ts");
const home = fs.mkdtempSync(path.join(os.tmpdir(), "linear-freeze-"));
afterAll(() => fs.rmSync(home, { recursive: true, force: true }));
fs.mkdirSync(path.join(home, ".config"), { recursive: true });
fs.writeFileSync(path.join(home, ".config", "linear.env"), "LINEAR_API_KEY=fake-test-key\n");
const marker = path.join(home, ".config", "omp-work", "linear-frozen.json");

function runCheck(mode: "unfrozen" | "frozen") {
	const r = Bun.spawnSync(["bun", fixture, mode], { env: { ...process.env, HOME: home } });
	return { code: r.exitCode, out: r.stdout.toString().trim(), err: r.stderr.toString().trim() };
}

describe("linear freeze fence", () => {
	test("without the marker a mutation reaches fetch", () => {
		const r = runCheck("unfrozen");
		expect(r.err).toBe("");
		expect(r.out).toBe("OK unfrozen");
		expect(r.code).toBe(0);
	});
	test("with the marker present, mutations are refused before any network call and reads pass through", () => {
		fs.mkdirSync(path.dirname(marker), { recursive: true });
		fs.writeFileSync(marker, "{}\n");
		const r = runCheck("frozen");
		expect(r.err).toBe("");
		expect(r.out).toBe("OK frozen");
		expect(r.code).toBe(0);
	});
	test("marker removal re-enables mutations", () => {
		fs.rmSync(marker);
		const r = runCheck("unfrozen");
		expect(r.out).toBe("OK unfrozen");
		expect(r.code).toBe(0);
	});
	test("XDG_CONFIG_HOME overrides the HOME fallback for the marker", () => {
		// Marker only under XDG → frozen, even though HOME/.config has none.
		const xdg = fs.mkdtempSync(path.join(os.tmpdir(), "linear-freeze-xdg-"));
		const xdgMarker = path.join(xdg, "omp-work", "linear-frozen.json");
		fs.mkdirSync(path.dirname(xdgMarker), { recursive: true });
		fs.writeFileSync(xdgMarker, "{}\n");
		const frozen = Bun.spawnSync(["bun", fixture, "frozen"], { env: { ...process.env, HOME: home, XDG_CONFIG_HOME: xdg } });
		expect(frozen.stdout.toString().trim()).toBe("OK frozen");
		// Marker only under HOME/.config → ignored when XDG is set.
		fs.rmSync(xdgMarker);
		fs.mkdirSync(path.dirname(marker), { recursive: true });
		fs.writeFileSync(marker, "{}\n");
		const unfrozen = Bun.spawnSync(["bun", fixture, "unfrozen"], { env: { ...process.env, HOME: home, XDG_CONFIG_HOME: xdg } });
		expect(unfrozen.stdout.toString().trim()).toBe("OK unfrozen");
		fs.rmSync(marker);
		fs.rmSync(xdg, { recursive: true, force: true });
	});
});
