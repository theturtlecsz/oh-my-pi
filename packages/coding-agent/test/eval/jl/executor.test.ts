import { afterEach, describe, expect, it, spyOn } from "bun:test";
import { $which, TempDir } from "@oh-my-pi/pi-utils";
import { disposeJuliaKernelSessionsByOwner, executeJulia } from "../../../src/eval/jl/executor";
import { JuliaKernel } from "../../../src/eval/jl/kernel";

const JULIA_PATH = $which("julia");
const HAS_JULIA = Boolean(JULIA_PATH);
const OWNER_ID = "julia-executor-tests";

describe.skipIf(!HAS_JULIA)("executeJulia cancellation and error reporting", () => {
	afterEach(async () => {
		await disposeJuliaKernelSessionsByOwner(OWNER_ID);
	});

	it("throws an explicit timeout error instead of returning exitCode undefined when deadline expires", async () => {
		using tempDir = TempDir.createSync("@omp-eval-julia-executor-");
		const promise = executeJulia("println(1)", {
			cwd: tempDir.path(),
			deadlineMs: Date.now() - 1_000,
			kernelOwnerId: OWNER_ID,
		});

		await expect(promise).rejects.toThrow(/Julia execution timed out/);
	});

	it("throws an explicit kernel exited error when kernel startup fails or cancels", async () => {
		using tempDir = TempDir.createSync("@omp-eval-julia-executor-");
		const abortError = new (class extends Error {
			override name = "AbortError";
		})("Julia kernel init cancelled");
		const startSpy = spyOn(JuliaKernel, "start").mockRejectedValueOnce(abortError);

		try {
			const promise = executeJulia("println(1)", {
				cwd: tempDir.path(),
				kernelOwnerId: OWNER_ID,
				reset: true,
			});

			await expect(promise).rejects.toThrow(/Julia kernel exited: Julia kernel init cancelled/);
		} finally {
			startSpy.mockRestore();
		}
	});

	it("returns a cancelled result when caller explicitly aborts execution signal", async () => {
		using tempDir = TempDir.createSync("@omp-eval-julia-executor-");
		const controller = new AbortController();
		controller.abort();

		const result = await executeJulia("println(1)", {
			cwd: tempDir.path(),
			signal: controller.signal,
			kernelOwnerId: OWNER_ID,
		});

		expect(result.cancelled).toBe(true);
		expect(result.exitCode).toBeUndefined();
	});
});
