/**
 * workflow/agent-stop.ts — hold this session while the workspace stop is engaged (OMP-405).
 *
 * Until the first poll settles, state is released. A stopped poll aborts a busy
 * turn every time; the warning fires only on the released → stopped edge.
 * Turns that begin outside `input` (host resume, `.` / `c`, RPC `/`) are halted
 * by the next busy stopped poll, not at turn start. A failed poll keeps the
 * last settled state.
 */
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import type { WorkClient } from "@oh-my-pi/pi-work-client";

const DEFAULT_POLL_MS = 5000;
const RELEASED_NOTICE = "Agent stop released.";

function engagedNotice(stopReason: string | null): string {
	const detail = stopReason ? `Agent stop engaged: ${stopReason}` : "Agent stop engaged";
	return `${detail}. Only the owner can release it via omp-work stop release.`;
}

export function installAgentStopGate(
	pi: ExtensionAPI,
	client: Pick<WorkClient, "stopStatus">,
	options?: { pollMs?: number },
): void {
	const pollMs = options?.pollMs ?? DEFAULT_POLL_MS;
	let sessionCtx: ExtensionContext | null = null;
	let stopped = false;
	let reason: string | null = null;
	let flight: Promise<void> | null = null;
	let timer: ReturnType<ExtensionContext["setInterval"]> | null = null;

	function apply(view: { stopped: boolean; reason: string | null }, ctx: ExtensionContext): void {
		if (view.stopped) {
			const entered = !stopped;
			stopped = true;
			reason = view.reason ?? null;
			if (!ctx.isIdle()) ctx.abort();
			if (entered) ctx.ui.notify(engagedNotice(reason), "warning");
			return;
		}
		const left = stopped;
		stopped = false;
		reason = null;
		if (left) ctx.ui.notify(RELEASED_NOTICE, "info");
	}

	function tick(): Promise<void> {
		if (flight) return flight;
		let resolveRun!: () => void;
		const run = new Promise<void>(resolve => {
			resolveRun = resolve;
		});
		// Assign before stopStatus so a synchronous throw still joins this flight
		// and a second tick cannot start another request.
		flight = run;
		void (async () => {
			try {
				const view = await client.stopStatus();
				const ctx = sessionCtx;
				if (ctx) apply(view, ctx);
			} catch {
				// Last settled state stands. tick itself must not reject.
			} finally {
				if (flight === run) flight = null;
				resolveRun();
			}
		})();
		return run;
	}

	pi.on("session_start", (_event, ctx) => {
		sessionCtx = ctx;
		if (timer !== null) {
			ctx.clearTimer(timer);
			timer = null;
		}
		void tick();
		timer = ctx.setInterval(() => tick(), pollMs);
	});

	pi.on("input", (_event, ctx) => {
		if (!stopped) return undefined;
		ctx.ui.notify(engagedNotice(reason), "warning");
		return { handled: true };
	});

	pi.on("tool_call", () => {
		if (!stopped) return undefined;
		return { block: true, reason: engagedNotice(reason) };
	});
}
