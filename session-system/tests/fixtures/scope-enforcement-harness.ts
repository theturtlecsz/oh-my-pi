import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const probe = process.argv[2];
if (!probe) throw new Error("probe repo path required");

const WORK_ID = "00000000-0000-7000-8000-000000000001";
const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";

globalThis.fetch = (async (url: unknown) => {
	const u = String(url);
	if (u.includes("/health/ready")) {
		return new Response(JSON.stringify({ ready: true, alerts: [] }), { status: 200 });
	}
	if (u.includes("/tree")) {
		return new Response(
			JSON.stringify({
				projects: [{ project_id: "proj-1", workspace_id: WORKSPACE_ID, name: "Old Global Project" }],
				items: [
					{
						work_id: WORK_ID,
						workspace_id: WORKSPACE_ID,
						alias: { key: "HOME-1" },
						revision: { title: "Old global NOW", description: "", scope: "", acceptance_criteria: [] },
						state: "IN_PROGRESS",
						project_id: "proj-1",
					},
				],
				relations: [],
			}),
			{ status: 200 },
		);
	}
	if (u.includes(`/focus/${OWNER_ID}`)) {
		return new Response(
			JSON.stringify({
				workspace_id: WORKSPACE_ID,
				owner_id: OWNER_ID,
				work_id: WORK_ID,
				version: 1,
			}),
			{ status: 200 },
		);
	}
	if (u.includes("/v1/work-items/")) {
		return new Response(
			JSON.stringify({
				work_id: WORK_ID,
				workspace_id: WORKSPACE_ID,
				alias: { key: "HOME-1" },
				revision: { title: "Old global NOW", description: "", scope: "", acceptance_criteria: [] },
				state: "IN_PROGRESS",
				project_id: "proj-1",
			}),
			{ status: 200 },
		);
	}
	return new Response(JSON.stringify({ ok: true }), { status: 200 });
}) as typeof fetch;

// Startup Git tripwire: a `git` shim first on PATH records every exec. Scope
// discovery is native now (vcs.gitInfo walks the filesystem), so binding the
// extension — its factory runs createWorkflowHost, whose scope decision is the
// regression — must spawn no git process. A positive control execs the shim
// afterward so a silent shim cannot masquerade as "no Git". The extension's
// session_start handlers legitimately run git (dirtyPaths/headCommit), so the
// shim is removed before those run.
const tripwireRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-scope-tripwire-"));
const tripwireLog = path.join(tripwireRoot, "git.log");
const shimDir = path.join(tripwireRoot, "bin");
fs.mkdirSync(shimDir);
fs.writeFileSync(
	path.join(shimDir, "git"),
	`#!/bin/sh\nprintf '%s\\n' "$*" >> ${JSON.stringify(tripwireLog)}\nexit 0\n`,
	{
		mode: 0o755,
	},
);
const readHits = (): string[] =>
	fs.existsSync(tripwireLog)
		? fs
				.readFileSync(tripwireLog, "utf8")
				.split("\n")
				.filter(line => line.length > 0)
		: [];
const realPath = process.env.PATH ?? "";
process.env.PATH = `${shimDir}${path.delimiter}${realPath}`;

const repoRoot = path.resolve(import.meta.dir, "../../..");
const extPath = path.join(repoRoot, "session-system/extensions/work-now.ts");
const result = await loadExtensions([extPath], probe);
const startupGitHits = readHits();
// Positive control through the same API the extension's session_start Git uses,
// so a shim that silently fails cannot read as "no Git".
spawnSync("git", ["--tripwire-positive-control"]);
const tripwireControl = readHits().length > startupGitHits.length;
process.env.PATH = realPath;
fs.rmSync(tripwireRoot, { recursive: true, force: true });

const report: {
	loaded: boolean;
	loadErrors: string[];
	startupGitHits: string[];
	tripwireControl: boolean;
	statuses: string[];
	refusalText: string;
	explicitText: string;
} = {
	loaded: false,
	loadErrors: result.errors.map(error => error.error),
	startupGitHits,
	tripwireControl,
	statuses: [],
	refusalText: "",
	explicitText: "",
};

const ext = result.extensions[0];
if (!ext) {
	if (report.loadErrors.length === 0) throw new Error("work-now extension did not load");
} else {
	report.loaded = true;
	const statuses: string[] = [];
	const ctx = {
		// hand-rolled owner-session context (the runner normally supplies taskDepth)
		taskDepth: 0,
		models: undefined,
		sessionManager: { getBranch: () => [] },
		ui: {
			theme: { fg: (_color: string, text: string) => text },
			setStatus: (_key: string, text: string) => statuses.push(text),
			notify: () => {},
		},
		// The agent-stop gate's session_start handler arms its managed interval and may abort;
		// keep the runner's context surface present.
		isIdle: () => true,
		abort: () => {},
		setInterval: () => ({}),
		clearTimer: () => {},
	} as unknown as ExtensionContext;

	const sessionStarts = ext.handlers.get("session_start") ?? [];
	if (sessionStarts.length === 0) throw new Error("session_start handler missing");
	for (const handler of sessionStarts) await handler({}, ctx);

	const tool = ext.tools.get("work");
	if (!tool) throw new Error("work tool missing");
	const refusal = await tool.definition.execute(
		"refusal",
		{ action: "create_work", title: "must not inherit NOW's project" },
		undefined,
		undefined,
		ctx,
	);
	const explicit = await tool.definition.execute(
		"explicit",
		{ action: "create_work", title: "explicit route", project: "Chosen Project" },
		undefined,
		undefined,
		ctx,
	);
	report.statuses = statuses;
	report.refusalText = refusal.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
	report.explicitText = explicit.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
}
process.stdout.write(JSON.stringify(report));
