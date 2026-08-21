/**
 * workflow/transcript.ts — the process-global transcript tag shared by every
 * workflow module copy (the omp loader cache-busts each top-level extension's
 * module graph, so module-local state would split per extension).
 *
 * OMP-47: extracted from the deleted audit-bridge — confirmation receipts are
 * its only remaining consumer; audit authority now lives in WorkService.
 */
import { randomBytes } from "node:crypto";

interface TranscriptStore {
	transcriptRef: string;
}
const GLOBAL_KEY = "__ompWorkTranscriptRef__";
const store = ((globalThis as Record<string, unknown>)[GLOBAL_KEY] as TranscriptStore | undefined) ??
	((globalThis as Record<string, unknown>)[GLOBAL_KEY] = { transcriptRef: "" } satisfies TranscriptStore);

/** Per-process transcript tag. Generated lazily; the workflow host resets it on
 *  session start/switch so receipts never cross transcripts. */
export function currentTranscriptRef(): string {
	if (!store.transcriptRef) store.transcriptRef = `t-${randomBytes(8).toString("hex")}`;
	return store.transcriptRef;
}

export function resetTranscriptRef(): string {
	store.transcriptRef = `t-${randomBytes(8).toString("hex")}`;
	return store.transcriptRef;
}
