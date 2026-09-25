/**
 * OMP-248: `buildSystemPrompt` reports degradation of the required
 * instruction-prep steps (`customPrompt`, `loadSystemPromptFiles`,
 * `loadProjectContextFiles`) via `instructionPrepDegradations`, and only those
 * steps reach stderr / `logger.warn`. Decorative prep steps (skills, workspace
 * tree, environment probes, …) degrade silently.
 */
import { afterEach, describe, expect, it, vi } from "bun:test";
import * as discovery from "@oh-my-pi/pi-coding-agent/discovery";
import { buildSystemPrompt } from "@oh-my-pi/pi-coding-agent/system-prompt";
import { logger } from "@oh-my-pi/pi-utils";

function emptyCapabilityResult() {
	return { items: [], all: [], warnings: [], providers: [] };
}

/**
 * Spy `loadCapability` on the discovery namespace, calling through for every
 * capability except `context-files`, which the supplied implementation owns.
 */
function spyLoadCapability(contextFiles: () => Promise<ReturnType<typeof emptyCapabilityResult>>) {
	const loadCapability = discovery.loadCapability;
	return vi.spyOn(discovery, "loadCapability").mockImplementation(async (id, options) => {
		if (id === "context-files") return await contextFiles();
		return await loadCapability(id, options);
	});
}

function stderrText(spy: { mock: { calls: unknown[][] } }): string {
	return spy.mock.calls.map(call => String(call[0])).join("");
}

function warnText(spy: { mock: { calls: unknown[][] } }): string {
	return spy.mock.calls.map(call => JSON.stringify(call)).join("\n");
}

describe("buildSystemPrompt instruction-prep degradation", () => {
	afterEach(() => {
		vi.restoreAllMocks();
	});

	it("reports a required context-files timeout and names it on stderr", async () => {
		spyLoadCapability(async () => {
			await Bun.sleep(6000);
			return emptyCapabilityResult();
		});
		const stderrSpy = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
		const warnSpy = vi.spyOn(logger, "warn").mockImplementation(() => {});

		const result = await buildSystemPrompt({ cwd: process.cwd() });

		expect(result.instructionPrepDegradations).toEqual([{ source: "loadProjectContextFiles", cause: "timeout" }]);
		expect(stderrText(stderrSpy)).toContain("loadProjectContextFiles");
		expect(warnText(warnSpy)).toContain("loadProjectContextFiles");

		// Let the timed-out background work settle before restoring the spies.
		await Bun.sleep(1500);
	}, 20000);

	it("reports a required context-files failure and warns with its name", async () => {
		spyLoadCapability(async () => {
			throw new Error("context-files boom");
		});
		const stderrSpy = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
		const warnSpy = vi.spyOn(logger, "warn").mockImplementation(() => {});

		const result = await buildSystemPrompt({ cwd: process.cwd() });

		expect(result.instructionPrepDegradations).toEqual([{ source: "loadProjectContextFiles", cause: "error" }]);
		expect(warnText(warnSpy)).toContain("loadProjectContextFiles");
		expect(stderrText(stderrSpy)).not.toContain("loadProjectContextFiles");
	}, 20000);

	it("keeps a decorative prep timeout silent", async () => {
		spyLoadCapability(async () => emptyCapabilityResult());
		const stderrSpy = vi.spyOn(process.stderr, "write").mockImplementation(() => true);
		const warnSpy = vi.spyOn(logger, "warn").mockImplementation(() => {});
		const workspaceTree = Bun.sleep(5500).then(() => {
			throw new Error("workspace tree boom");
		});

		const result = await buildSystemPrompt({
			cwd: process.cwd(),
			contextFiles: [],
			skills: [],
			resolvedCustomPrompt: "X",
			includeWorkspaceTree: true,
			workspaceTree,
		});

		// Let the post-timeout background rejection fire and log.
		await Bun.sleep(1000);

		expect(result.instructionPrepDegradations).toEqual([]);
		expect(stderrText(stderrSpy)).not.toContain("buildWorkspaceTree");
		expect(warnText(warnSpy)).not.toContain("buildWorkspaceTree");
	}, 20000);
});
