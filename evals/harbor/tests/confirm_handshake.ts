/**
 * confirm_handshake.ts — drive the workflow confirmation handshake from stdin so
 * a Python test can prove a fixture's scripted confirm call is accepted by the
 * real ``confirmWrite`` gate (which mints random single-use ids).
 *
 * Reads JSON lines on stdin and writes JSON lines on stdout:
 *
 *   {"phase":"preview","action","question","detail","params","options"}
 *     → {"phase":"preview","approved":false,"preview":"…confirmation_id: cf-…"}
 *   {"phase":"confirm","confirmation_id","params"?}
 *     → {"phase":"confirm","approved":true}
 *
 * The process holds the pending receipt between the two phases, so the id the
 * preview minted is the id the confirm must carry. An optional ``params`` on
 * the confirm replaces the previewed payload, letting a test run a fully
 * resolved scripted call through the gate.
 */
import {
	confirmWrite,
	resetConfirmations,
	type ConfirmGateParams,
} from "../../../session-system/extensions/workflow/confirm";

interface GateOptions {
	expectedRevisionId?: string;
	currentRevisionId?: string;
	isSubagent?: boolean;
}

interface PreviewRequest {
	phase: "preview";
	action: string;
	question: string;
	detail: string;
	params: ConfirmGateParams;
	options: GateOptions;
}

interface ConfirmRequest {
	phase: "confirm";
	confirmation_id: string;
	params?: ConfirmGateParams;
}

function emit(value: unknown): void {
	process.stdout.write(JSON.stringify(value) + "\n");
}

resetConfirmations({ resetShared: true });
let plan: PreviewRequest | null = null;

for await (const line of console) {
	const stripped = line.trim();
	if (!stripped) continue;
	const message = JSON.parse(stripped) as PreviewRequest | ConfirmRequest;
	if (message.phase === "preview") {
		plan = message;
		emit({
			phase: "preview",
			...confirmWrite(plan.action, plan.question, plan.detail, plan.params, plan.options),
		});
		continue;
	}
	if (message.phase === "confirm") {
		if (!plan) throw new Error("confirm requested before preview");
		emit({
			phase: "confirm",
			...confirmWrite(
				plan.action,
				plan.question,
				plan.detail,
				{ ...(message.params ?? plan.params), confirm: true, confirmation_id: message.confirmation_id },
				plan.options,
			),
		});
		break;
	}
}
