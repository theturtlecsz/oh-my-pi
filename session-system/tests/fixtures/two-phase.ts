/**
 * HOME-147: tool writes are transcript-bound two-phase — the first call returns
 * a "CONFIRM REQUIRED" preview carrying a confirmation_id; the write lands only
 * on the repeat call with confirm:true + that id. This helper runs the round
 * trip the way the owner does.
 */
export async function confirmRoundTrip(
	call: (params: Record<string, unknown>) => Promise<string>,
	params: Record<string, unknown>,
): Promise<{ preview: string; confirmed: string; confirmationId: string }> {
	const preview = await call(params);
	const m = /confirmation_id: (\S+)/.exec(preview);
	if (!preview.includes("CONFIRM REQUIRED") || !m) {
		throw new Error(`expected a two-phase preview, got: ${preview}`);
	}
	const confirmed = await call({ ...params, confirm: true, confirmation_id: m[1] });
	return { preview, confirmed, confirmationId: m[1] };
}
