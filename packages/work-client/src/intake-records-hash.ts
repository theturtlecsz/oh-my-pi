/** Hash one intake command result the way store.py records result_sha256:
 *  sha256 of the result alone, not the envelope. Canonical JSON is the
 *  index.ts body with number text changed to match
 *  json.dumps(json.loads(JSON.stringify(n))). Strings, including created_at,
 *  stay verbatim. */
function canonicalJson(value: unknown): string {
	if (value === null || typeof value !== "object") {
		if (typeof value === "number") return canonicalNumber(value);
		return JSON.stringify(value) ?? "null";
	}
	if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
	const entries = Object.entries(value as Record<string, unknown>)
		.filter(([, v]) => v !== undefined)
		.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
		.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`);
	return `{${entries.join(",")}}`;
}

/** Digit-only JSON text (optional minus) wins over the exponent thresholds.
 *  Otherwise 0 < abs(n) < 1e-4 or abs(n) >= 1e16 uses toExponential(),
 *  preserving sign and padding the exponent to at least two digits. */
function canonicalNumber(value: number): string {
	const text = JSON.stringify(value) ?? "null";
	if (/^-?\d+$/.test(text)) return text;
	const magnitude = Math.abs(value);
	if ((magnitude > 0 && magnitude < 1e-4) || magnitude >= 1e16) {
		return value.toExponential().replace(/e([+-])(\d+)$/, (_match, sign: string, exponent: string) => {
			return `e${sign}${exponent.padStart(2, "0")}`;
		});
	}
	return text;
}

/** Normalize received_at as text without Date:
 *  UTC +00:00/Z -> Z; preserve other offsets (e.g. +05:30);
 *  drop zero or missing fraction; else pad/truncate to 6 digits, no rounding. */
function normalizeReceivedAt(text: string): string {
	const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:\d{2}|Z)$/.exec(text);
	if (!match) return text;
	const prefix = match[1];
	const rawFraction = match[2];
	const zone = match[3];

	let fractionPart = "";
	if (rawFraction !== undefined) {
		const frac6 = rawFraction.padEnd(6, "0").slice(0, 6);
		if (frac6 !== "000000") {
			fractionPart = `.${frac6}`;
		}
	}

	const normZone = zone === "+00:00" || zone === "-00:00" || zone === "Z" ? "Z" : zone;
	return `${prefix}${fractionPart}${normZone}`;
}

/** sha256(canonical JSON of result) — UTF-8 bytes, lowercase hex. */
export function intakeResultSha256(result: unknown): string {
	return new Bun.CryptoHasher("sha256").update(canonicalJson(result), "utf8").digest("hex");
}

/** sha256(canonical JSON of { api_version, workspace_id, command }) — UTF-8 bytes, lowercase hex.
 *  Excludes operation_id/request_id/correlation_id. Applies actual-path defaults without mutating input. */
export function intakeCommandSha256(envelope: {
	api_version?: unknown;
	workspace_id?: unknown;
	command?: unknown;
}): string {
	const copiedCommand =
		envelope.command !== null && typeof envelope.command === "object"
			? (structuredClone(envelope.command) as Record<string, unknown>)
			: (envelope.command as Record<string, unknown>);

	if (copiedCommand !== null && typeof copiedCommand === "object") {
		const payload = copiedCommand.payload;
		if (payload !== null && typeof payload === "object") {
			const pl = payload as Record<string, unknown>;

			if (pl.claim !== null && typeof pl.claim === "object") {
				const claim = pl.claim as Record<string, unknown>;
				if (!("admitted_by" in claim) || claim.admitted_by === undefined) {
					claim.admitted_by = null;
				}
				if (Array.isArray(claim.evidence)) {
					for (const ev of claim.evidence) {
						if (ev !== null && typeof ev === "object") {
							const evObj = ev as Record<string, unknown>;
							if (!("quote" in evObj) || evObj.quote === undefined) {
								evObj.quote = null;
							}
						}
					}
				}
			}

			if (Array.isArray(pl.evidence_links)) {
				for (const link of pl.evidence_links) {
					if (link !== null && typeof link === "object") {
						const linkObj = link as Record<string, unknown>;
						if (linkObj.span !== null && typeof linkObj.span === "object") {
							const spanObj = linkObj.span as Record<string, unknown>;
							if (!("quote" in spanObj) || spanObj.quote === undefined) {
								spanObj.quote = null;
							}
						}
					}
				}
			}

			if (copiedCommand.type === "record_intake_decision" || ("decision" in pl && pl.decision !== null)) {
				if (!("superseded_decision_id" in pl) || pl.superseded_decision_id === undefined) {
					pl.superseded_decision_id = null;
				}
				if (!("verified_relay_operation_id" in pl) || pl.verified_relay_operation_id === undefined) {
					pl.verified_relay_operation_id = null;
				}
			}

			if (pl.source !== null && typeof pl.source === "object") {
				const src = pl.source as Record<string, unknown>;
				if (!("spans" in src) || src.spans === undefined) {
					src.spans = [];
				}
			}

			if (pl.instruction !== null && typeof pl.instruction === "object") {
				const inst = pl.instruction as Record<string, unknown>;
				if (inst.provenance !== null && typeof inst.provenance === "object") {
					const prov = inst.provenance as Record<string, unknown>;
					if (typeof prov.received_at === "string") {
						prov.received_at = normalizeReceivedAt(prov.received_at);
					}
				}
			}
		}
	}

	const target = {
		api_version: envelope.api_version,
		workspace_id: String(envelope.workspace_id),
		command: copiedCommand,
	};
	return new Bun.CryptoHasher("sha256").update(canonicalJson(target), "utf8").digest("hex");
}
