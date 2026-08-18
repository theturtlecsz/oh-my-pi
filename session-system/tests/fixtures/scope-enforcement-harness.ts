import * as path from "node:path";
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

const repoRoot = path.resolve(import.meta.dir, "../../..");
const extPath = path.join(repoRoot, "session-system/extensions/work-now.ts");
const result = await loadExtensions([extPath], probe);
if (result.errors.length > 0) throw new Error(result.errors.map(error => error.error).join("; "));
const ext = result.extensions[0];
if (!ext) throw new Error("work-now extension did not load");

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
} as unknown as ExtensionContext;

const sessionStart = ext.handlers.get("session_start")?.[0];
if (!sessionStart) throw new Error("session_start handler missing");
await sessionStart({}, ctx);

const tool = ext.tools.get("work");
if (!tool) throw new Error("work tool missing");
const refusal = await tool.definition.execute(
	"refusal",
	{ action: "create_work", title: "must not inherit NOW's project" },
	undefined,
	undefined,
	ctx,
);
const refusalText = refusal.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
const explicit = await tool.definition.execute(
	"explicit",
	{ action: "create_work", title: "explicit route", project: "Chosen Project" },
	undefined,
	undefined,
	ctx,
);
const explicitText = explicit.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
process.stdout.write(JSON.stringify({ statuses, refusalText, explicitText }));
