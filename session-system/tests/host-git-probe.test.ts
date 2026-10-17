import { afterEach, expect, test, vi } from "bun:test";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { z } from "zod";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { VcsGitRepoInfo } from "@oh-my-pi/pi-natives";
import * as vcs from "@oh-my-pi/pi-natives/vcs";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { resetConfirmations } from "../extensions/workflow/confirm";
import { createWorkflowHost } from "../extensions/workflow/host";

interface WorkTool {
	name: string;
	execute: (
		id: string,
		params: Record<string, unknown>,
		signal: unknown,
		update: unknown,
		ctx: ExtensionContext,
	) => Promise<{ content: { text: string }[] }>;
}

const repoInfo: VcsGitRepoInfo = {
	repoRoot: "/stub",
	gitEntryPath: "/stub/.git",
	gitDir: "/stub/.git",
	commonDir: "/stub/.git",
	headPath: "/stub/.git/HEAD",
	isReftable: false,
};

function makeBackend(temp: string): WorkflowBackend {
	return {
		cacheFile: path.join(temp, "cache.json"),
		queueNoun: "decision queue",
		markerFile: ".work-project",
		teamNoun: "ledger",
		scopeFix: "add a .work-project marker",
		reviewKind: "review",
		createIssue: vi.fn(async () => ({ id: "new", key: "HOME-10", title: "t" })),
		setNowRemote: vi.fn(async () => undefined),
	} as unknown as WorkflowBackend;
}

function makePi(onWork: (tool: WorkTool) => void): ExtensionAPI {
	return {
		logger: { warn() {}, error() {}, debug() {}, info() {} },
		zod: z,
		registerTool(spec: WorkTool) {
			if (spec.name === "work") onWork(spec);
		},
		registerMessageRenderer() {},
		registerCommand() {},
		registerFlag() {},
		sendMessage() {},
		appendEntry() {},
		getSessionId: () => "probe",
		on() {},
	} as unknown as ExtensionAPI;
}

afterEach(() => {
	vi.restoreAllMocks();
	resetConfirmations({ resetShared: true });
});

test("createWorkflowHost spawns no git process", async () => {
	const temp = await fs.mkdtemp(path.join(os.tmpdir(), "host-git-probe-"));
	const previousPath = process.env.PATH;
	const marker = path.join(temp, "git-called");
	try {
		const bin = path.join(temp, "bin");
		await Bun.write(path.join(bin, "git"), `#!/bin/sh\necho "$@" >> "${marker}"\nexit 0\n`);
		await fs.chmod(path.join(bin, "git"), 0o755);
		process.env.PATH = `${bin}${path.delimiter}${previousPath ?? ""}`;
		createWorkflowHost({
			backend: makeBackend(temp),
			teamNoun: "ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(makePi(() => {}));
		expect(await Bun.file(marker).exists()).toBe(false);
	} finally {
		if (previousPath === undefined) delete process.env.PATH;
		else process.env.PATH = previousPath;
		await fs.rm(temp, { recursive: true, force: true });
	}
});

test("unscoped-write refusal follows vcs.gitInfo", async () => {
	const temp = await fs.mkdtemp(path.join(os.tmpdir(), "host-git-probe-"));
	const previousCwd = process.cwd();
	try {
		process.chdir(temp);
		for (const info of [repoInfo, null]) {
			resetConfirmations({ resetShared: true });
			vi.spyOn(vcs, "gitInfo").mockReturnValue(info);
			let work: WorkTool | undefined;
			createWorkflowHost({
				backend: makeBackend(temp),
				teamNoun: "ledger",
				entryType: "work-now",
				acceptEntry: () => true,
			})(
				makePi(tool => {
					work = tool;
				}),
			);
			if (!work) throw new Error("work tool missing");
			const ctx = {
				taskDepth: 0,
				sessionManager: { getSessionId: () => "probe" },
				ui: { notify() {}, setStatus() {} },
			} as unknown as ExtensionContext;
			const result = await work.execute(
				"probe",
				{ action: "create_work", title: "T", description: "D" },
				undefined,
				undefined,
				ctx,
			);
			const text = result.content.map(item => item.text).join("\n");
			if (info) expect(text).toContain("refused: unscoped git repo");
			else {
				expect(text).not.toContain("refused: unscoped git repo");
				expect(text).toContain("CONFIRM REQUIRED");
			}
			vi.restoreAllMocks();
		}
	} finally {
		process.chdir(previousCwd);
	}
});
