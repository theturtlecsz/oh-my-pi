export function grantRemoteRef(execution: unknown): string {
	let target = execution;
	if (typeof target === "string") {
		try {
			target = JSON.parse(target);
		} catch {
			// keep target as string; validation below will throw
		}
	}
	if (typeof target !== "object" || target === null) {
		throw new Error("execution is missing or invalid");
	}
	const grant = (target as { grant?: unknown }).grant;
	if (typeof grant !== "object" || grant === null) {
		throw new Error("execution.grant is missing");
	}
	const remoteRef = (grant as { remote_ref?: unknown }).remote_ref;
	if (typeof remoteRef !== "string" || !remoteRef.startsWith("refs/heads/")) {
		throw new Error(`grant.remote_ref must be a string starting with refs/heads/, got: ${String(remoteRef)}`);
	}
	return remoteRef;
}
