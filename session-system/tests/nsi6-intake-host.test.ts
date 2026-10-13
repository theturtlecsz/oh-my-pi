import { afterAll, afterEach, beforeEach, describe, expect, test, vi } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { z } from "zod";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { resolveLocalUrlToPath } from "@oh-my-pi/pi-coding-agent/internal-urls";
import {
	type AssessBoundedIntakeResult,
	type BoundedIntakeDraft,
	sha256Hex,
} from "@oh-my-pi/pi-work-client";
import type { WorkflowBackend } from "../extensions/workflow/backend";
import { resetConfirmations } from "../extensions/workflow/confirm";
import {
	createWorkflowHost,
	readNsiRouteMode,
} from "../extensions/workflow/host";
import { createWorkBackend } from "../extensions/workflow/work";

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

type HostEventHandler = (event: unknown, ctx: unknown) => Promise<unknown> | unknown;

const tempDirs: string[] = [];

function makeTempDir(prefix: string): string {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
	tempDirs.push(dir);
	return dir;
}

afterAll(() => {
	for (const dir of tempDirs) {
		try {
			fs.rmSync(dir, { recursive: true, force: true });
		} catch {
			// ignore cleanup errors
		}
	}
});

function writeRouteState(
	configHome: string,
	state: { mode: "current" | "typed"; [key: string]: unknown },
): string {
	const dir = path.join(configHome, "omp", "work-ledger");
	fs.mkdirSync(dir, { recursive: true });
	const stateFile = path.join(dir, "nsi-route.json");
	const payload = {
		kind: "nsi6-route-state",
		version: 1,
		...state,
	};
	fs.writeFileSync(stateFile, JSON.stringify(payload, null, 2));
	return stateFile;
}

function makeIntakeDraft(title: string, description: string): BoundedIntakeDraft {
	return {
		archetype: "small_code_change",
		source: {
			text: description,
			sha256: sha256Hex(description),
			spans: [],
		},
		goal: { id: "goal", statement: title },
		acceptance_criteria: [
			{ id: "ac-1", statement: "The check passes", observable_outcome: "The check passes" },
		],
	};
}

describe("NSI-6 route state reader", () => {
	let originalXdg: string | undefined;

	beforeEach(() => {
		originalXdg = process.env.XDG_CONFIG_HOME;
	});

	afterEach(() => {
		if (originalXdg === undefined) {
			delete process.env.XDG_CONFIG_HOME;
		} else {
			process.env.XDG_CONFIG_HOME = originalXdg;
		}
	});

	test("absent nsi-route.json defaults to current mode", () => {
		const tempHome = makeTempDir("nsi6-route-absent-");
		process.env.XDG_CONFIG_HOME = tempHome;
		expect(readNsiRouteMode()).toBe("current");
	});

	test("unreadable or invalid state file throws", () => {
		const tempHome = makeTempDir("nsi6-route-invalid-");
		process.env.XDG_CONFIG_HOME = tempHome;
		const dir = path.join(tempHome, "omp", "work-ledger");
		fs.mkdirSync(dir, { recursive: true });
		fs.writeFileSync(path.join(dir, "nsi-route.json"), "invalid json content");
		expect(() => readNsiRouteMode()).toThrow("NSI-6 route state invalid JSON");

		fs.writeFileSync(
			path.join(dir, "nsi-route.json"),
			JSON.stringify({ kind: "wrong-kind", version: 1, mode: "typed" }),
		);
		expect(() => readNsiRouteMode()).toThrow("unsupported kind or version");
	});

	test("valid route state returns configured mode", () => {
		const tempHome = makeTempDir("nsi6-route-valid-");
		process.env.XDG_CONFIG_HOME = tempHome;
		writeRouteState(tempHome, { mode: "typed" });
		expect(readNsiRouteMode()).toBe("typed");

		writeRouteState(tempHome, { mode: "current" });
		expect(readNsiRouteMode()).toBe("current");
	});
});

