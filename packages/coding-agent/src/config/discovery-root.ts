import * as path from "node:path";

/**
 * Host-owned configuration root, independent of the directory tools edit.
 * Managed installations set it before startup so candidate configuration cannot
 * change their runtime. This is configuration isolation, not a sandbox.
 */
export function getDiscoveryCwd(cwd: string): string {
	const configured = process.env.OMP_DISCOVERY_CWD;
	if (!configured) return cwd;
	if (!path.isAbsolute(configured)) throw new Error("OMP_DISCOVERY_CWD must be absolute");
	return path.normalize(configured);
}
