import { describe, expect, it } from "bun:test";
import {
	CPK5_SCHEMA,
	CpkDisposableProjection,
	CpkFleetManagerProjection,
	CpkNativeOutbox,
	CpkProjectionError,
	type CpkProjectionErrorCode,
	CpkWebUiProjection,
	compareProjectionParity,
	parseCpkNativeFact,
} from "../src/extensibility/cpk/projections";

function captureProjectionError(fn: () => unknown): CpkProjectionError {
	try {
		fn();
	} catch (err) {
		if (err instanceof CpkProjectionError) return err;
		throw err;
	}
	throw new Error("expected CpkProjectionError");
}

function expectCode(fn: () => unknown, code: CpkProjectionErrorCode): void {
	expect(captureProjectionError(fn).code).toBe(code);
}

describe("CPK-5 replayable disposable projections (OMP-207)", () => {
	it("canonicalizes a native fact and rejects invalid shapes with stable error codes", () => {
		const fact = parseCpkNativeFact({
			schema: CPK5_SCHEMA,
			sourceIdentity: "workservice",
			sourceVersion: "1.0.0",
			sequence: 1,
			factType: "task_created",
			timestamp: "2026-09-26T12:00:00Z",
			payload: { taskId: "task-1" },
		});
		expect(fact.schema).toBe(CPK5_SCHEMA);
		expect(fact.sourceIdentity).toBe("workservice");
		expect(fact.sequence).toBe(1);

		expectCode(() => parseCpkNativeFact(null), "not_object");
		expectCode(() => parseCpkNativeFact([]), "not_object");
		expectCode(() => parseCpkNativeFact({ schema: "cpk5/v2" }), "invalid_schema");
		expectCode(
			() =>
				parseCpkNativeFact({
					schema: CPK5_SCHEMA,
					sourceIdentity: "",
					sourceVersion: "1.0.0",
					sequence: 1,
					factType: "t",
					timestamp: "now",
				}),
			"invalid_fact",
		);
		expectCode(
			() =>
				parseCpkNativeFact({
					schema: CPK5_SCHEMA,
					sourceIdentity: "workservice",
					sourceVersion: "1.0.0",
					sequence: 0,
					factType: "t",
					timestamp: "now",
				}),
			"invalid_fact",
		);
	});

	it("commits facts sequentially to the native outbox and exports/imports JSONL faithfully", () => {
		const outbox = new CpkNativeOutbox("workservice", "1.0.0");
		const f1 = outbox.commit("task_created", { taskId: "task-1", title: "Task 1" });
		const f2 = outbox.commit("task_updated", { taskId: "task-1", status: "running" });

		expect(f1.sequence).toBe(1);
		expect(f2.sequence).toBe(2);
		expect(outbox.count).toBe(2);

		const jsonl = outbox.exportJsonl();
		const lines = jsonl.split("\n");
		expect(lines.length).toBe(2);

		const imported = CpkNativeOutbox.importJsonl(jsonl, "workservice", "1.0.0");
		expect(imported.count).toBe(2);
		expect(imported.facts()[0].sequence).toBe(1);
		expect(imported.facts()[1].sequence).toBe(2);
	});

	it("binds every projection row to source identity, version, sequence, and projection version", () => {
		const outbox = new CpkNativeOutbox("test-source", "2.1.0");
		const fact = outbox.commit("item_set", { id: "item-42", value: "hello" }, "2026-09-26T12:00:00Z");

		const proj = new CpkDisposableProjection<{ id: string; value: string }>("items", "0.9.0", (state, f) => {
			const p = f.payload as { id: string; value: string };
			state.set(p.id, {
				id: p.id,
				sourceIdentity: f.sourceIdentity,
				sourceVersion: f.sourceVersion,
				sourceSequence: f.sequence,
				projectionVersion: "0.9.0",
				updatedAt: f.timestamp,
				data: p,
			});
		});

		const res = proj.consume(fact);
		expect(res.accepted).toBe(true);

		const row = proj.getRow("item-42");
		expect(row).toBeDefined();
		expect(row?.sourceIdentity).toBe("test-source");
		expect(row?.sourceVersion).toBe("2.1.0");
		expect(row?.sourceSequence).toBe(1);
		expect(row?.projectionVersion).toBe("0.9.0");
		expect(row?.data.value).toBe("hello");
	});

	it("detects and ignores duplicate delivery without state corruption", () => {
		const outbox = new CpkNativeOutbox();
		const fact = outbox.commit("count", { id: "counter", increment: 1 });

		let total = 0;
		const proj = new CpkDisposableProjection<{ count: number }>("counter", "1.0", (state, f) => {
			const p = f.payload as { id: string; increment: number };
			total += p.increment;
			state.set(p.id, {
				id: p.id,
				sourceIdentity: f.sourceIdentity,
				sourceVersion: f.sourceVersion,
				sourceSequence: f.sequence,
				projectionVersion: "1.0",
				updatedAt: f.timestamp,
				data: { count: total },
			});
		});

		const first = proj.consume(fact);
		expect(first.accepted).toBe(true);
		expect(first.deduplicated).toBe(false);
		expect(total).toBe(1);

		const dup = proj.consume(fact);
		expect(dup.accepted).toBe(false);
		expect(dup.deduplicated).toBe(true);
		expect(total).toBe(1); // Not applied again!
	});

	it("detects gaps in delivery, flags projection status as stale, and refuses un-ordered facts", () => {
		const outbox = new CpkNativeOutbox();
		const f1 = outbox.commit("note", { id: "n1", text: "first" });
		outbox.commit("note", { id: "n2", text: "second" });
		const f3 = outbox.commit("note", { id: "n3", text: "third" });

		const proj = new CpkDisposableProjection<{ text: string }>("notes", "1.0", (state, f) => {
			const p = f.payload as { id: string; text: string };
			state.set(p.id, {
				id: p.id,
				sourceIdentity: f.sourceIdentity,
				sourceVersion: f.sourceVersion,
				sourceSequence: f.sequence,
				projectionVersion: "1.0",
				updatedAt: f.timestamp,
				data: { text: p.text },
			});
		});

		proj.consume(f1);
		expect(proj.isCurrent).toBe(true);

		// Skip f2 and feed f3 (sequence 3 when expected is 2)
		const gapRes = proj.consume(f3);
		expect(gapRes.accepted).toBe(false);
		expect(gapRes.gap).toBe(true);
		expect(proj.isStale).toBe(true);
		expect(proj.status).toBe("stale");
		expect(proj.getRow("n3")).toBeUndefined();
	});

	it("detects schema incompatibility and refuses to corrupt projection state", () => {
		const proj = new CpkDisposableProjection("test", "1.0", (state, f) => {
			state.set("key", {
				id: "key",
				sourceIdentity: f.sourceIdentity,
				sourceVersion: f.sourceVersion,
				sourceSequence: f.sequence,
				projectionVersion: "1.0",
				updatedAt: f.timestamp,
				data: null,
			});
		});

		const badFact = {
			schema: "cpk5/v99" as unknown as typeof CPK5_SCHEMA,
			sourceIdentity: "workservice",
			sourceVersion: "1.0.0",
			sequence: 1,
			factType: "bad",
			timestamp: "now",
			payload: {},
		};

		const res = proj.consume(badFact);
		expect(res.accepted).toBe(false);
		expect(res.schemaIncompatible).toBe(true);
		expect(proj.isStale).toBe(true);
	});

	it("proves derived stores are disposable: disposing and rebuilding produces exact canonical parity", () => {
		const outbox = new CpkNativeOutbox();
		outbox.commit("set", { id: "a", value: 10 });
		outbox.commit("set", { id: "b", value: 20 });
		outbox.commit("set", { id: "c", value: 30 });

		const makeProj = () =>
			new CpkDisposableProjection<{ value: number }>("kv", "1.0", (state, f) => {
				const p = f.payload as { id: string; value: number };
				state.set(p.id, {
					id: p.id,
					sourceIdentity: f.sourceIdentity,
					sourceVersion: f.sourceVersion,
					sourceSequence: f.sequence,
					projectionVersion: "1.0",
					updatedAt: f.timestamp,
					data: { value: p.value },
				});
			});

		const live = makeProj();
		for (const fact of outbox.facts()) {
			live.consume(fact);
		}
		const initialDigest = live.digest();

		// Dispose completely
		live.dispose();
		expect(live.rowCount).toBe(0);
		expect(live.checkpoint.lastSequence).toBe(0);

		// Rebuild strictly from native facts
		live.rebuild(outbox.facts());
		expect(live.rowCount).toBe(3);
		expect(live.digest()).toBe(initialDigest);

		// Recover from checkpoint loss
		const fresh = makeProj();
		fresh.recoverFromCheckpointLoss(outbox.facts());
		expect(compareProjectionParity(live, fresh).match).toBe(true);
	});

	it("guarantees native commits succeed with projections offline, and offline state is never presented as current", () => {
		const outbox = new CpkNativeOutbox();
		const webui = new CpkWebUiProjection("1.0.0");
		const fleet = new CpkFleetManagerProjection("1.0.0");

		// Take projections offline
		webui.setOffline(true);
		fleet.setOffline(true);

		// Native commits proceed without interference
		const f1 = outbox.commit("task_created", { taskId: "task-1", title: "Build feature" });
		const f2 = outbox.commit("worker_registered", { workerId: "worker-1", role: "builder" });

		// Attempted delivery while offline does not mutate projection
		const r1 = webui.consume(f1);
		const r2 = fleet.consume(f2);
		expect(r1.offline).toBe(true);
		expect(r2.offline).toBe(true);

		// Offline banner is explicitly flagged; never presented as current
		expect(webui.isCurrent).toBe(false);
		expect(webui.banner.isCurrent).toBe(false);
		expect(webui.banner.status).toBe("offline");
		expect(webui.banner.message).toContain("offline");

		expect(fleet.isCurrent).toBe(false);
		expect(fleet.banner.isCurrent).toBe(false);
		expect(fleet.banner.status).toBe("offline");

		// When coming back online, replaying from outbox brings projections to current
		webui.setOffline(false);
		fleet.setOffline(false);

		webui.rebuild(outbox.facts());
		fleet.rebuild(outbox.facts());

		expect(webui.isCurrent).toBe(true);
		expect(webui.banner.isCurrent).toBe(true);
		expect(webui.tasks.length).toBe(1);

		expect(fleet.isCurrent).toBe(true);
		expect(fleet.banner.isCurrent).toBe(true);
		expect(fleet.workers.length).toBe(1);
	});

	it("integrates thin WebUI consumer: exposes read-only view and derives task lifecycle correctly", () => {
		const outbox = new CpkNativeOutbox();
		const webui = new CpkWebUiProjection("1.0.0");

		webui.consume(outbox.commit("task_created", { taskId: "task-100", title: "Inspect logs" }));
		webui.consume(outbox.commit("step_completed", { taskId: "task-100", stepIndex: 1 }));
		webui.consume(outbox.commit("step_completed", { taskId: "task-100", stepIndex: 2 }));
		webui.consume(outbox.commit("task_updated", { taskId: "task-100", status: "completed", summary: "Logs clean" }));

		expect(webui.tasks.length).toBe(1);
		const task = webui.tasks[0];
		expect(task.title).toBe("Inspect logs");
		expect(task.status).toBe("completed");
		expect(task.completedSteps).toBe(2);
		expect(task.lastSummary).toBe("Logs clean");
		expect(webui.activeTasksCount).toBe(0);
	});

	it("integrates thin Fleet Manager consumer: exposes read-only view and derives worker assignments correctly", () => {
		const outbox = new CpkNativeOutbox();
		const fleet = new CpkFleetManagerProjection("1.0.0");

		fleet.consume(outbox.commit("worker_registered", { workerId: "worker-A", role: "indexer" }));
		fleet.consume(outbox.commit("worker_registered", { workerId: "worker-B", role: "compiler" }));
		fleet.consume(outbox.commit("task_assigned", { workerId: "worker-A", taskId: "job-1" }));
		fleet.consume(outbox.commit("worker_stopped", { workerId: "worker-B", reason: "drain" }));

		expect(fleet.fleetCapacity).toBe(1); // worker-B stopped, worker-A active
		expect(fleet.busyWorkersCount).toBe(1);

		const workerA = fleet.workers.find(w => w.workerId === "worker-A");
		expect(workerA?.status).toBe("busy");
		expect(workerA?.currentTask).toBe("job-1");

		const workerB = fleet.workers.find(w => w.workerId === "worker-B");
		expect(workerB?.status).toBe("stopped");
	});

	it("derives outbox directly from native session entries and session JSONL without secondary ledgers", () => {
		const rawSessionJsonl = [
			JSON.stringify({ type: "session", id: "sess-1", timestamp: "2026-09-26T10:00:00Z", cwd: "/tmp" }),
			JSON.stringify({
				type: "custom",
				customType: "work-now-execute-outbox",
				data: { action: "dispatch", taskId: "job-99" },
				timestamp: "2026-09-26T10:01:00Z",
			}),
			JSON.stringify({
				type: "message",
				id: "msg-1",
				role: "assistant",
				content: "Started job",
				timestamp: "2026-09-26T10:02:00Z",
			}),
		].join("\n");

		const outbox = CpkNativeOutbox.fromSessionJsonl(rawSessionJsonl, "session-store", "3.0.0");
		expect(outbox.count).toBe(3);
		expect(outbox.sourceIdentity).toBe("session-store");
		expect(outbox.sourceVersion).toBe("3.0.0");

		const facts = outbox.facts();
		expect(facts[0].factType).toBe("session");
		expect(facts[1].factType).toBe("work-now-execute-outbox");
		expect((facts[1].payload as { taskId: string }).taskId).toBe("job-99");
		expect(facts[2].factType).toBe("message");
	});

	it("proves canonical parity fails when row provenance differs despite identical row data", () => {
		const makeProjWithSource = (sourceIdentity: string, sourceVersion: string) => {
			const proj = new CpkDisposableProjection<{ count: number }>("kv", "1.0", (state, f) => {
				const p = f.payload as { id: string; count: number };
				state.set(p.id, {
					id: p.id,
					sourceIdentity: f.sourceIdentity,
					sourceVersion: f.sourceVersion,
					sourceSequence: f.sequence,
					projectionVersion: "1.0",
					updatedAt: f.timestamp,
					data: { count: p.count },
				});
			});
			proj.consume({
				schema: CPK5_SCHEMA,
				sourceIdentity,
				sourceVersion,
				sequence: 1,
				factType: "item",
				timestamp: "2026-09-26T12:00:00Z",
				payload: { id: "row-1", count: 42 },
			});
			return proj;
		};

		const projA = makeProjWithSource("workservice", "1.0.0");
		const projB = makeProjWithSource("git", "2.0.0");

		// Identical row id and payload data, but different sourceIdentity/version
		expect(projA.getRow("row-1")?.data.count).toBe(projB.getRow("row-1")?.data.count);

		const parity = compareProjectionParity(projA, projB);
		expect(parity.match).toBe(false);
		expect(parity.provenanceMatch).toBe(false);
		expect(parity.candidateDigest).not.toBe(parity.baselineDigest);
	});

	it("schema incompatibility sticks and refuses subsequent facts until recoverFromCorruption is called", () => {
		const outbox = new CpkNativeOutbox();
		const f1 = outbox.commit("task_created", { id: "t1" });
		const f2 = {
			schema: "cpk5/v99" as unknown as typeof CPK5_SCHEMA,
			sourceIdentity: "workservice",
			sourceVersion: "1.0.0",
			sequence: 2,
			factType: "bad",
			timestamp: "now",
			payload: {},
		};
		const f3 = outbox.commit("task_created", { id: "t3" });

		const proj = new CpkDisposableProjection<{ id: string }>("tasks", "1.0", (state, f) => {
			const p = f.payload as { id: string };
			state.set(p.id, {
				id: p.id,
				sourceIdentity: f.sourceIdentity,
				sourceVersion: f.sourceVersion,
				sourceSequence: f.sequence,
				projectionVersion: "1.0",
				updatedAt: f.timestamp,
				data: { id: p.id },
			});
		});

		expect(proj.consume(f1).accepted).toBe(true);
		expect(proj.isCurrent).toBe(true);

		// Bad fact corrupts projection
		const badRes = proj.consume(f2);
		expect(badRes.accepted).toBe(false);
		expect(badRes.schemaIncompatible).toBe(true);
		expect(proj.isStale).toBe(true);

		// Subsequent valid fact is REFUSED because schema failure sticks!
		const nextRes = proj.consume(f3);
		expect(nextRes.accepted).toBe(false);
		expect(nextRes.schemaIncompatible).toBe(true);
		expect(proj.isStale).toBe(true);
		expect(proj.getRow("t3")).toBeUndefined();

		// Calling recoverFromCorruption resets corruption and replays cleanly
		proj.recoverFromCorruption([f1, f3]);
		expect(proj.isCurrent).toBe(true);
		expect(proj.rowCount).toBe(2);
		expect(proj.getRow("t3")).toBeDefined();
	});

	it("faithfully reflects rebuilding status in banners without presenting rebuilding state as current", () => {
		const webui = new CpkWebUiProjection("1.0.0");
		const fleet = new CpkFleetManagerProjection("1.0.0");

		expect(webui.banner.status).toBe("current");
		expect(fleet.banner.status).toBe("current");

		const outbox = new CpkNativeOutbox();
		for (let i = 1; i <= 5; i++) {
			outbox.commit("task_created", { taskId: `task-${i}`, title: `Task ${i}` });
		}

		// During rebuild, banner must show status rebuilding and isCurrent false
		let checkedDuringRebuild = false;
		const customProj = new CpkDisposableProjection("test", "1.0", () => {
			if (!checkedDuringRebuild) {
				checkedDuringRebuild = true;
				expect(customProj.status).toBe("rebuilding");
				expect(customProj.isCurrent).toBe(false);
			}
		});

		customProj.rebuild(outbox.facts());
		expect(checkedDuringRebuild).toBe(true);
		expect(customProj.isCurrent).toBe(true);

		webui.rebuild(outbox.facts());
		expect(webui.isCurrent).toBe(true);
		expect(webui.banner.isCurrent).toBe(true);

		fleet.rebuild(outbox.facts());
		expect(fleet.isCurrent).toBe(true);
		expect(fleet.banner.isCurrent).toBe(true);
	});

	it("forbids any mutation on thin WebUI and Fleet Manager projections", () => {
		const webui = new CpkWebUiProjection("1.0.0");
		const fleet = new CpkFleetManagerProjection("1.0.0");

		expectCode(() => webui.createTask({ title: "illegal" }), "mutation_forbidden");
		expectCode(() => webui.updateTask("t1", { status: "running" }), "mutation_forbidden");
		expectCode(() => webui.deleteTask("t1"), "mutation_forbidden");

		expectCode(() => fleet.registerWorker({ role: "illegal" }), "mutation_forbidden");
		expectCode(() => fleet.dispatchWorker("w1", "job-1"), "mutation_forbidden");
		expectCode(() => fleet.stopWorker("w1"), "mutation_forbidden");
	});

	it("restores checkpoints and verifies cursor against native outbox to detect stale cursors", () => {
		const outbox = new CpkNativeOutbox();
		const f1 = outbox.commit("item", { id: "a" });
		const f2 = outbox.commit("item", { id: "b" });

		const proj = new CpkDisposableProjection("items", "1.0", (state, f) => {
			const p = f.payload as { id: string };
			state.set(p.id, {
				id: p.id,
				sourceIdentity: f.sourceIdentity,
				sourceVersion: f.sourceVersion,
				sourceSequence: f.sequence,
				projectionVersion: "1.0",
				updatedAt: f.timestamp,
				data: p,
			});
		});

		proj.consume(f1);
		proj.consume(f2);
		const checkpoint = proj.checkpoint;

		const fresh = new CpkDisposableProjection("items", "1.0", () => {});
		fresh.restoreCheckpoint(checkpoint);
		expect(fresh.checkpoint.lastSequence).toBe(2);

		// Verify cursor against outbox
		const verification = fresh.verifyCursor(outbox);
		expect(verification.valid).toBe(true);
		expect(verification.cursor).toBe(2);

		// Advance outbox without feeding fresh projection -> detect stale cursor
		outbox.commit("item", { id: "c" });
		// If projection is flagged stale or cursor is behind when stale
		fresh.consume({
			schema: "cpk5/v99" as unknown as typeof CPK5_SCHEMA,
			sourceIdentity: "workservice",
			sourceVersion: "1.0.0",
			sequence: 3,
			factType: "bad",
			timestamp: "now",
			payload: {},
		});
		const staleVerif = fresh.verifyCursor(outbox);
		expect(staleVerif.isStale).toBe(true);
		expect(staleVerif.valid).toBe(false);
	});
});
