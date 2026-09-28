/**
 * Machine-local secret for the relay's CDP client leg (`/cdp`, `/json*`).
 *
 * Created once at `<dir>/cdp-token` (default `~/.omp/browser-relay/cdp-token`):
 * 32 random bytes as hex, mode 0600. The name is published with `link`, so a
 * concurrent caller either creates the file or reads the finished one.
 */
import { randomBytes } from "node:crypto";
import { chmod, link, mkdir, open, readFile, rm } from "node:fs/promises";
import * as path from "node:path";
import { getBrowserRelayDir } from "@oh-my-pi/pi-utils";

const CDP_TOKEN_FILE = "cdp-token";

function isEexist(err: unknown): boolean {
	return err instanceof Error && "code" in err && err.code === "EEXIST";
}

/** Return the relay CDP token, creating `<dir>/cdp-token` on the first call. */
export async function loadRelayCdpToken(dir = getBrowserRelayDir()): Promise<string> {
	await mkdir(dir, { recursive: true });
	const file = path.join(dir, CDP_TOKEN_FILE);
	const created = randomBytes(32).toString("hex");
	const tmp = path.join(dir, `.${CDP_TOKEN_FILE}.${process.pid}.${randomBytes(4).toString("hex")}`);
	const handle = await open(tmp, "wx", 0o600);
	try {
		await handle.writeFile(created);
		await chmod(tmp, 0o600);
	} catch (err) {
		await rm(tmp, { force: true }).catch(() => undefined);
		throw err;
	} finally {
		await handle.close();
	}
	try {
		await link(tmp, file);
	} catch (err) {
		if (!isEexist(err)) throw err;
		const token = (await readFile(file, "utf8")).trim();
		if (!token) throw new Error(`browser relay CDP token at ${file} is empty`);
		return token;
	} finally {
		await rm(tmp, { force: true });
	}
	return created;
}
