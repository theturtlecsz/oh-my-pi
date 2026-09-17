import { describe, expect, test } from "bun:test";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import type { CloseEventView, WorkflowBackend } from "../extensions/workflow/backend";
import {
	deliverCheckpoint,
	deliverPendingCheckpoints,
	queueCheckpointDelivery,
	queueCheckpointDeliverySettlement,
	queuePendingCheckpointDeliveries,
} from "../extensions/workflow/checkpoint-delivery";

function mockEvent(overrides: Partial<CloseEventView> = {}): CloseEventView {
	return {
		eventId: "00000000-0000-7000-8000-000000000001",
		eventType: "close_review_checkpoint",
		reasonCode: "review_checkpoint",
		renderedText: "CLOSE ATTEMPT — close_review_checkpoint\nreview_checkpoint: recorded",
		renderedSha256: "0".repeat(64),
		requiresDelivery: true,
		requiresFreshAuthorization: false,
		...overrides,
	};
}

describe("checkpoint-delivery queued and awaited paths", () => {
	test("queued delivery starts delivery in background without blocking", async () => {
		let deliveryResolved = false;
		const { promise: deliveryPromise, resolve: releaseDelivery } = Promise.withResolvers<void>();
		const { promise: attestationDone, resolve: notifyAttestation } = Promise.withResolvers<void>();

		const mockPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {
				await deliveryPromise;
				deliveryResolved = true;
			},
		} as unknown as ExtensionAPI;

		let attestationCalls = 0;
		const mockBackend: WorkflowBackend = {
			attestDelivery: async (eventId, sessionId, _hash, status) => {
				attestationCalls++;
				expect(eventId).toBe("00000000-0000-7000-8000-000000000001");
				expect(sessionId).toBe("session-1");
				expect(status).toBe("delivered");
				notifyAttestation();
				return {
					status: "applied",
					event: mockEvent(),
				};
			},
			pendingDeliveries: async () => [mockEvent()],
		} as unknown as WorkflowBackend;

		const notices: string[] = [];
		const event = mockEvent();

		// Queue delivery — returns immediately
		queueCheckpointDelivery(mockPi, mockBackend, event, notice => notices.push(notice));

		// At this point, deliverMessage has not resolved yet, so attestDelivery has not been called
		expect(deliveryResolved).toBe(false);
		expect(attestationCalls).toBe(0);

		// Release deliverMessage and await attestation signal
		releaseDelivery();
		await attestationDone;

		expect(deliveryResolved).toBe(true);
		expect(attestationCalls).toBe(1);
		expect(notices).toHaveLength(0);
	});

	test("queuePendingCheckpointDeliveries returns events immediately and queues delivery", async () => {
		const { promise: deliveryPromise, resolve: releaseDelivery } = Promise.withResolvers<void>();
		const { promise: attestationDone, resolve: notifyAttestation } = Promise.withResolvers<void>();

		const mockPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {
				await deliveryPromise;
			},
		} as unknown as ExtensionAPI;

		let attestations = 0;
		const event = mockEvent({ eventId: "00000000-0000-7000-8000-000000000002" });
		const mockBackend: WorkflowBackend = {
			pendingDeliveries: async () => [event],
			attestDelivery: async () => {
				attestations++;
				notifyAttestation();
				return { status: "applied", event };
			},
		} as unknown as WorkflowBackend;

		const queued = await queuePendingCheckpointDeliveries(mockPi, mockBackend, "OMP-1");
		expect(queued.queuedCount).toBe(1);
		expect(queued.events).toEqual([event]);
		expect(attestations).toBe(0);

		releaseDelivery();
		await attestationDone;
		expect(attestations).toBe(1);
	});

	test("failed deliverMessage attests failed status", async () => {
		const mockPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {
				throw new Error("delivery rejected");
			},
		} as unknown as ExtensionAPI;

		let attestedStatus: string | null = null;
		const event = mockEvent({ eventId: "00000000-0000-7000-8000-000000000003" });
		const mockBackend: WorkflowBackend = {
			attestDelivery: async (_id, _session, _hash, status) => {
				attestedStatus = status;
				return { status: "applied", event };
			},
		} as unknown as WorkflowBackend;

		const status = await deliverCheckpoint(mockPi, mockBackend, event);
		expect(status).toBe("failed");
		expect(attestedStatus).toBe("failed");
	});

	test("deliverPendingCheckpoints in awaited context runs synchronously and reports counts", async () => {
		const mockPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {},
		} as unknown as ExtensionAPI;

		const event1 = mockEvent({ eventId: "00000000-0000-7000-8000-000000000004" });
		const event2 = mockEvent({ eventId: "00000000-0000-7000-8000-000000000005" });
		const mockBackend: WorkflowBackend = {
			pendingDeliveries: async () => [event1, event2],
			attestDelivery: async (_id, _session, _hash, _status) => ({ status: "applied", event: event1 }),
		} as unknown as WorkflowBackend;

		const pass = await deliverPendingCheckpoints(mockPi, mockBackend, "OMP-1");
		expect(pass.delivered).toBe(2);
		expect(pass.failed).toBe(0);
		expect(pass.notices).toHaveLength(0);
	});

	test("deliverCheckpoint throws with server-rendered text when attestation is refused", async () => {
		const mockPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {},
		} as unknown as ExtensionAPI;

		const event = mockEvent({
			eventId: "00000000-0000-7000-8000-000000000006",
			reasonCode: "attestation_window_expired",
			renderedText: "CLOSE ATTEMPT — checkpoint_delivery_attested\nattestation_window_expired: window closed",
		});
		const mockBackend: WorkflowBackend = {
			attestDelivery: async () => ({
				status: "refused",
				event,
			}),
		} as unknown as WorkflowBackend;

		await expect(deliverCheckpoint(mockPi, mockBackend, event)).rejects.toThrow("attestation_window_expired: window closed");
	});

	test("deliverCheckpoint ignores idempotent delivery_already_resolved refusal", async () => {
		const mockPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {},
		} as unknown as ExtensionAPI;

		const event = mockEvent({
			eventId: "00000000-0000-7000-8000-000000000007",
			reasonCode: "delivery_already_resolved",
			renderedText: "CLOSE ATTEMPT — checkpoint_delivery_attested\ndelivery_already_resolved: already settled",
		});
		const mockBackend: WorkflowBackend = {
			attestDelivery: async () => ({
				status: "refused",
				event,
			}),
		} as unknown as WorkflowBackend;

		const status = await deliverCheckpoint(mockPi, mockBackend, event);
		expect(status).toBe("delivered");
	});

	test("closeout request refuses before delivery attestation and succeeds after delivery settles", async () => {
		const { promise: deliveryPromise, resolve: releaseDelivery } = Promise.withResolvers<void>();
		const { promise: attestationDone, resolve: notifyAttestation } = Promise.withResolvers<void>();

		const mockPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {
				await deliveryPromise;
			},
		} as unknown as ExtensionAPI;

		let attested = false;
		let closeoutRequested = false;
		const event = mockEvent({ eventId: "00000000-0000-7000-8000-000000000008" });

		const mockBackend: WorkflowBackend = {
			pendingDeliveries: async () => (attested ? [] : [event]),
			attestDelivery: async () => {
				attested = true;
				notifyAttestation();
				return { status: "applied", event };
			},
			proposeClose: async () => {
				if (!attested) {
					throw new Error("delivery_pending: closeout checkpoint must reach owner first");
				}
				closeoutRequested = true;
			},
		} as unknown as WorkflowBackend;

		// 1. Tool handler queues delivery and returns immediately without awaiting
		const queued = await queuePendingCheckpointDeliveries(mockPi, mockBackend, "OMP-1");
		expect(queued.queuedCount).toBe(1);
		expect(attested).toBe(false);

		// 2. Before delivery settles, closeout request is refused
		await expect(mockBackend.proposeClose({ id: "1", key: "OMP-1", title: "T" })).rejects.toThrow("delivery_pending");
		expect(closeoutRequested).toBe(false);

		// 3. Release delivery and wait for attestation
		releaseDelivery();
		await attestationDone;
		expect(attested).toBe(true);

		// 4. After delivery attestation settles, closeout request succeeds
		await mockBackend.proposeClose({ id: "1", key: "OMP-1", title: "T" });
		expect(closeoutRequested).toBe(true);
	});

	test("queueCheckpointDeliverySettlement settles only after injection and attestation, and dedupes to the in-flight delivery", async () => {
		const { promise: injection, resolve: releaseInjection } = Promise.withResolvers<void>();
		const { promise: attestation, resolve: releaseAttestation } = Promise.withResolvers<void>();
		const seam: string[] = [];
		const mockPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async (message: { customType?: string; content?: string }) => {
				seam.push(`deliverMessage:${message.customType}`);
				await injection;
				seam.push("injected");
			},
		} as unknown as ExtensionAPI;
		const event = mockEvent({ eventId: "00000000-0000-7000-8000-000000000009" });
		const mockBackend: WorkflowBackend = {
			attestDelivery: async (eventId, sessionId, renderedSha256, status) => {
				seam.push(`attestDelivery:${eventId}:${sessionId}:${renderedSha256 === event.renderedSha256 ? "digest-ok" : "digest-bad"}:${status}`);
				await attestation;
				seam.push("attested");
				return { status: "applied", event };
			},
		} as unknown as WorkflowBackend;

		let settledFirst: string | undefined;
		let settledSecond: string | undefined;
		const first = queueCheckpointDeliverySettlement(mockPi, mockBackend, event);
		void first.then(status => {
			settledFirst = status;
		});
		// A second queue of the same event while the first is in flight is the
		// SAME settlement: no second deliverMessage, no second attestation.
		const second = queueCheckpointDeliverySettlement(mockPi, mockBackend, event);
		void second.then(status => {
			settledSecond = status;
		});
		expect(second).toBe(first);
		await Promise.resolve();
		expect(seam).toEqual(["deliverMessage:close-attempt-checkpoint"]);

		// Injection alone does not settle it: attestation has not returned.
		releaseInjection();
		await new Promise<void>(resolve => setTimeout(resolve, 0));
		expect(seam).toEqual([
			"deliverMessage:close-attempt-checkpoint",
			"injected",
			"attestDelivery:00000000-0000-7000-8000-000000000009:session-1:digest-ok:delivered",
		]);
		expect(settledFirst).toBeUndefined();
		expect(settledSecond).toBeUndefined();

		releaseAttestation();
		expect(await first).toBe("delivered");
		await new Promise<void>(resolve => setTimeout(resolve, 0));
		expect(seam.at(-1)).toBe("attested");
		expect(settledFirst).toBe("delivered");
		expect(settledSecond).toBe("delivered");
		expect(seam.filter(step => step.startsWith("deliverMessage:"))).toHaveLength(1);
		expect(seam.filter(step => step.startsWith("attestDelivery:"))).toHaveLength(1);

		// Once settled the event is no longer in flight: a later queue is a fresh delivery.
		const third = queueCheckpointDeliverySettlement(mockPi, mockBackend, event);
		expect(third).not.toBe(first);
		expect(await third).toBe("delivered");
		expect(seam.filter(step => step.startsWith("deliverMessage:"))).toHaveLength(2);
		expect(seam.filter(step => step.startsWith("attestDelivery:"))).toHaveLength(2);
	});

	test("queueCheckpointDeliverySettlement never rejects: a failed injection settles as failed after its failed attestation, a refused attestation settles after its notice", async () => {
		const seam: string[] = [];
		const failingPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {
				seam.push("deliverMessage");
				throw new Error("injection rejected");
			},
		} as unknown as ExtensionAPI;
		const failedEvent = mockEvent({ eventId: "00000000-0000-7000-8000-00000000000a" });
		const failedBackend: WorkflowBackend = {
			attestDelivery: async (_id, _session, _hash, status) => {
				seam.push(`attestDelivery:${status}`);
				return { status: "applied", event: failedEvent };
			},
		} as unknown as WorkflowBackend;
		const notices: string[] = [];
		expect(await queueCheckpointDeliverySettlement(failingPi, failedBackend, failedEvent, notice => notices.push(notice))).toBe("failed");
		expect(seam).toEqual(["deliverMessage", "attestDelivery:failed"]);
		expect(notices).toEqual([]);

		const refusedEvent = mockEvent({
			eventId: "00000000-0000-7000-8000-00000000000b",
			reasonCode: "attestation_window_expired",
			renderedText: "CLOSE ATTEMPT — checkpoint_delivery_attested\nattestation_window_expired: window closed",
		});
		const refusingPi: ExtensionAPI = {
			getSessionId: () => "session-1",
			deliverMessage: async () => {},
		} as unknown as ExtensionAPI;
		const refusingBackend: WorkflowBackend = {
			attestDelivery: async () => ({ status: "refused", event: refusedEvent }),
		} as unknown as WorkflowBackend;
		const refusedNotices: string[] = [];
		expect(await queueCheckpointDeliverySettlement(refusingPi, refusingBackend, refusedEvent, notice => refusedNotices.push(notice))).toBe("delivered");
		expect(refusedNotices).toEqual([
			'checkpoint "close_review_checkpoint" attestation refused (attestation_window_expired: CLOSE ATTEMPT — checkpoint_delivery_attested\nattestation_window_expired: window closed)',
		]);

		const throwingBackend: WorkflowBackend = {
			attestDelivery: async () => {
				throw new Error("service unreachable");
			},
		} as unknown as WorkflowBackend;
		const thrownNotices: string[] = [];
		const thrownEvent = mockEvent({ eventId: "00000000-0000-7000-8000-00000000000c" });
		expect(await queueCheckpointDeliverySettlement(refusingPi, throwingBackend, thrownEvent, notice => thrownNotices.push(notice))).toBe("delivered");
		expect(thrownNotices).toEqual([
			'checkpoint "close_review_checkpoint" attestation failed (Error: service unreachable) — it stays pending and closeout stays blocked',
		]);
	});
});
