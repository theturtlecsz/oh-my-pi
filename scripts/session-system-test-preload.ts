/**
 * Root `bun test` preload: give every test process a private temp root.
 *
 * Prompt fixtures isolate cwd and agentDir but not os.tmpdir(), so an
 * AgentSession's plan lookup falls back to the shared `<os.tmpdir()>/omp-local`
 * root. When that shared root is unwritable — a mode-000 leftover from an
 * earlier run, or a hostile TMPDIR — session-system tests fail on EACCES
 * instead of exercising their own fixtures.
 *
 * Allocating one mkdtemp root under the inherited temp dir and pointing
 * TMPDIR/TMP/TEMP at it for the process lifetime keeps every run on a private
 * root, so no process ever shares `<os.tmpdir()>/omp-local`. The root is
 * removed synchronously on both lifecycle paths that can end the run: an
 * explicit `process.exit()` fires the `exit` handler, while a normal
 * completion does not (verified against Bun 1.4.2) and is covered by bun:test's
 * `afterAll` teardown instead.
 *
 * `TempDir` is imported from `@oh-my-pi/pi-utils/temp` rather than the package
 * barrel so the preload loads before the test config without pulling in a
 * native-backed module. A failed allocation throws: the run must not start on
 * an unknown temp root.
 */
import * as os from "node:os";
import * as path from "node:path";
import { afterAll } from "bun:test";
import { TempDir } from "@oh-my-pi/pi-utils/temp";

const root = TempDir.createSync(path.join(os.tmpdir(), "omp-session-test-"));
process.env.TMPDIR = root.path();
process.env.TMP = root.path();
process.env.TEMP = root.path();
process.on("exit", () => root.removeSync());
afterAll(() => root.removeSync());
