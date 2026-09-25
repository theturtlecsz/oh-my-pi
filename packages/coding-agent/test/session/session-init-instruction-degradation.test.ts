import { afterEach, describe, expect, it } from "bun:test";
import { SessionManager } from "@oh-my-pi/pi-coding-agent/session/session-manager";
import { TempDir } from "@oh-my-pi/pi-utils";

const tempDirs: TempDir[] = [];
const managers: SessionManager[] = [];

function makeTempDir(prefix: string): TempDir {
	const dir = TempDir.createSync(prefix);
	tempDirs.push(dir);
	return dir;
}

afterEach(async () => {
	await Promise.all(managers.splice(0).map(manager => manager.close()));
	await Promise.all(tempDirs.splice(0).map(dir => dir.remove()));
});

describe("SessionManager session_init instructionPrepDegradations", () => {
	it("persists and reads back non-empty instruction prep degradations", async () => {
		const tempDir = makeTempDir("@pi-session-init-degradation-");
		const sm = SessionManager.create(tempDir.path(), tempDir.join("sessions"));
		managers.push(sm);

		sm.appendSessionInit({
			systemPrompt: "s",
			task: "t",
			tools: ["read"],
			instructionPrepDegradations: [{ source: "loadSystemPromptFiles", cause: "timeout" }],
		});
		await sm.ensureOnDisk();
		await sm.flush();

		const peek = await SessionManager.peekSessionInit(sm.getSessionFile()!);
		expect(peek?.init?.instructionPrepDegradations).toEqual([{ source: "loadSystemPromptFiles", cause: "timeout" }]);
	});

	it("omits the key when instruction prep degradations are empty", async () => {
		const tempDir = makeTempDir("@pi-session-init-degradation-empty-");
		const sm = SessionManager.create(tempDir.path(), tempDir.join("sessions"));
		managers.push(sm);

		sm.appendSessionInit({
			systemPrompt: "s",
			task: "t",
			tools: ["read"],
			instructionPrepDegradations: [],
		});
		await sm.ensureOnDisk();
		await sm.flush();

		const peek = await SessionManager.peekSessionInit(sm.getSessionFile()!);
		expect(peek?.init).not.toBeNull();
		expect("instructionPrepDegradations" in peek!.init!).toBe(false);
	});
});
