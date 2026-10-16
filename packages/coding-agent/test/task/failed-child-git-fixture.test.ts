import { describe, expect, it } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { TempDir } from "@oh-my-pi/pi-utils";
import { spawnFixtureSync } from "../helpers/git-fixture";

describe("spawnFixtureSync", () => {
	it("throws at the deadline and kills the hung child", () => {
		using dir = TempDir.createSync("@omp-fixture-hang-");
		const pidFile = path.join(dir.path(), "pid");
		const script = `require("node:fs").writeFileSync(process.argv[1], String(process.pid)); setInterval(() => {}, 1000);`;
		const start = Date.now();
		expect(() => spawnFixtureSync([process.execPath, "-e", script, pidFile], dir.path(), 500)).toThrow();
		expect(Date.now() - start).toBeLessThan(5_000);

		const pid = Number(fs.readFileSync(pidFile, "utf8"));
		expect(pid).toBeGreaterThan(0);
		let code: string | undefined;
		try {
			process.kill(pid, 0);
		} catch (err) {
			code = (err as NodeJS.ErrnoException).code;
		}
		expect(code).toBe("ESRCH");
	}, 10_000);
});
