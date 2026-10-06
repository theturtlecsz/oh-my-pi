/**
 * Stage-context delivery (OMP defect 1).
 *
 * Agents almost never call optional memory tools, so the compiled stage context
 * (the Work Ledger digest and stage inputs) is pushed into the first provider
 * request at launch instead. The launcher (flood) already compiles that digest
 * into a file; this module only reads it. No new store.
 *
 * Default off: the feature is active only when `PI_STAGE_CONTEXT_FILE` (alias
 * `OMP_STAGE_CONTEXT_FILE`) is set.
 */
import { $env } from "@oh-my-pi/pi-utils/env";

/** Upper bound on the injected block; a larger digest is a launcher bug and fails loud. */
export const MAX_STAGE_CONTEXT_BYTES = 64 * 1024;

/**
 * Reads the compiled stage context for this worker, or `undefined` when the flag is off.
 * Throws when the flag is on but the file is missing, empty, or oversized: a worker
 * that was promised context must not start without it.
 */
export async function loadStageContext(
	path: string | undefined = $env.PI_STAGE_CONTEXT_FILE,
): Promise<string | undefined> {
	if (!path) return undefined;
	const text = (await Bun.file(path).text()).trim();
	if (!text) throw new Error(`Stage context file is empty: ${path}`);
	if (Buffer.byteLength(text) > MAX_STAGE_CONTEXT_BYTES) {
		throw new Error(`Stage context file exceeds ${MAX_STAGE_CONTEXT_BYTES} bytes: ${path}`);
	}
	return text;
}
