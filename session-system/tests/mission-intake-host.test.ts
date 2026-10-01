import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { WORK_CONTRACT_SHA256 } from "@oh-my-pi/pi-work-client";
import { afterAll, expect, test } from "bun:test";

const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "ss-mission-intake-"));
const harness = path.join(import.meta.dir, "fixtures/mission-intake-harness.ts");

afterAll(() => fs.rmSync(tempRoot, { recursive: true, force: true }));

interface IntakePost {
	url: string;
	contract: string | null;
	body: {
		request_id: string;
		payload: {
			mission_id: string;
			intake: {
				source: { text: string; sha256: string; spans: unknown[] };
				goal: { id: string; statement: string };
				acceptance_criteria: Array<{ id: string; statement: string; observable_outcome: string }>;
			};
			scope: {
				project_id: string;
				risk_policy: string;
				approval_policy: string;
				effort_policy: string;
				kind: string;
			};
		};
	};
}

function run(): Record<string, unknown> {
	const root = path.join(tempRoot, "case");
	const home = path.join(root, "home");
	const probe = path.join(root, "repo");
	fs.mkdirSync(path.join(home, ".omp", "agent"), { recursive: true });
	fs.mkdirSync(path.join(home, ".config", "omp-work"), { recursive: true });
	fs.mkdirSync(probe, { recursive: true });
	fs.writeFileSync(
		path.join(home, ".config", "omp-work", "client.json"),
		JSON.stringify({
			base_url: "http://127.0.0.1:54322",
			workspace_id: "00000000-0000-7000-8000-000000000001",
			owner_id: "00000000-0000-7000-8000-000000000002",
		}),
	);
	const child = Bun.spawnSync([process.execPath, harness, probe], {
		cwd: probe,
		env: {
			...process.env,
			HOME: home,
			XDG_CONFIG_HOME: path.join(home, ".config"),
			OMP_WORK_BEARER: "test-token",
			PI_CODING_AGENT_DIR: path.join(home, ".omp", "agent"),
		},
	});
	expect(child.exitCode, child.stderr.toString()).toBe(0);
	return JSON.parse(child.stdout.toString()) as Record<string, unknown>;
}

function postsOf(value: unknown): IntakePost[] {
	return value as IntakePost[];
}

test("draft_mission posts the saved blueprint once and renders each route", () => {
	const out = run();
	const blueprint = String(out.blueprint);
	const digest = createHash("sha256").update(blueprint, "utf8").digest("hex");
	expect(out.mismatch).toContain(
		"intake_blueprint_mismatch: description must exactly match local://intake-gate.md; save the changed bytes and re-run intake lint",
	);
	expect(out.postsAfterMismatch).toBe(0);
	expect(out.preview).toContain("CONFIRM REQUIRED");
	expect(out.postsAfterPreview).toBe(0);
	expect(out.postsAfterAwaiting).toBe(1);

	const posts = postsOf(out.posts);
	expect(posts.every(post => post.url.includes("/client/mission-intake"))).toBe(true);
	expect(posts.some(post => JSON.stringify(post.body).includes("create_work_batch"))).toBe(false);

	const first = posts[0];
	expect(first?.url).toBe("http://127.0.0.1:54322/v1/workspaces/00000000-0000-7000-8000-000000000001/client/mission-intake");
	expect(first?.contract).toBe(WORK_CONTRACT_SHA256);
	expect(first?.body.payload.intake.source.text).toBe(blueprint);
	expect(first?.body.payload.intake.source.sha256).toBe(digest);
	expect(first?.body.payload.intake.source.spans).toEqual([]);
	expect(first?.body.payload.intake.goal).toEqual({ id: "goal", statement: "Awaiting" });
	expect(first?.body.payload.intake.acceptance_criteria).toEqual([
		{ id: "ac-1", statement: "the focused check passes", observable_outcome: "the focused check passes" },
		{ id: "ac-2", statement: "the reply stays plain", observable_outcome: "the reply stays plain" },
	]);
	expect(first?.body.payload.scope).toEqual({
		project_id: "proj-1",
		risk_policy: "default",
		approval_policy: "default",
		effort_policy: "default",
		kind: "engineering.execute",
	});
	expect(out.awaiting).toBe("awaits the owner's confirmation");
	expect(out.clarify).toBe("Which file owns the gate?");
	expect(out.held).toBe("held until a budget exists");
	expect(out.proceeded).toBe("proceeding");

	const retry = posts.filter(post => post.body.payload.intake.goal.statement === "Retry");
	expect(retry).toHaveLength(2);
	expect(retry[0]?.body.request_id).toBe(retry[1]?.body.request_id);
	expect(retry[0]?.body.payload.mission_id).toBe(retry[1]?.body.payload.mission_id);
	expect(retry[0]?.body.request_id).not.toBe(first?.body.request_id);
	expect(out.retrySecond).toBe("proceeding");
});
