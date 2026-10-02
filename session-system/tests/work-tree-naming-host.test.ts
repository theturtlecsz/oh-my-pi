import { afterEach, describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { z } from "zod";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { createWorkflowHost } from "../extensions/workflow/host";

interface RegisteredToolSpec {
	name: string;
	description: string;
	parameters: z.ZodObject<Record<string, z.ZodTypeAny>>;
	execute: (
		toolCallId: string,
		params: Record<string, unknown>,
		signal?: unknown,
		onUpdate?: unknown,
		ctx?: ExtensionContext,
	) => Promise<{ content: { type: string; text: string }[]; details?: Record<string, unknown> }>;
}

const originalCwd = process.cwd();
const temporaryDirs: string[] = [];

afterEach(() => {
	process.chdir(originalCwd);
	for (const dir of temporaryDirs.splice(0)) fs.rmSync(dir, { recursive: true, force: true });
});

function temporaryDir(marker?: string): string {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), "omp-tree-naming-"));
	temporaryDirs.push(dir);
	if (marker !== undefined) fs.writeFileSync(path.join(dir, ".work-project"), `${marker}\n`);
	return dir;
}

async function recordedTreeProject(options: { marker?: string; project?: string }): Promise<string | undefined> {
	process.chdir(temporaryDir(options.marker));

	let registeredTool: RegisteredToolSpec | undefined;
	const fakePi = {
		logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
		zod: z,
		registerTool: (spec: RegisteredToolSpec) => {
			if (spec.name === "work") registeredTool = spec;
		},
		registerMessageRenderer: () => {},
		registerCommand: () => {},
		registerFlag: () => {},
		on: () => {},
		sendMessage: () => {},
	} as unknown as ExtensionAPI;

	const calls: (string | undefined)[] = [];
	const mockBackend = {
		cacheFile: "cache.json",
		queueNoun: "decision queue",
		markerFile: ".work-project",
		reviewKind: "review",
		projectTreeLines: vi.fn(async (project?: string) => {
			calls.push(project);
			return ["TREE"];
		}),
	} as unknown as WorkflowBackend;

	createWorkflowHost({
		backend: mockBackend,
		teamNoun: "the ledger",
		entryType: "work-now",
		acceptEntry: () => true,
	})(fakePi);

	const fakeCtx = { taskDepth: 0 } as unknown as ExtensionContext;
	const params: Record<string, unknown> = { action: "tree" };
	if (options.project !== undefined) params.project = options.project;
	await registeredTool!.execute("call-1", params, undefined, undefined, fakeCtx);
	return calls[0];
}

describe("work tree project naming", () => {
	test("tree with no marker and no project lists the OMP side", async () => {
		expect(await recordedTreeProject({})).toBeUndefined();
	});

	test("tree with marker and no project lists the marker's Media Discovery side", async () => {
		expect(await recordedTreeProject({ marker: "Live TV" })).toBe("Live TV");
	});

	test("tree with marker and explicit project prefers the explicit project", async () => {
		expect(await recordedTreeProject({ marker: "Live TV", project: "Fleet" })).toBe("Fleet");
	});
});
