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

/** sha256(canonical JSON of result) — UTF-8 bytes, lowercase hex. */
export function intakeResultSha256(result: unknown): string {
	return new Bun.CryptoHasher("sha256").update(canonicalJson(result), "utf8").digest("hex");
}
