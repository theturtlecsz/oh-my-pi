import { $env } from "@oh-my-pi/pi-utils/env";

export const MAX_STAGE_CONTEXT_BYTES = 64 * 1024;

/** Compile the existing session-system digest only for opted-in worker launches. */
export async function compileStageContext(
	compile: (() => Promise<string>) | undefined,
	enabled: boolean = ($env.OMP_STAGE_CONTEXT_PUSH ?? $env.PI_STAGE_CONTEXT_PUSH) === "1",
): Promise<string | undefined> {
	if (!enabled) return undefined;
	if (!compile) throw new Error("Stage context compiler is required when stage-context push is enabled");
	const text = (await compile()).trim();
	if (!text) throw new Error("Compiled stage context is empty");
	if (Buffer.byteLength(text) > MAX_STAGE_CONTEXT_BYTES) {
		throw new Error(`Compiled stage context exceeds ${MAX_STAGE_CONTEXT_BYTES} bytes`);
	}
	return text;
}
