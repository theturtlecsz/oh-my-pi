import * as path from "node:path";
import { loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";

const probe = process.argv[2];
if (!probe) throw new Error("probe repo path required");
const repoRoot = path.resolve(import.meta.dir, "../../..");
const extPath = path.join(repoRoot, "session-system/extensions/linear-now.ts");
const result = await loadExtensions([extPath], probe);
if (result.errors.length > 0) throw new Error(result.errors.map(error => error.error).join("; "));
const ext = result.extensions[0];
if (!ext) throw new Error("linear-now extension did not load");

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