describe("NSI-6 intake host enforcement", () => {
	let tempHome: string;
	let artifactsDir: string;
	let originalXdg: string | undefined;
	let registeredTool: RegisteredToolSpec | undefined;
	const listeners: Record<string, HostEventHandler[]> = {};
	let mockBackend: WorkflowBackend;
	let fakeCtx: ExtensionContext;
	let localProtocolOptions: { getArtifactsDir: () => string; getSessionId: () => string };

	const blueprintContent =
		"# Ship the gate\n\n## Acceptance criteria\n- the check passes\n";

	function writeBlueprint(name: string, content: string): void {
		const target = resolveLocalUrlToPath(`local://${name}`, localProtocolOptions);
		fs.mkdirSync(path.dirname(target), { recursive: true });
		fs.writeFileSync(target, content);
	}

	async function emit(eventName: string, event: unknown): Promise<void> {
		for (const h of listeners[eventName] ?? []) {
			await h(event, fakeCtx);
		}
	}

	async function activateIntakeSession(args?: string): Promise<void> {
		await emit("message_start", {
			message: {
				role: "custom",
				customType: "skill-prompt",
				attribution: "user",
				details: { name: "intake", ...(args ? { args } : {}) },
			},
		});
		if (!args?.includes("--publish")) {
			await emit("message_end", {
				message: {
					role: "assistant",
					content: [
						{
							type: "text",
							text: "## Figured out myself\n- fact\n## Asking you\n- question\n## Leaving for later\n- later",
						},
					],
				},
			});
		}
	}

	async function executeWork(params: Record<string, unknown>): Promise<string> {
		if (!registeredTool) throw new Error("work tool not registered");
		const res = await registeredTool.execute("call-1", params, undefined, undefined, fakeCtx);
		return res.content.map(c => c.text).join("\n");
	}

	beforeEach(() => {
		originalXdg = process.env.XDG_CONFIG_HOME;
		tempHome = makeTempDir("nsi6-host-home-");
		process.env.XDG_CONFIG_HOME = tempHome;

		artifactsDir = path.join(tempHome, ".artifacts");
		fs.mkdirSync(artifactsDir, { recursive: true });
		localProtocolOptions = {
			getArtifactsDir: () => artifactsDir,
			getSessionId: () => "session-test",
		};

		resetConfirmations({ resetShared: true });
		for (const k of Object.keys(listeners)) delete listeners[k];
		registeredTool = undefined;

		const fakePi = {
			logger: { warn: () => {}, error: () => {}, debug: () => {}, info: () => {} },
			zod: z,
			registerTool: (spec: RegisteredToolSpec) => {
				if (spec.name === "work") registeredTool = spec;
			},
			registerMessageRenderer: () => {},
			registerCommand: () => {},
			registerFlag: () => {},
			on: (event: string, handler: HostEventHandler) => {
				listeners[event] = listeners[event] || [];
				listeners[event]!.push(handler);
			},
			sendMessage: () => {},
			appendEntry: () => {},
			getSessionId: () => "session-test",
		} as unknown as ExtensionAPI;

		mockBackend = {
			cacheFile: path.join(tempHome, "cache.json"),
			queueNoun: "decision queue",
			markerFile: ".work-project",
			teamNoun: "the ledger",
			reviewKind: "review",
			findIssue: vi.fn().mockResolvedValue({ id: "w-parent", key: "HOME-1", title: "Parent work" }),
			createIssue: vi.fn().mockResolvedValue({ id: "w-new", key: "HOME-10", title: "New item" }),
			createBatch: vi.fn().mockResolvedValue({
				parent: { id: "p-batch", key: "HOME-20", title: "Batch parent" },
				children: [
					{ id: "c-1", key: "HOME-21", title: "Child 1" },
					{ id: "c-2", key: "HOME-22", title: "Child 2" },
				],
				text: "created HOME-20 + 2 child(ren)",
			}),
			draftMission: vi.fn().mockResolvedValue({
				outcome: "proceeded",
				questions: [],
			}),
			createSameSessionChild: vi.fn().mockResolvedValue({
				id: "w-sub",
				key: "HOME-30",
				title: "Same session child",
			}),
			setNowRemote: vi.fn().mockResolvedValue(undefined),
			assessBoundedIntake: vi.fn().mockResolvedValue({
				ready_for_ratification: true,
				issue_count: 0,
				blocking_question_count: 0,
				warning_count: 0,
				questions: [],
				warnings: [],
				decision_posture: "ready",
				required_gates: [],
			} satisfies AssessBoundedIntakeResult),
		} as unknown as WorkflowBackend;

		createWorkflowHost({
			backend: mockBackend,
			teamNoun: "the ledger",
			entryType: "work-now",
			acceptEntry: () => true,
		})(fakePi);

		fakeCtx = {
			taskDepth: 0,
			localProtocolOptions,
			sessionManager: { getSessionId: () => "session-test" },
			ui: { notify: () => {}, setStatus: () => {} },
		} as unknown as ExtensionContext;

		writeBlueprint("intake-test.md", blueprintContent);
	});

	afterEach(() => {
		if (originalXdg === undefined) {
			delete process.env.XDG_CONFIG_HOME;
		} else {
			process.env.XDG_CONFIG_HOME = originalXdg;
		}
	});

	test("in current mode (absent state), single create_work accepts prose intake without structured intake draft", async () => {
		await activateIntakeSession();
		const preview = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
		});
		expect(preview).toContain("CONFIRM REQUIRED");
		expect(preview).toContain("Model wants to file a work item");
		expect(mockBackend.createIssue).not.toHaveBeenCalled();

		const confirmId = /confirmation_id: (\S+)/.exec(preview)?.[1];
		expect(confirmId).toBeDefined();

		const confirmed = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			confirm: true,
			confirmation_id: confirmId,
		});
		expect(confirmed).toContain("created HOME-10 + NOW");
		expect(mockBackend.createIssue).toHaveBeenCalledTimes(1);
		expect(mockBackend.setNowRemote).toHaveBeenCalledTimes(1);
	});

	test("in typed mode, backend lacking assessBoundedIntake is refused with intake_backend_unsupported", async () => {
		writeRouteState(tempHome, { mode: "typed" });
		// Remove assessBoundedIntake to simulate an older backend
		delete (mockBackend as Partial<WorkflowBackend>).assessBoundedIntake;

		await activateIntakeSession();

		const singleResult = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: makeIntakeDraft("Ship the gate", blueprintContent),
		});
		expect(singleResult).toContain("intake_backend_unsupported: typed intake mode requires backend assessment support");

		const batchResult = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: makeIntakeDraft("Ship the gate", blueprintContent),
			batch: [
				{
					title: "Child 1",
					description: blueprintContent,
					blocks: [1],
					intake: makeIntakeDraft("Child 1", blueprintContent),
				},
				{
					title: "Child 2",
					description: blueprintContent,
					blocks: [],
					intake: makeIntakeDraft("Child 2", blueprintContent),
				},
			],
		});
		expect(batchResult).toContain("intake_backend_unsupported: typed intake mode requires backend assessment support");

		const missionResult = await executeWork({
			action: "draft_mission",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: makeIntakeDraft("Ship the gate", blueprintContent),
		});
		expect(missionResult).toContain("intake_backend_unsupported: typed intake mode requires backend assessment support");
	});

	test("in typed mode, single create_work requires matching structured intake draft", async () => {
		writeRouteState(tempHome, { mode: "typed" });
		await activateIntakeSession();

		// 1. Missing intake draft
		const missingRes = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
		});
		expect(missingRes).toContain("intake_typed_required: typed intake requires matching structured intake draft (intake)");
		expect(mockBackend.createIssue).not.toHaveBeenCalled();

		// 2. Mismatched goal statement
		const mismatchedGoalDraft = makeIntakeDraft("Different Goal", blueprintContent);
		const goalRes = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: mismatchedGoalDraft,
		});
		expect(goalRes).toContain('intake_draft_mismatch: draft goal "Different Goal" does not match title "Ship the gate"');

		// 3. Mismatched source text
		const mismatchedSourceDraft = makeIntakeDraft("Ship the gate", "other text");
		const sourceRes = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: mismatchedSourceDraft,
		});
		expect(sourceRes).toContain("intake_draft_mismatch: draft source text does not match blueprint description");

		// 4. Mismatched source sha256
		const badShaDraft = makeIntakeDraft("Ship the gate", blueprintContent);
		badShaDraft.source.sha256 = "0".repeat(64);
		const shaRes = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: badShaDraft,
		});
		expect(shaRes).toContain("intake_draft_mismatch: draft source sha256");

		// 5. Negative assessment
		(mockBackend.assessBoundedIntake as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
			ready_for_ratification: false,
			issue_count: 1,
			blocking_question_count: 1,
			warning_count: 0,
			questions: [
				{
					rule_class: "missing_verification_oracle",
					deduplication_key: "oracle-1",
					statement: "Missing oracle test verification",
					priority: 1,
					claim_ids: [],
				},
			],
			warnings: [],
			decision_posture: "clarify",
			required_gates: [],
		});
		const negativeRes = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: makeIntakeDraft("Ship the gate", blueprintContent),
		});
		expect(negativeRes).toContain("intake_assessment_refused: typed intake assessment failed (1 issue(s)): Missing oracle test verification");
		expect(mockBackend.createIssue).not.toHaveBeenCalled();
	});

	test("in typed mode, preview writes nothing and confirmed write creates issue and sets NOW", async () => {
		writeRouteState(tempHome, { mode: "typed" });
		await activateIntakeSession();

		const draft = makeIntakeDraft("Ship the gate", blueprintContent);
		const preview = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: draft,
		});

		expect(preview).toContain("CONFIRM REQUIRED");
		expect(preview).toContain("Model wants to file a work item");
		expect(preview).toContain("becomes NOW");
		// Preview must write nothing to backend
		expect(mockBackend.createIssue).not.toHaveBeenCalled();
		expect(mockBackend.setNowRemote).not.toHaveBeenCalled();

		const confirmId = /confirmation_id: (\S+)/.exec(preview)?.[1];
		expect(confirmId).toBeDefined();

		const confirmed = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: draft,
			confirm: true,
			confirmation_id: confirmId,
		});

		expect(confirmed).toContain("created HOME-10 + NOW");
		expect(mockBackend.createIssue).toHaveBeenCalledTimes(1);
		expect(mockBackend.setNowRemote).toHaveBeenCalledTimes(1);
	});

	test("changed typed proposal bytes between preview and confirm invalidates confirmation fingerprint", async () => {
		writeRouteState(tempHome, { mode: "typed" });
		await activateIntakeSession();

		const draft = makeIntakeDraft("Ship the gate", blueprintContent);
		const preview = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: draft,
		});

		const confirmId = /confirmation_id: (\S+)/.exec(preview)?.[1];
		expect(confirmId).toBeDefined();

		// Change intake acceptance criteria between preview and confirm
		const tamperedDraft = JSON.parse(JSON.stringify(draft)) as BoundedIntakeDraft;
		tamperedDraft.acceptance_criteria.push({
			id: "ac-tampered",
			statement: "Sneaky addition",
			observable_outcome: "Tampered outcome",
		});

		const confirmResult = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: tamperedDraft,
			confirm: true,
			confirmation_id: confirmId,
		});

		expect(confirmResult).toContain("REFUSED — payload changed since the preview");
		expect(mockBackend.createIssue).not.toHaveBeenCalled();
	});

	test("in typed mode, batch create_work checks parent and all children", async () => {
		writeRouteState(tempHome, { mode: "typed" });
		await activateIntakeSession();

		const parentDraft = makeIntakeDraft("Ship the gate", blueprintContent);
		const childDraft1 = makeIntakeDraft("Child 1", blueprintContent);
		const childDraft2 = makeIntakeDraft("Child 2", blueprintContent);

		// Child missing intake
		const childMissing = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: parentDraft,
			batch: [
				{
					title: "Child 1",
					description: blueprintContent,
					blocks: [1],
				},
				{
					title: "Child 2",
					description: blueprintContent,
					blocks: [],
					intake: childDraft2,
				},
			],
		});
		expect(childMissing).toContain('batch entry [0] "Child 1": intake_typed_required');
		expect(mockBackend.createBatch).not.toHaveBeenCalled();

		// Child assessment failure
		(mockBackend.assessBoundedIntake as ReturnType<typeof vi.fn>)
			.mockResolvedValueOnce({
				ready_for_ratification: true,
				issue_count: 0,
				blocking_question_count: 0,
				warning_count: 0,
				questions: [],
				warnings: [],
				decision_posture: "ready",
				required_gates: [],
			})
			.mockResolvedValueOnce({
				ready_for_ratification: true,
				issue_count: 0,
				blocking_question_count: 0,
				warning_count: 0,
				questions: [],
				warnings: [],
				decision_posture: "ready",
				required_gates: [],
			})
			.mockResolvedValueOnce({
				ready_for_ratification: false,
				issue_count: 1,
				blocking_question_count: 1,
				warning_count: 0,
				questions: [
					{
						rule_class: "missing_acceptance_criteria",
						deduplication_key: "ac",
						statement: "Child missing required verification",
						priority: 1,
						claim_ids: [],
					},
				],
				warnings: [],
				decision_posture: "clarify",
				required_gates: [],
			});

		const childNegative = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: parentDraft,
			batch: [
				{
					title: "Child 1",
					description: blueprintContent,
					blocks: [1],
					intake: childDraft1,
				},
				{
					title: "Child 2",
					description: blueprintContent,
					blocks: [],
					intake: childDraft2,
				},
			],
		});
		expect(childNegative).toContain('batch entry [1] "Child 2": intake_assessment_refused: typed intake assessment failed');
		expect(mockBackend.createBatch).not.toHaveBeenCalled();

		// All valid
		(mockBackend.assessBoundedIntake as ReturnType<typeof vi.fn>).mockResolvedValue({
			ready_for_ratification: true,
			issue_count: 0,
			blocking_question_count: 0,
			warning_count: 0,
			questions: [],
			warnings: [],
			decision_posture: "ready",
			required_gates: [],
		});

		const batchPreview = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: parentDraft,
			batch: [
				{
					title: "Child 1",
					description: blueprintContent,
					blocks: [1],
					intake: childDraft1,
				},
				{
					title: "Child 2",
					description: blueprintContent,
					blocks: [],
					intake: childDraft2,
				},
			],
		});
		expect(batchPreview).toContain("CONFIRM REQUIRED");
		expect(batchPreview).toContain("Model wants to publish a BATCH — 1 parent + 2 children");
		expect(mockBackend.createBatch).not.toHaveBeenCalled();

		const confirmId = /confirmation_id: (\S+)/.exec(batchPreview)?.[1];
		expect(confirmId).toBeDefined();

		const batchConfirmed = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: parentDraft,
			batch: [
				{
					title: "Child 1",
					description: blueprintContent,
					blocks: [1],
					intake: childDraft1,
				},
				{
					title: "Child 2",
					description: blueprintContent,
					blocks: [],
					intake: childDraft2,
				},
			],
			confirm: true,
			confirmation_id: confirmId,
		});
		expect(batchConfirmed).toContain("created HOME-20 + 2 child(ren) + parent is NOW");
		expect(mockBackend.createBatch).toHaveBeenCalledTimes(1);
		expect(mockBackend.setNowRemote).toHaveBeenCalledTimes(1);
	});

	test("in typed mode, draft_mission enforces typed intake and passes draft to backend", async () => {
		writeRouteState(tempHome, { mode: "typed" });
		await activateIntakeSession();

		// 1. Missing intake
		const missingRes = await executeWork({
			action: "draft_mission",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
		});
		expect(missingRes).toContain("intake_typed_required: typed intake requires matching structured intake draft (intake)");
		expect(mockBackend.draftMission).not.toHaveBeenCalled();

		// 2. Valid intake
		const draft = makeIntakeDraft("Ship the gate", blueprintContent);
		const preview = await executeWork({
			action: "draft_mission",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: draft,
		});
		expect(preview).toContain("CONFIRM REQUIRED");
		expect(preview).toContain("Model wants to draft a mission");
		expect(mockBackend.draftMission).not.toHaveBeenCalled();

		const confirmId = /confirmation_id: (\S+)/.exec(preview)?.[1];
		expect(confirmId).toBeDefined();

		const confirmed = await executeWork({
			action: "draft_mission",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: draft,
			confirm: true,
			confirmation_id: confirmId,
		});
		expect(confirmed).toBe("proceeding");
		expect(mockBackend.draftMission).toHaveBeenCalledWith({
			title: "Ship the gate",
			blueprint: blueprintContent,
			project: "TestProject",
			draft,
		});
	});

	test("intake --publish enforces typed mode, preventing prose intake bypass", async () => {
		writeRouteState(tempHome, { mode: "typed" });
		// --publish sets intakeScanRequired to false, but intakeActive remains true
		await activateIntakeSession("--publish local://intake-test.md");

		// Attempt prose intake bypass under --publish
		const proseBypass = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
		});
		expect(proseBypass).toContain("intake_typed_required: typed intake requires matching structured intake draft (intake)");
		expect(mockBackend.createIssue).not.toHaveBeenCalled();

		// Structured intake succeeds under --publish
		const draft = makeIntakeDraft("Ship the gate", blueprintContent);
		const preview = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: draft,
		});
		expect(preview).toContain("CONFIRM REQUIRED");

		const confirmId = /confirmation_id: (\S+)/.exec(preview)?.[1];
		expect(confirmId).toBeDefined();

		const confirmed = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
			intake: draft,
			confirm: true,
			confirmation_id: confirmId,
		});
		expect(confirmed).toContain("created HOME-10 + NOW");
	});

	test("same_session_found_fixed routine finding remains outside intake in typed mode", async () => {
		writeRouteState(tempHome, { mode: "typed" });
		await activateIntakeSession();

		const preview = await executeWork({
			action: "create_work",
			kind: "same_session_found_fixed",
			work: "HOME-1",
			title: "Fix leak in buffer",
			body: "## Finding\nBuffer leaked bytes\n\n## Verification\nRan memory audit pass",
		});
		expect(preview).toContain("CONFIRM REQUIRED");
		expect(preview).toContain("Model wants to file a same-session found-and-fixed child");
		expect(mockBackend.createSameSessionChild).not.toHaveBeenCalled();

		const confirmId = /confirmation_id: (\S+)/.exec(preview)?.[1];
		expect(confirmId).toBeDefined();

		const confirmed = await executeWork({
			action: "create_work",
			kind: "same_session_found_fixed",
			work: "HOME-1",
			title: "Fix leak in buffer",
			body: "## Finding\nBuffer leaked bytes\n\n## Verification\nRan memory audit pass",
			confirm: true,
			confirmation_id: confirmId,
		});
		expect(confirmed).toContain("created HOME-30 as a same-session child of HOME-1");
		expect(mockBackend.createSameSessionChild).toHaveBeenCalledTimes(1);
	});

	test("explicit rollback restores current mode and allows prose intake again", async () => {
		// First in typed mode
		writeRouteState(tempHome, { mode: "typed" });
		expect(readNsiRouteMode()).toBe("typed");

		// Rollback to current mode
		const rollbackReceipt = {
			kind: "nsi6-route-state",
			version: 1,
			mode: "current",
			prior_binding: { mode: "typed" },
			state_sha256: "0".repeat(64),
		};
		writeRouteState(tempHome, rollbackReceipt);
		expect(readNsiRouteMode()).toBe("current");

		// Rehearsal: appends proof to process.env.NSI6_ROLLBACK_OUT if present, else temporary file
		const rollbackOutPath =
			process.env.NSI6_ROLLBACK_OUT || path.join(tempHome, "rollback-proof.json");
		const proofPayload = {
			verified: true,
			receipt: JSON.stringify(rollbackReceipt),
		};
		fs.appendFileSync(rollbackOutPath, `${JSON.stringify(proofPayload)}\n`);

		// Verify written proof
		const writtenLines = fs.readFileSync(rollbackOutPath, "utf8").trim().split("\n");
		const parsedProof = JSON.parse(writtenLines[writtenLines.length - 1]!) as {
			verified: boolean;
			receipt: string;
		};
		expect(parsedProof.verified).toBe(true);
		expect(parsedProof.receipt).toContain('"mode":"current"');

		// In rolled-back current mode, prose intake is restored
		await activateIntakeSession();
		const preview = await executeWork({
			action: "create_work",
			title: "Ship the gate",
			description: blueprintContent,
			project: "TestProject",
		});
		expect(preview).toContain("CONFIRM REQUIRED");
		expect(preview).toContain("Model wants to file a work item");
	});

	test("work.draftMission consumes supplied input.draft instead of rebuilding from Markdown", async () => {
		const postedBodies: unknown[] = [];
		const testClientConfig = {
			baseUrl: "http://127.0.0.1:54321",
			workspaceId: "00000000-0000-7000-8000-000000000001",
			ownerId: "00000000-0000-7000-8000-000000000002",
		};
		const pendingDir = path.join(tempHome, "pending");
		fs.mkdirSync(pendingDir, { recursive: true });

		const mockFetch = (async (url: string | URL | Request, init?: RequestInit) => {
			const u = String(url);
			if (u.includes("/tree")) {
				return Response.json({
					workspace_id: testClientConfig.workspaceId,
					projects: [{ project_id: "p-1", name: "TestProject", health: "onTrack" }],
					items: [],
					relations: [],
				});
			}
			if (u.includes("/client/mission-intake")) {
				const body = JSON.parse(String(init?.body)) as { payload: { intake: unknown } };
				postedBodies.push(body);
				return Response.json({
					receipt: { state: "applied", operation_id: "00000000-0000-7000-8000-000000000099" },
					result: {
						type: "draft_mission_intake",
						outcome: "proceeded",
						questions: [],
					},
				});
			}
			return new Response("Not found", { status: 404 });
		}) as unknown as typeof fetch;

		const workBackend = createWorkBackend(
			testClientConfig,
			() => "test-token",
			mockFetch,
			pendingDir,
		);

		const customDraft: BoundedIntakeDraft = {
			archetype: "small_code_change",
			source: {
				text: "Custom blueprint text",
				sha256: sha256Hex("Custom blueprint text"),
				spans: [{ start: 0, end: 6 }],
			},
			goal: { id: "goal", statement: "Custom Goal" },
			acceptance_criteria: [
				{ id: "ac-custom", statement: "Custom criterion", observable_outcome: "Custom outcome" },
			],
		};

		const result = await workBackend.draftMission({
			title: "Custom Goal",
			blueprint: "Custom blueprint text",
			project: "TestProject",
			draft: customDraft,
		});

		expect(result.outcome).toBe("proceeded");
		expect(postedBodies).toHaveLength(1);
		const posted = postedBodies[0] as { payload: { intake: BoundedIntakeDraft } };
		// Verifies that the supplied input.draft was posted verbatim, not synthesized
		expect(posted.payload.intake).toEqual(customDraft);
	});
});
