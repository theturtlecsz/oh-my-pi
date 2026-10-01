// Drive the real work-now extension through draft_mission against an in-memory client API.
import * as fs from "node:fs";
import * as path from "node:path";
import { loadExtensions, type ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { ExtensionRunner, Settings, TOP_LEVEL_AGENT } from "@oh-my-pi/pi-coding-agent";
import { resolveLocalUrlToPath } from "@oh-my-pi/pi-coding-agent/internal-urls";

const probe = process.argv[2];
if (!probe) throw new Error("usage: mission-intake-harness <probe-repo>");

const WORKSPACE_ID = "00000000-0000-7000-8000-000000000001";
const OWNER_ID = "00000000-0000-7000-8000-000000000002";
const PROJECT_ID = "proj-1";

interface RecordedPost {
	url: string;
	contract: string | null;
	body: unknown;
}

const posts: RecordedPost[] = [];
let retryLeft = 1;

function routeFor(title: string): { outcome: string; questions: Array<Record<string, unknown>> } {
	if (title === "Clarify") {
		return {
			outcome: "clarify",
			questions: [
				{
					rule_class: "missing_verification_oracle",
					deduplication_key: "oracle",
					statement: "Which file owns the gate?",
					priority: 0,
					claim_ids: [],
				},
			],
		};
	}
	if (title === "Held") return { outcome: "held", questions: [] };
	if (title === "Proceeding" || title === "Retry") return { outcome: "proceeded", questions: [] };
	return { outcome: "awaiting_owner", questions: [] };
}

globalThis.fetch = (async (url: unknown, init?: { body?: string; method?: string; headers?: HeadersInit }) => {
	const target = String(url);
	const method = init?.method ?? "GET";
	if (method === "POST") {
		const body = init?.body ? (JSON.parse(init.body) as Record<string, unknown>) : null;
		posts.push({ url: target, contract: new Headers(init?.headers).get("x-omp-contract-sha256"), body });
		const payload = body?.payload as { mission_id?: string; intake?: { goal?: { statement?: string } } } | undefined;
		const title = payload?.intake?.goal?.statement ?? "";
		if (target.includes("/client/mission-intake")) {
			if (title === "Retry" && retryLeft > 0) {
				retryLeft -= 1;
				return new Response(JSON.stringify({ error: { code: "unavailable", diagnostics: ["lost"] } }), { status: 500 });
			}
			const route = routeFor(title);
			return Response.json({
				outcome: "applied",
				state: null,
				evidence: [],
				blockers: [],
				decisions: [],
				artifacts: [],
				operation: "mission.intake",
				contract: "client.omp.dev/v1",
				result: {
					type: "draft_mission_intake",
					mission_id: payload?.mission_id ?? null,
					outcome: route.outcome,
					questions: route.questions,
					mission: null,
					decision_id: null,
					basis: null,
				},
				detail: null,
			});
		}
		return Response.json({ receipt: { state: "applied" }, result: { type: "unexpected" } });
	}
	if (target.includes("/v1/health/")) return Response.json({ live: true, ready: true, alerts: [] });
	if (target.includes("/stop")) {
		return Response.json({
			workspace_id: WORKSPACE_ID,
			stopped: false,
			reason: null,
			changed_at: null,
			changed_by_actor_kind: null,
		});
	}
	if (target.includes("/focus/")) {
		return Response.json({ workspace_id: WORKSPACE_ID, owner_id: OWNER_ID, work_id: null, version: 1 });
	}
	if (target.includes("/tree")) {
		return Response.json({
			workspace_id: WORKSPACE_ID,
			projects: [{ project_id: PROJECT_ID, workspace_id: WORKSPACE_ID, name: "The Bookends", health: "onTrack" }],
			items: [],
			relations: [],
		});
	}
	return new Response(JSON.stringify({ error: { code: "not_found", diagnostics: [target] } }), { status: 404 });
}) as typeof fetch;

const repoRoot = path.resolve(import.meta.dir, "../../..");
const loaded = await loadExtensions([path.join(repoRoot, "session-system/extensions/work-now.ts")], probe);
if (loaded.errors.length > 0) throw new Error(loaded.errors.map(error => error.error).join("; "));
const extension = loaded.extensions[0];
if (!extension) throw new Error("work-now extension did not load");
const tool = extension.tools.get("work");
if (!tool) throw new Error("work tool did not register");

const artifactsDir = path.join(probe, ".artifacts");
fs.mkdirSync(artifactsDir, { recursive: true });
const localProtocolOptions = { getArtifactsDir: () => artifactsDir, getSessionId: () => "session-test" };
const model = { id: "claude-fable-5", provider: "anthropic", name: "Claude Fable 5", api: "anthropic-messages" };
/** Real isolated settings for the runner (upstream reads registry handles from it), with the audit role pinned. */
function harnessSettings(auditModel?: string): Settings {
	const settings = Settings.isolated();
	if (auditModel) settings.setModelRole("audit", auditModel);
	return settings;
}

const runner = new ExtensionRunner(
	loaded.extensions,
	loaded.runtime,
	probe,
	{ getCwd: () => probe, getBranch: () => [], getSessionId: () => "session-test", getArtifactsDir: () => artifactsDir } as never,
	{ getAvailable: () => [model], hasProvider: () => true } as never,
	undefined,
	harnessSettings(),
	localProtocolOptions,
	undefined,
	TOP_LEVEL_AGENT,
);
runner.initialize(
	{
		appendEntry: () => {},
		getSessionId: () => "session-test",
		deliverMessage: async () => {},
		setModel: async () => true,
		getThinkingLevel: () => "high",
		setThinkingLevel: () => {},
		sendMessage: () => {},
		sendUserMessage: () => {},
		getActiveTools: () => ["work"],
		setActiveTools: async () => {},
	} as never,
	{
		getModel: () => model,
		isIdle: () => true,
		abort: () => {},
		hasPendingMessages: () => false,
		shutdown: () => {},
		getSystemPrompt: () => [],
	} as never,
	undefined,
	{
		theme: { fg: (_color: string, text: string) => text },
		setStatus: () => {},
		notify: () => {},
		select: async () => undefined,
		confirm: async () => true,
	} as never,
);
await runner.emit({ type: "session_start" } as never);
const ctx = runner.createContext();

async function execute(params: Record<string, unknown>): Promise<string> {
	const result = await tool.definition.execute("t", params, undefined, undefined, ctx as ExtensionContext);
	return result.content.map(part => (part.type === "text" ? part.text : "")).join("\n");
}

function writeBlueprint(name: string, content: string): void {
	const file = resolveLocalUrlToPath(`local://${name}`, localProtocolOptions);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.writeFileSync(file, content);
}

const blueprint = "# Ship the gate\n\n## Acceptance criteria\n- the focused check passes\n- the reply stays plain\n";
await runner.emit({
	type: "message_start",
	message: {
		role: "custom",
		customType: "skill-prompt",
		attribution: "user",
		details: { name: "intake", path: "/x/SKILL.md" },
		content: "intake",
		timestamp: Date.now(),
	},
} as never);
await runner.emit({
	type: "message_end",
	message: {
		role: "assistant",
		content: [{ type: "text", text: "## Figured out myself\n- fact\n## Asking you\n- decision\n## Leaving for later\n- parked" }],
	},
} as never);
writeBlueprint("intake-gate.md", blueprint);

async function confirmed(title: string): Promise<string> {
	const preview = await execute({ action: "draft_mission", title, description: blueprint, project: "The Bookends" });
	const id = /confirmation_id: (\S+)/.exec(preview)?.[1];
	if (!id) throw new Error(`no confirmation for ${title}: ${preview}`);
	return execute({
		action: "draft_mission",
		title,
		description: blueprint,
		project: "The Bookends",
		confirm: true,
		confirmation_id: id,
	});
}

const out: Record<string, unknown> = { blueprint };
out.mismatch = await execute({
	action: "draft_mission",
	title: "Awaiting",
	description: "not the blueprint",
	project: "The Bookends",
});
out.postsAfterMismatch = posts.length;
const preview = await execute({ action: "draft_mission", title: "Awaiting", description: blueprint, project: "The Bookends" });
out.preview = preview;
out.postsAfterPreview = posts.length;
const confirmationId = /confirmation_id: (\S+)/.exec(preview)?.[1];
if (!confirmationId) throw new Error(`preview missing confirmation: ${preview}`);
out.awaiting = await execute({
	action: "draft_mission",
	title: "Awaiting",
	description: blueprint,
	project: "The Bookends",
	confirm: true,
	confirmation_id: confirmationId,
});
out.postsAfterAwaiting = posts.length;
out.clarify = await confirmed("Clarify");
out.held = await confirmed("Held");
out.proceeded = await confirmed("Proceeding");
out.retryFirst = await confirmed("Retry");
out.retrySecond = await confirmed("Retry");
out.posts = posts;

process.stdout.write(JSON.stringify(out));
