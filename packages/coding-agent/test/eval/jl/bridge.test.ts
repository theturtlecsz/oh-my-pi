import { describe, expect, it } from "bun:test";
import * as path from "node:path";
import { $which, TempDir } from "@oh-my-pi/pi-utils";
import RUNNER_SCRIPT from "../../../src/eval/jl/runner.jl" with { type: "text" };

const JULIA_PATH = $which("julia");
const HAS_JULIA = Boolean(JULIA_PATH);

// The bridge marshals its payload with the runner's own JSON codec, so the
// fixture evaluates that slice of runner.jl rather than a stand-in. Markers are
// checked so a reorder fails loudly instead of silently dropping the helpers.
function runnerJsonHelpers(): string {
	const start = RUNNER_SCRIPT.indexOf("function json_parse(s::String)");
	const end = RUNNER_SCRIPT.indexOf("function emit_frame(frame)");
	if (start < 0 || end < 0 || end <= start) {
		throw new Error("runner.jl JSON codec markers moved; update the Julia bridge fixture");
	}
	return RUNNER_SCRIPT.slice(start, end);
}

// Loads the production prelude the way the runner does (Core.eval into Main),
// then drives one real `tool.<name>(args)` call through the loopback bridge.
// The sentinels are the observable contract: loading the prelude must NOT pay
// the `Downloads` stdlib load (OMP-340), and the bridge call must still return
// the host's value with the module loaded lazily on that first call. The
// prelude arrives as a file rather than an embedded string literal so its own
// `$`-interpolation is evaluated by Julia, not by this fixture.
function buildDriver(): string {
	return `${runnerJsonHelpers()}

global current_rid = nothing

function __downloads_loaded()
    return any(m -> nameof(m) == :Downloads, values(Base.loaded_modules))
end

Core.eval(Main, Meta.parse("begin\\n" * read(ARGS[1], String) * "\\nend"))

println("DOWNLOADS_AFTER_PRELUDE=", __downloads_loaded())
Main.current_rid = "jl-bridge-fixture"
println("TOOL_VALUE=", Main.tool.read(path = "artifact://alpha"))
println("DOWNLOADS_AFTER_TOOL=", __downloads_loaded())
println("TOOL_VALUE_2=", Main.tool.read(path = "artifact://beta"))
`;
}

describe.skipIf(!HAS_JULIA)("eval Julia bridge lazy stdlib load", () => {
	it("keeps the Downloads load off kernel startup and still round-trips tool calls", async () => {
		const seen: Array<{ session: string; run: string; name: string; args: Record<string, unknown> }> = [];
		const bridge = Bun.serve({
			hostname: "127.0.0.1",
			port: 0,
			async fetch(request) {
				const url = new URL(request.url);
				if (request.method !== "POST" || url.pathname !== "/v1/tool") {
					return new Response("Not Found", { status: 404 });
				}
				const body = (await request.json()) as {
					session: string;
					run: string;
					name: string;
					args: Record<string, unknown>;
				};
				seen.push(body);
				return Response.json({ ok: true, value: `read:${body.args.path}` });
			},
		});

		try {
			using tempDir = TempDir.createSync("@omp-eval-julia-bridge-");
			const preludePath = path.join(tempDir.path(), "prelude.jl");
			const driverPath = path.join(tempDir.path(), "driver.jl");
			await Bun.write(
				preludePath,
				await Bun.file(new URL("../../../src/eval/jl/prelude.jl", import.meta.url)).text(),
			);
			await Bun.write(driverPath, buildDriver());
			const proc = Bun.spawn(
				[
					JULIA_PATH as string,
					"--startup-file=no",
					"--history-file=no",
					"--color=no",
					"--project=@.",
					driverPath,
					preludePath,
				],
				{
					cwd: process.cwd(),
					env: {
						...process.env,
						PI_TOOL_BRIDGE_URL: bridge.url.toString(),
						PI_TOOL_BRIDGE_TOKEN: "test-token",
						PI_TOOL_BRIDGE_SESSION: "test-session",
					},
					stdout: "pipe",
					stderr: "pipe",
				},
			);
			const [stdout, stderr, exitCode] = await Promise.all([
				new Response(proc.stdout).text(),
				new Response(proc.stderr).text(),
				proc.exited,
			]);
			expect(stderr).toBe("");
			expect(exitCode).toBe(0);

			const value = (key: string): string | undefined =>
				stdout
					.split("\n")
					.find(line => line.startsWith(`${key}=`))
					?.slice(key.length + 1);
			// Regression guard for OMP-340: an eager `using Downloads` in the
			// prelude reintroduces the cold-depot compile that blew the kernel
			// startup budget.
			expect(value("DOWNLOADS_AFTER_PRELUDE")).toBe("false");
			expect(value("TOOL_VALUE")).toBe("read:artifact://alpha");
			expect(value("DOWNLOADS_AFTER_TOOL")).toBe("true");
			expect(value("TOOL_VALUE_2")).toBe("read:artifact://beta");

			expect(seen).toEqual([
				{
					session: "test-session",
					run: "jl-bridge-fixture",
					name: "tool:read",
					args: { path: "artifact://alpha" },
				},
				{
					session: "test-session",
					run: "jl-bridge-fixture",
					name: "tool:read",
					args: { path: "artifact://beta" },
				},
			]);
		} finally {
			bridge.stop(true);
		}
	}, 120_000);
});
