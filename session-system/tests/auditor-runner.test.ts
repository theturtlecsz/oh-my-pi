import { describe, expect, test } from "bun:test";
import * as path from "node:path";
import type { ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { prepareNativeAuditRunner } from "../extensions/workflow/auditor-runner";
import type { CloseAttemptSnapshot } from "../extensions/workflow/backend";
import {
	confirmWrite,
	RECEIPT_TTL_MS,
	resetConfirmations,
} from "../extensions/workflow/confirm";
import {
	renderNextActionBanner,
	renderSummaryResumeDigest,
} from "../extensions/workflow/host";

describe("native auditor runner (OMP-168)", () => {
	test("prepareNativeAuditRunner fails if @audit role cannot be resolved", async () => {
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: { resolve: () => undefined },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		await expect(prepareNativeAuditRunner(fakeCtx)).rejects.toThrow("@audit");
	});

	test("prepareNativeAuditRunner returns a runner when preconditions exist", async () => {
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: { resolve: (role: string) => (role === "@audit" ? { id: "gpt-5.2", provider: "openai" } : undefined) },
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeAuditRunner(fakeCtx);
		expect(typeof runner).toBe("function");
	});

	test("runner returns started:false when cancelled before start", async () => {
		const repoRoot = path.resolve(import.meta.dir, "../..");
		const fakeCtx = {
			cwd: repoRoot,
			models: { resolve: (role: string) => (role === "@audit" ? { id: "gpt-5.2", provider: "openai" } : undefined) },
			modelRegistry: { getApiKey: () => Promise.resolve("key") },
			taskDepth: 0,
		} as unknown as ExtensionContext;
		const runner = await prepareNativeAuditRunner(fakeCtx);

		const abortController = new AbortController();
		abortController.abort(); // already aborted

		const result = await runner("test task", "attempt-1", abortController.signal);
		expect(result.started).toBe(false);
		expect(result.payload).toBeUndefined();
	});
});

describe("renderNextActionBanner table-driven coverage (OMP-168)", () => {
	const snapshot = (state: string): CloseAttemptSnapshot => ({
		attemptId: "att-1",
		state,
		remainingLaunches: 3,
		remainingReports: 2,
		hasManifest: true,
		isLaunchable: state === "audit_ready",
		nextAction: "",
	});

	test("active state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("active"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT active",
			'NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"verification"',
			'BLOCKED ACTIONS: run_audit, append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT active");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audit_ready state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audit_ready"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audit_ready",
			'NEXT REQUIRED ACTION: work action:"run_audit", work:"HOME-1"',
			'BLOCKED ACTIONS: append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audit_ready");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("auditor_in_flight state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("auditor_in_flight"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT auditor_in_flight",
			"NEXT REQUIRED ACTION: wait for the current native run to settle and use get_work only for recovery",
			"BLOCKED ACTIONS: run_audit, append_evidence, /done",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT auditor_in_flight");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audited state banner (authorized)", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audited"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audited",
			'NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"closeout"',
			"BLOCKED ACTIONS: run_audit, /done",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audited");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("audited state banner (unauthorized)", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("audited"), false);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT audited",
			"NEXT REQUIRED ACTION: owner /summary must be entered in this session to authorize closeout review",
			'BLOCKED ACTIONS: append_evidence kind:"closeout", /done',
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT audited");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("closeout_requested state banner", () => {
		const lines = renderNextActionBanner("HOME-1", snapshot("closeout_requested"), true);
		expect(lines).toEqual([
			"STATUS: CLOSE ATTEMPT closeout_requested",
			"NEXT REQUIRED ACTION: owner /done closes this work",
			"BLOCKED ACTIONS: run_audit, append_evidence",
		]);
		expect(lines[0]).toBe("STATUS: CLOSE ATTEMPT closeout_requested");
		expect(lines.filter(l => l.startsWith("NEXT REQUIRED ACTION:"))).toHaveLength(1);
	});

	test("terminal or missing snapshot returns empty array", () => {
		expect(renderNextActionBanner("HOME-1", undefined, true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("completed"), true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("superseded"), true)).toEqual([]);
		expect(renderNextActionBanner("HOME-1", snapshot("budget_exhausted"), true)).toEqual([]);
	});

	test("renderSummaryResumeDigest contains banner and 5 compact review sections", () => {
		const digest = renderSummaryResumeDigest("HOME-1", snapshot("audited"));
		expect(digest).toContain("STATUS: CLOSE ATTEMPT audited");
		expect(digest).toContain('NEXT REQUIRED ACTION: work action:"append_evidence", work:"HOME-1", kind:"closeout"');
		expect(digest).toContain("Satisfied steps must NOT be repeated");
		expect(digest).toContain('Call `work action:"get_work", work:"HOME-1"`');
		expect(digest).toContain('1. Verbatim `work action:"my_now"` completion tree');
		expect(digest).toContain("2. MOVED");
		expect(digest).toContain("3. PROOF");
		expect(digest).toContain("4. UNVERIFIED / BLOCKED");
		expect(digest).toContain("5. NEXT SESSION");
	});
});

