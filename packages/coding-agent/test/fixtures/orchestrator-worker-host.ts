// Exercise production CLI dispatch, including its require-loaded worker graph.
import "../../src/cli";

// Observe the jail's actual environment without logging ephemeral task grants.
const env = Object.fromEntries(
	Object.entries(process.env).filter(([name]) => name !== "OMP_MODEL_TOKEN" && name !== "OMP_TASK_TOKEN"),
);
process.stdout.write(`${JSON.stringify({ type: "worker_environment", env })}\n`);
