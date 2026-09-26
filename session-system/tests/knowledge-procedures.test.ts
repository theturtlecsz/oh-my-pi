import { describe, expect, test } from "bun:test";
import {
	procedureDigestLines,
	type ProcedureInput,
	type ProcedureRunner,
} from "../extensions/workflow/procedures";

describe("procedureDigestLines", () => {
	const defaultInput: ProcedureInput = {
		workKey: "OMP-100",
		projectId: "proj-1",
		cwd: "/repo/test",
	};

	test("returns empty array when OMP_KNOWLEDGE_LEARNING_CMD is unset", async () => {
		let runnerCalled = false;
		const fakeRunner: ProcedureRunner = () => {
			runnerCalled = true;
			return { exitCode: 0, stdout: "[]" };
		};

		const lines = await procedureDigestLines(defaultInput, {
			env: {},
			run: fakeRunner,
		});

		expect(lines).toEqual([]);
		expect(runnerCalled).toBe(false);
	});

	test("returns empty array when OMP_KNOWLEDGE_LEARNING_CMD is empty string", async () => {
		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: "" },
			run: () => ({ exitCode: 0, stdout: "[]" }),
		});

		expect(lines).toEqual([]);
	});

	test("parses JSON output from successful runner into PROCEDURE lines", async () => {
		let capturedCommand: string[] = [];
		const fakeRunner: ProcedureRunner = (cmd) => {
			capturedCommand = cmd;
			return {
				exitCode: 0,
				stdout: JSON.stringify([
					"PROCEDURE proc-1@v1 supply=sup-1: Title - step 1; step 2",
					"PROCEDURE proc-2@v1 supply=sup-2: Another - step A",
				]),
			};
		};

		const baseCmd = ["uv", "run", "--project", "/opt/omp-knowledge", "python", "-m", "omp_knowledge.learning", "--state-dir", "/data/state", "--workspace", "ws-123"];
		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(baseCmd) },
			run: fakeRunner,
		});

		expect(lines).toEqual([
			"PROCEDURE proc-1@v1 supply=sup-1: Title - step 1; step 2",
			"PROCEDURE proc-2@v1 supply=sup-2: Another - step A",
		]);
		expect(capturedCommand).toEqual([
			...baseCmd,
			"supply",
			"--work-key",
			"OMP-100",
			"--project-id",
			"proj-1",
			"--cwd",
			"/repo/test",
			"--json",
		]);
	});

	test("omits --project-id when projectId is absent or undefined", async () => {
		let capturedCommand: string[] = [];
		const fakeRunner: ProcedureRunner = (cmd) => {
			capturedCommand = cmd;
			return { exitCode: 0, stdout: "[]" };
		};

		const baseCmd = ["omp-learning"];
		const lines = await procedureDigestLines(
			{ workKey: "OMP-200", cwd: "/another/dir" },
			{
				env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(baseCmd) },
				run: fakeRunner,
			},
		);

		expect(lines).toEqual([]);
		expect(capturedCommand).toEqual([
			"omp-learning",
			"supply",
			"--work-key",
			"OMP-200",
			"--cwd",
			"/another/dir",
			"--json",
		]);
	});

	test("returns unavailable line on non-zero runner exit code", async () => {
		const fakeRunner: ProcedureRunner = () => ({
			exitCode: 1,
			stderr: "database locked",
			stdout: "",
		});

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(["runner"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["PROCEDURES: unavailable (exit 1)"]);
	});

	test("returns unavailable line on status non-zero", async () => {
		const fakeRunner: ProcedureRunner = () => ({
			status: 2,
			stdout: "",
		});

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(["runner"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["PROCEDURES: unavailable (exit 2)"]);
	});

	test("returns unavailable line on timeout flag", async () => {
		const fakeRunner: ProcedureRunner = () => ({
			timedOut: true,
			stdout: "",
		});

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(["runner"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["PROCEDURES: unavailable (timeout)"]);
	});

	test("returns unavailable line on runner error with ETIMEDOUT", async () => {
		const err = new Error("spawnSync ETIMEDOUT");
		(err as { code: string }).code = "ETIMEDOUT";
		const fakeRunner: ProcedureRunner = () => ({
			error: err,
			stdout: "",
		});

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(["runner"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["PROCEDURES: unavailable (timeout)"]);
	});

	test("returns unavailable line on rejected promise with timeout", async () => {
		const fakeRunner: ProcedureRunner = () => Promise.reject(new Error("operation timeout"));

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(["runner"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["PROCEDURES: unavailable (timeout)"]);
	});

	test("returns unavailable line when runner stdout is bad JSON", async () => {
		const fakeRunner: ProcedureRunner = () => ({
			exitCode: 0,
			stdout: "malformed JSON {",
		});

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(["runner"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["PROCEDURES: unavailable (bad JSON)"]);
	});

	test("returns unavailable line when env OMP_KNOWLEDGE_LEARNING_CMD is bad JSON", async () => {
		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: "not-json[" },
			run: () => ({ exitCode: 0, stdout: "[]" }),
		});

		expect(lines).toEqual(["PROCEDURES: unavailable (bad JSON)"]);
	});

	test("never throws when runner throws unexpected exception", async () => {
		const fakeRunner: ProcedureRunner = () => {
			throw new Error("unexpected spawn failure");
		};

		const lines = await procedureDigestLines(defaultInput, {
			env: { OMP_KNOWLEDGE_LEARNING_CMD: JSON.stringify(["runner"]) },
			run: fakeRunner,
		});

		expect(lines).toEqual(["PROCEDURES: unavailable (unexpected spawn failure)"]);
	});
});