describe("confirmation lifecycle (OMP-168)", () => {
	test("same-transcript identical payload approves once; consumed receipt refuses retry", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Model wants to create an issue";
		const detail = "title: test";
		const params = { title: "test" };

		const first = confirmWrite(action, question, detail, params);
		expect(first.approved).toBe(false);
		if (first.approved) throw new Error("expected unapproved preview");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.preview);
		expect(match).not.toBeNull();
		const confirmationId = match![1];

		// Second call with confirm:true and confirmation_id approves
		const second = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: confirmationId,
		});
		expect(second.approved).toBe(true);

		// Third call with already consumed id is refused
		const third = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: confirmationId,
		});
		expect(third.approved).toBe(false);
		if (!third.approved) {
			expect(third.preview).toContain("already consumed");
		}
	});

	test("59-minute receipt remains usable", () => {
		resetConfirmations({ resetShared: true });
		const action = "revise_work";
		const question = "Model wants to revise";
		const detail = "new title";
		const params = { title: "revised" };

		const first = confirmWrite(action, question, detail, params);
		expect(first.approved).toBe(false);
		if (first.approved) throw new Error("expected unapproved preview");

		const match = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.preview);
		const confirmationId = match![1];

		// Advance time by 59 minutes
		const originalNow = Date.now;
		try {
			Date.now = () => originalNow() + 59 * 60_000;
			const outcome = confirmWrite(action, question, detail, {
				...params,
				confirm: true,
				confirmation_id: confirmationId,
			});
			expect(outcome.approved).toBe(true);
		} finally {
			Date.now = originalNow;
		}
	});

	test(">60-minute expired receipt returns a fresh preview and new ID without writing", () => {
		resetConfirmations({ resetShared: true });
		const action = "set_now";
		const question = "Model wants to set now";
		const detail = "HOME-1";
		const params = { work: "HOME-1" };

		const first = confirmWrite(action, question, detail, params);
		const oldId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		const originalNow = Date.now;
		try {
			Date.now = () => originalNow() + 61 * 60_000;
			const retry = confirmWrite(action, question, detail, {
				...params,
				confirm: true,
				confirmation_id: oldId,
			});
			expect(retry.approved).toBe(false);
			if (!retry.approved) {
				expect(retry.preview).toContain("CONFIRM REQUIRED");
				const newId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(retry.preview)![1];
				expect(newId).not.toBe(oldId);
			}
		} finally {
			Date.now = originalNow;
		}
	});

	test("foreign transcript receipt returns a fresh preview and new ID without writing", () => {
		resetConfirmations({ resetShared: true });
		const action = "queue_work";
		const question = "Model wants to queue";
		const detail = "HOME-2";
		const params = { work: "HOME-2", question: "Is this done?" };

		const first = confirmWrite(action, question, detail, params);
		const oldId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Switch session / rotate transcript without clearing unconsumed receipts
		resetConfirmations({ resetShared: true });

		const retry = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: oldId,
		});
		expect(retry.approved).toBe(false);
		if (!retry.approved) {
			expect(retry.preview).toContain("CONFIRM REQUIRED");
			const newId = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(retry.preview)![1];
			expect(newId).not.toBe(oldId);
		}
	});

	test("changed payload and unknown receipt stay refused", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Model wants to create";
		const detail = "title A";
		const params = { title: "title A" };

		const first = confirmWrite(action, question, detail, params);
		const id = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Changed payload
		const changed = confirmWrite(action, question, "title B", {
			title: "title B",
			confirm: true,
			confirmation_id: id,
		});
		expect(changed.approved).toBe(false);
		if (!changed.approved) {
			expect(changed.preview).toContain("payload changed");
		}

		// Unknown ID
		const unknown = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: "cf-unknown0000",
		});
		expect(unknown.approved).toBe(false);
		if (!unknown.approved) {
			expect(unknown.preview).toContain("unknown or already-used");
		}
	});

	test("subagent reset (resetShared:false) never invalidates owner receipts", () => {
		resetConfirmations({ resetShared: true });
		const action = "create_work";
		const question = "Owner create";
		const detail = "owner details";
		const params = { title: "owner task" };

		const first = confirmWrite(action, question, detail, params, { isSubagent: false });
		const id = /confirmation_id:\s*(cf-[a-f0-9]+)/.exec(first.approved ? "" : first.preview)![1];

		// Subagent session resets local confirmations
		resetConfirmations({ resetShared: false });

		// Owner confirmation call still approves
		const confirmCall = confirmWrite(action, question, detail, {
			...params,
			confirm: true,
			confirmation_id: id,
		});
		expect(confirmCall.approved).toBe(true);
	});
});
