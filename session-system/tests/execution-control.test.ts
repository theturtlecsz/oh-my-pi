import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type {
	ExecutionGrantItemView,
	ExecutionGrantView,
	Fetch,
	WorkItemView,
} from "@oh-my-pi/pi-work-client";
import { createMockWorkService, type MockWorkService } from "./fixtures/omp246-mock-work-service";
import { runControl, type ControlDeps } from "../tools/execution-control";

describe("execution-control CLI", () => {
	const workspaceId = "00000000-0000-7000-8000-000000000000";
	const ownerId = "00000000-0000-7000-8000-000000000002";
	const grantId = "11112222-3333-4444-5555-666677778888";
	const grantRef = "11112222";
	const judgeSha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
	const mockBearer = "mock-bearer-token-secret-123";

	let tempDir: string;
	let mockService: MockWorkService;
	let capturedStdout: string[];
	let capturedStderr: string[];
	let lastCommandPayload: Record<string, unknown> | null;

	function createDeps(fetchFn: Fetch = mockService.fetch): ControlDeps {
		return {
			loadConfig: () => ({
				baseUrl: "http://127.0.0.1:9999",
				workspaceId,
				ownerId,
			}),
			loadToken: () => mockBearer,
			fetchImpl: fetchFn,
			pendingDir: join(tempDir, "pending-ops"),
			stdout: (msg: string) => capturedStdout.push(msg),
			stderr: (msg: string) => capturedStderr.push(msg),
		};
	}

	beforeEach(() => {
		tempDir = mkdtempSync(join(tmpdir(), "execution-control-test-"));
		capturedStdout = [];
		capturedStderr = [];
		lastCommandPayload = null;

		const grant: ExecutionGrantView = {
			grant_id: grantId,
			workspace_id: workspaceId,
			owner_id: ownerId,
			repository: "theturtlecsz/oh-my-pi",
			remote_ref: "main",
			state: "active",
			mode: "single",
			grant_version: 1,
			max_continuations: 8,
			max_close_attempts: 3,
			max_no_progress: 3,
			continuations_scheduled: 0,
			terminal_reason: null,
			authorization_hash: "authhash123",
			judge_sha256: judgeSha256,
			created_at: new Date().toISOString(),
			expires_at: new Date(Date.now() + 3600_000).toISOString(),
		};

		const workItem: WorkItemView = {
			work_id: "00000000-0000-7000-8000-000000000001",
			workspace_id: workspaceId,
			alias: { key: "OMP-280", title: "Delivery 1 controls" },
			state: "IN_PROGRESS",
			revision: {
				revision_id: "00000000-0000-7000-8000-000000000003",
				work_id: "00000000-0000-7000-8000-000000000001",
				revision_number: 1,
				summary: "controls",
				intent: "controls",
				author: "test",
				created_at: new Date().toISOString(),
				criteria: [],
			},
			candidate: null,
			project_id: null,
			archived: false,
		};

		const item: ExecutionGrantItemView = {
			item_id: "00000000-0000-7000-8000-000000000004",
			workspace_id: workspaceId,
			grant_id: grantId,
			work_id: workItem.work_id,
			position: 0,
			phase: "executing",
			claimed_revision_id: workItem.revision.revision_id,
			initial_git_baseline: "base123",
			original_request: "controls",
			original_request_sha256: "reqsha",
			close_attempts_started: 0,
			consecutive_no_progress: 0,
		};

		const baseMock = createMockWorkService({
			workspaceId,
			ownerId,
			grant,
			items: [item],
			workItems: [workItem],
		});

		const wrappedFetch: Fetch = async (input, init) => {
			if (init?.method === "POST") {
				try {
					const body = JSON.parse(String(init.body)) as { command?: { payload?: Record<string, unknown> } };
					if (body.command?.payload) {
						lastCommandPayload = body.command.payload;
					}
				} catch {}
			}
			return baseMock.fetch(input, init);
		};

		mockService = {
			...baseMock,
			fetch: wrappedFetch,
		};
	});

	afterEach(() => {
		rmSync(tempDir, { recursive: true, force: true });
	});

	test("inspect: projects public control and resolves activeWorkKey alias", async () => {
		const deps = createDeps();
		const res = await runControl(["inspect", "--json"], deps);
		expect(res.exitCode).toBe(0);
		const data = res.data as { ok: boolean; control: Record<string, unknown> };
		expect(data.ok).toBe(true);
		expect(data.control.grantRef).toBe(grantRef);
		expect(data.control.state).toBe("active");
		expect(data.control.grantVersion).toBe(1);
		expect(data.control.mode).toBe("single");
		expect(data.control.activeWorkKey).toBe("OMP-280");
		expect(data.control.canPause).toBe(true);
		expect(data.control.canStop).toBe(true);
	});

	test("active pause: transitions to paused and increments version", async () => {
		const deps = createDeps();
		const res = await runControl(
			["pause", "--json", "--grant-ref", grantRef, "--expected-version", "1"],
			deps,
		);
		expect(res.exitCode).toBe(0);
		const data = res.data as { ok: boolean; control: Record<string, unknown> };
		expect(data.ok).toBe(true);
		expect(data.control.state).toBe("paused");
		expect(data.control.grantVersion).toBe(2);
		expect(data.control.canPause).toBe(false);
		expect(data.control.canStop).toBe(true);
		expect(mockService.grant().state).toBe("paused");
		expect(mockService.grant().grant_version).toBe(2);
	});

	test("paused refusal: refusing pause when grant is already paused", async () => {
		const deps = createDeps();
		// First pause successfully
		await runControl(
			["pause", "--json", "--grant-ref", grantRef, "--expected-version", "1"],
			deps,
		);
		// Second pause must refuse with not_active
		const res = await runControl(
			["pause", "--json", "--grant-ref", grantRef, "--expected-version", "2"],
			deps,
		);
		expect(res.exitCode).toBe(0);
		const data = res.data as { ok: boolean; code: string; control: Record<string, unknown> };
		expect(data.ok).toBe(false);
		expect(data.code).toBe("not_active");
		expect(data.control.state).toBe("paused");
		expect(data.control.canPause).toBe(false);
		expect(data.control.canStop).toBe(true);
	});

	test("stale version: mismatch with expected version returns stale_version with fresh control", async () => {
		const deps = createDeps();
		// Call with expected-version 99 while grant is at version 1
		const res = await runControl(
			["pause", "--json", "--grant-ref", grantRef, "--expected-version", "99"],
			deps,
		);
		expect(res.exitCode).toBe(0);
		const data = res.data as { ok: boolean; code: string; control: Record<string, unknown> };
		expect(data.ok).toBe(false);
		expect(data.code).toBe("stale_version");
		expect(data.control.grantVersion).toBe(1);
	});

	test("stop reason/terminal state: records webui_stop: reason and moves to stopped", async () => {
		const deps = createDeps();
		const res = await runControl(
			[
				"stop",
				"--json",
				"--grant-ref",
				grantRef,
				"--expected-version",
				"1",
				"--reason",
				"critical fix needed",
			],
			deps,
		);
		expect(res.exitCode).toBe(0);
		const data = res.data as { ok: boolean; control: Record<string, unknown> };
		expect(data.ok).toBe(true);
		expect(data.control.state).toBe("stopped");
		expect(data.control.terminalReason).toBe("webui_stop: critical fix needed");
		expect(data.control.canPause).toBe(false);
		expect(data.control.canStop).toBe(false);
		expect(mockService.grant().state).toBe("stopped");
		expect(mockService.grant().terminal_reason).toBe("webui_stop: critical fix needed");
	});

	test("stop refuses a reason that becomes empty after control-character removal", async () => {
		const deps = createDeps();
		const postsBefore = mockService.posts();
		const res = await runControl(
			[
				"stop",
				"--json",
				"--grant-ref",
				grantRef,
				"--expected-version",
				"1",
				"--reason",
				"\x00\x01",
			],
			deps,
		);
		const data = res.data as { ok: boolean; code: string };
		expect(res.exitCode).toBe(0);
		expect(data).toMatchObject({ ok: false, code: "invalid_reason" });
		expect(mockService.posts()).toBe(postsBefore);
	});

	test("already-terminal no second mutation: stop on stopped grant emits already_terminal with zero POSTs", async () => {
		const deps = createDeps();
		// First stop
		await runControl(
			[
				"stop",
				"--json",
				"--grant-ref",
				grantRef,
				"--expected-version",
				"1",
				"--reason",
				"initial stop",
			],
			deps,
		);
		expect(mockService.grant().state).toBe("stopped");

		const postsBefore = mockService.posts();

		// Second stop attempt on terminal grant
		const res = await runControl(
			[
				"stop",
				"--json",
				"--grant-ref",
				grantRef,
				"--expected-version",
				"2",
				"--reason",
				"second stop attempt",
			],
			deps,
		);
		expect(res.exitCode).toBe(0);
		const data = res.data as { ok: boolean; code: string; control: Record<string, unknown> };
		expect(data.ok).toBe(false);
		expect(data.code).toBe("already_terminal");
		expect(data.control.state).toBe("stopped");
		// Zero additional mutation POSTs to backend
		expect(mockService.posts()).toBe(postsBefore);
	});

	test("wrong grant fence: refuses operation when grantRef does not match resolved grant prefix", async () => {
		const deps = createDeps();
		const res = await runControl(
			["pause", "--json", "--grant-ref", "deadbeef", "--expected-version", "1"],
			deps,
		);
		expect(res.exitCode).toBe(0);
		const data = res.data as { ok: boolean; code: string; control: Record<string, unknown> };
		expect(data.ok).toBe(false);
		expect(data.code).toBe("grant_mismatch");
		expect(data.control.grantRef).toBe(grantRef);
	});

	test("recorded judge hash use: payload carries grant's recorded judge hash, not client input", async () => {
		const deps = createDeps();
		await runControl(
			["pause", "--json", "--grant-ref", grantRef, "--expected-version", "1"],
			deps,
		);
		expect(lastCommandPayload).not.toBeNull();
		expect(lastCommandPayload?.judge_sha256).toBe(judgeSha256);
	});

	test("absence of bearer, judge hash, and full grant ID in stdout output", async () => {
		const deps = createDeps();
		await runControl(["inspect", "--json"], deps);
		await runControl(
			["pause", "--json", "--grant-ref", grantRef, "--expected-version", "1"],
			deps,
		);
		await runControl(
			[
				"stop",
				"--json",
				"--grant-ref",
				grantRef,
				"--expected-version",
				"2",
				"--reason",
				"clean stop",
			],
			deps,
		);
		// Error case as well
		await runControl(
			["pause", "--json", "--grant-ref", "wrongref", "--expected-version", "3"],
			deps,
		);

		const allOutput = capturedStdout.join("\n");
		expect(allOutput).not.toContain(mockBearer);
		expect(allOutput).not.toContain(judgeSha256);
		expect(allOutput).not.toContain(grantId);
		expect(allOutput).toContain(grantRef);
	});

	test("tightened argv parsing rejects duplicate verbs, malformed integers, and unrecognized args without leaking secrets", async () => {
		const deps = createDeps();

		// Duplicate / conflicting verbs
		const resDup = await runControl(["inspect", "pause", "--json"], deps);
		expect(resDup.exitCode).toBe(1);
		expect(capturedStderr.join("\n")).toContain("duplicate or conflicting verb");

		// Malformed integer (partial parse prevention)
		capturedStderr.length = 0;
		const resMalformedInt = await runControl(
			["pause", "--json", "--grant-ref", grantRef, "--expected-version", "123abc"],
			deps,
		);
		expect(resMalformedInt.exitCode).toBe(1);
		expect(capturedStderr.join("\n")).toContain("expected-version must be a non-negative integer");

		// Negative integer
		capturedStderr.length = 0;
		const resNegInt = await runControl(
			["pause", "--json", "--grant-ref", grantRef, "--expected-version", "-5"],
			deps,
		);
		expect(resNegInt.exitCode).toBe(1);
		expect(capturedStderr.join("\n")).toContain("expected-version must be a non-negative integer");

		// Unrecognized argument containing a secret token must not leak the secret to stderr
		capturedStderr.length = 0;
		const secretArg = "--secret-token=SUPER_CONFIDENTIAL_BEARER";
		const resUnknown = await runControl(["inspect", "--json", secretArg], deps);
		expect(resUnknown.exitCode).toBe(1);
		const errOutput = capturedStderr.join("\n");
		expect(errOutput).toContain("unrecognized or invalid argument");
		expect(errOutput).not.toContain("SUPER_CONFIDENTIAL_BEARER");

		// Missing required options
		capturedStderr.length = 0;
		const resMissingPause = await runControl(["pause", "--json"], deps);
		expect(resMissingPause.exitCode).toBe(1);
		expect(capturedStderr.join("\n")).toContain("pause requires --grant-ref and --expected-version");
	});
});
