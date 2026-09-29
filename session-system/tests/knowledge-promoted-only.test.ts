import { describe, expect, test } from "bun:test";
import {
	procedureDigestLines,
	type ProcedureInput,
	type ProcedureRunner,
} from "../extensions/workflow/procedures";

describe("knowledge-promoted-only", () => {
	const defaultInput: ProcedureInput = {
		workKey: "OMP-400",
		projectId: "proj-1",
		cwd: "/repo/test",
	};

	test("procedureDigestLines splices supply after module and preserves --promoted-only in argv", async () => {
		let capturedCommand: string[] = [];
		const fakeRunner: ProcedureRunner = (cmd) => {
			capturedCommand = cmd;
			return {
				exitCode: 0,
				stdout: JSON.stringify([
					"PROCEDURE proc-1@v1 supply=sup-1: Promoted procedure - step 1; step 2",
				]),
			};
		};

		const baseCmd = [
			"python",
			"-m",
			"omp_knowledge.learning",
			"--state-dir",
			"/tmp/learning-state",
			"--promoted-only",
		];

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(baseCmd) },
			run: fakeRunner,
		});

		expect(lines).toEqual([
			"PROCEDURE proc-1@v1 supply=sup-1: Promoted procedure - step 1; step 2",
		]);

		const moduleIndex = capturedCommand.indexOf("omp_knowledge.learning");
		expect(moduleIndex).toBeGreaterThanOrEqual(0);
		expect(capturedCommand[moduleIndex + 1]).toBe("supply");
		expect(capturedCommand).toContain("--promoted-only");
		expect(capturedCommand).toEqual([
			"python",
			"-m",
			"omp_knowledge.learning",
			"supply",
			"--work-key",
			"OMP-400",
			"--project-id",
			"proj-1",
			"--cwd",
			"/repo/test",
			"--json",
			"--state-dir",
			"/tmp/learning-state",
			"--promoted-only",
		]);
	});

	test("procedureDigestLines handles empty array output when no procedures are promoted", async () => {
		let capturedCommand: string[] = [];
		const fakeRunner: ProcedureRunner = (cmd) => {
			capturedCommand = cmd;
			return {
				exitCode: 0,
				stdout: "[]",
			};
		};

		const baseCmd = [
			"python",
			"-m",
			"omp_knowledge.learning",
			"--state-dir",
			"/tmp/learning-state",
			"--workspace",
			"ws-promoted",
			"--promoted-only",
		];

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(baseCmd) },
			run: fakeRunner,
		});

		expect(lines).toEqual([]);
		expect(capturedCommand).toContain("supply");
		expect(capturedCommand).toContain("--promoted-only");
	});
});
