/**
 * workflow/status.ts — bounded status/redaction helpers shared by the workflow
 * host and model-bookends (HOME-147). Everything here is pure: no I/O, no state.
 */

/** Strip bearer-shaped and URL secrets from text headed for notices/status. */
export function redactSecrets(text: string): string {
	return text
		.replace(/lin_api_[A-Za-z0-9]+/g, "lin_api_…")
		.replace(/\bsk-[A-Za-z0-9_-]{8,}/g, "sk-…")
		.replace(/\bAKIA[0-9A-Z]{16}\b/g, "AKIA…")
		.replace(/\bgh[pousr]_[A-Za-z0-9]{8,}/g, "gh…")
		.replace(/\bxox[baprs]-[A-Za-z0-9-]+/g, "xox…")
		.replace(/Bearer\s+\S+/gi, "Bearer …");
}

/** One-line error → recovery hint for /work status. */
export function oneRecovery(error: string): string {
	const e = error.toLowerCase();
	if (e.includes("unauthenticated") || e.includes("401")) return "check the bearer (OMP_WORK_BEARER / bearer_file, 0600)";
	if (e.includes("forbidden") || e.includes("403")) return "the capability lacks the scope — re-issue via omp_work ops capabilities";
	if (e.includes("econnrefused") || e.includes("fetch failed") || e.includes("unavailable")) return "is the loopback service up? python -m omp_work serve";
	if (e.includes("focus_conflict")) return "focus moved — re-read with my_now and retry";
	if (e.includes("stale_evidence") || e.includes("revision_conflict")) return "the revision moved — re-read and restart the attempt";
	if (e.includes("completion_blocked")) return "a /done gate is unmet — the blocker line names it";
	return "see ~/.omp/logs for the full error";
}

/** Cap a status line list; the tail line counts what was dropped. */
export function bounded(lines: string[], max = 12): string[] {
	return lines.length <= max ? lines : [...lines.slice(0, max - 1), `…+${lines.length - (max - 1)} more`];
}

export function healthWord(health: string | undefined): string {
	return health ? (HEALTH_WORDS[health] ?? health) : "?";
}

const HEALTH_WORDS: Record<string, string> = { onTrack: "on track", atRisk: "at risk", offTrack: "off track" };
