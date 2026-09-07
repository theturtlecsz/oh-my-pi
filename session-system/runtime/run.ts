import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { isEnoent } from "@oh-my-pi/pi-utils/fs-error";
import { $ } from "bun";
import { isContained } from "../../packages/coding-agent/src/discovery/contained-path";
import { releaseFileSha256, type StagedReleaseManifest, verifyRelease } from "./manifest";

export interface RuntimeLaunch {
	command: string[];
	cwd: string;
	env: Record<string, string>;
	manifestSha256: string;
}

/** Construct a child environment; never inherit another installation's code/config/provider overrides. */
export function runtimeEnvironment(releaseRoot: string, stateRoot: string): Record<string, string> {
	return {
		HOME: path.join(stateRoot, "home"),
		XDG_CONFIG_HOME: path.join(stateRoot, "config"),
		XDG_STATE_HOME: path.join(stateRoot, "state"),
		XDG_DATA_HOME: path.join(stateRoot, "data"),
		XDG_CACHE_HOME: path.join(stateRoot, "cache"),
		TMPDIR: path.join(stateRoot, "tmp"),
		PATH: `${path.join(releaseRoot, "bin")}${path.delimiter}/usr/local/bin${path.delimiter}/usr/bin${path.delimiter}/bin`,
		TERM: process.env.TERM ?? "dumb",
		LANG: process.env.LANG ?? "C.UTF-8",
		PI_CODING_AGENT_DIR: path.join(stateRoot, "home/.omp/agent"),
		OMP_DISCOVERY_CWD: path.join(stateRoot, "discovery"),
		OMP_RUNTIME_RELEASE: releaseRoot,
		PYTHONDONTWRITEBYTECODE: "1",
	};
}

interface RuntimeState {
	schemaVersion: 1;
	manifestSha256: string;
	releaseRoot: string;
	sourceCommit: string;
	managedLinks: string;
}

async function managedLinks(releaseRoot: string, env: Record<string, string>): Promise<string> {
	return $`bash ${path.join(releaseRoot, "source/session-system/install.sh")} --print-manifest`
		.cwd(releaseRoot)
		.env(env)
		.quiet()
		.text();
}

async function prepareState(
	releaseRoot: string,
	stateRoot: string,
	workspace: string,
	manifest: StagedReleaseManifest,
	manifestSha256: string,
	env: Record<string, string>,
): Promise<void> {
	const canonical = path.join(await fs.realpath(path.dirname(stateRoot)), path.basename(stateRoot));
	if (
		canonical !== stateRoot ||
		stateRoot === os.homedir() ||
		stateRoot === path.parse(stateRoot).root ||
		isContained(releaseRoot, stateRoot) ||
		isContained(stateRoot, releaseRoot) ||
		isContained(workspace, stateRoot) ||
		isContained(stateRoot, workspace)
	)
		throw new Error("Runtime state must be a distinct absolute directory outside installation and workspace");
	let exists = true;
	try {
		const stat = await fs.lstat(stateRoot);
		if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("Runtime state cannot be a file or symlink");
	} catch (error) {
		if (!isEnoent(error)) throw error;
		exists = false;
	}
	const stateFile = path.join(stateRoot, "runtime.json");
	if (exists) {
		const state = (await Bun.file(stateFile).json()) as RuntimeState;
		if (state.schemaVersion !== 1 || state.manifestSha256 !== manifestSha256 || state.releaseRoot !== releaseRoot) {
			throw new Error("Runtime state belongs to another installation; use a new state directory for an upgrade");
		}
		if ((await managedLinks(releaseRoot, env)) !== state.managedLinks) {
			throw new Error("Installed workflow links changed; restore admitted assets before restarting");
		}
		return;
	}
	await fs.mkdir(stateRoot, { mode: 0o700 });
	for (const directory of [
		env.HOME,
		env.XDG_CONFIG_HOME,
		env.XDG_STATE_HOME,
		env.XDG_DATA_HOME,
		env.XDG_CACHE_HOME,
		env.TMPDIR,
		env.OMP_DISCOVERY_CWD,
	]) {
		await fs.mkdir(directory, { recursive: true, mode: 0o700 });
	}
	// Reuse the existing installer against a fresh private home. Every managed link
	// targets the verified release, never the checkout being edited.
	await $`bash ${path.join(releaseRoot, "source/session-system/install.sh")}`.cwd(releaseRoot).env(env).quiet();
	const state: RuntimeState = {
		schemaVersion: 1,
		manifestSha256,
		releaseRoot,
		sourceCommit: manifest.source.commit,
		managedLinks: await managedLinks(releaseRoot, env),
	};
	await Bun.write(stateFile, `${JSON.stringify(state, null, 2)}\n`);
	await fs.chmod(stateFile, 0o600);
}

const CONTROL_FLAGS = new Set([
	"--cwd",
	"--profile",
	"--config",
	"--extension",
	"-e",
	"--hook",
	"--trusted-extension",
	"--plugin-dir",
	"--session-dir",
	"--append-system-prompt",
	"--system-prompt",
	"--model-roles",
]);

/** Verify installation before creating state or starting the real CLI/WorkService process. */
export async function prepareLaunch(options: {
	releaseRoot: string;
	stateRoot: string;
	workspace: string;
	expectedManifestSha256: string;
	args: string[];
	service?: boolean;
	postgresPort?: number;
}): Promise<RuntimeLaunch> {
	if (![options.releaseRoot, options.stateRoot, options.workspace].every(value => path.isAbsolute(value))) {
		throw new Error("Installation, state, and workspace paths must be absolute");
	}
	const releaseRoot = await fs.realpath(options.releaseRoot);
	const workspace = await fs.realpath(options.workspace);
	if (!(await fs.stat(workspace).then(stat => stat.isDirectory()))) throw new Error("Workspace must be a directory");
	const stateRoot = path.normalize(options.stateRoot);
	const manifest = await verifyRelease(releaseRoot, options.expectedManifestSha256);
	const manifestSha256 = await releaseFileSha256(path.join(releaseRoot, "manifest.json"));
	if (!options.service) {
		for (const arg of options.args) {
			if (CONTROL_FLAGS.has(arg.split("=", 1)[0]) || /^-e.+/.test(arg)) {
				throw new Error(`Managed installation does not accept runtime replacement flag: ${arg.split("=", 1)[0]}`);
			}
		}
	}
	const env = runtimeEnvironment(releaseRoot, stateRoot);
	if (options.postgresPort !== undefined) {
		if (
			!options.service ||
			!Number.isInteger(options.postgresPort) ||
			options.postgresPort < 1 ||
			options.postgresPort > 65535
		) {
			throw new Error("An explicit PostgreSQL port must be an integer from 1 to 65535 for a service launch");
		}
		env.OMP_WORK_POSTGRES_PORT = String(options.postgresPort);
	}
	await prepareState(releaseRoot, stateRoot, workspace, manifest, manifestSha256, env);
	const command = options.service
		? [path.join(releaseRoot, manifest.python.path), "-I", "-B", "-m", "omp_work", ...options.args]
		: [
				path.join(releaseRoot, manifest.bun.path),
				path.join(releaseRoot, "source/packages/coding-agent/src/cli.ts"),
				"--cwd",
				workspace,
				"--trusted-extension",
				path.join(releaseRoot, "source/session-system/extensions/work-now.ts"),
				"--trusted-extension",
				path.join(releaseRoot, "source/session-system/extensions/model-bookends.ts"),
				...options.args,
			];
	return { command, cwd: env.OMP_DISCOVERY_CWD, env, manifestSha256 };
}

if (import.meta.main) {
	const [releaseRoot, stateRoot, workspace, expectedManifestSha256, ...rawArgs] = process.argv.slice(2);
	if (!releaseRoot || !stateRoot || !workspace || !expectedManifestSha256) {
		throw new Error("Use the installation's bin/omp launcher with state, workspace, and manifest SHA256");
	}
	const service = rawArgs[0] === "--service";
	const args = service ? rawArgs.slice(1) : rawArgs;
	let postgresPort: number | undefined;
	if (service && args[0] === "--postgres-port") {
		postgresPort = Number(args[1]);
		args.splice(0, 2);
	}
	const launch = await prepareLaunch({
		releaseRoot,
		stateRoot,
		workspace,
		expectedManifestSha256,
		service,
		postgresPort,
		args,
	});
	const child = Bun.spawn(launch.command, {
		cwd: launch.cwd,
		env: launch.env,
		stdin: "inherit",
		stdout: "inherit",
		stderr: "inherit",
	});
	const interrupt = () => child.kill("SIGINT");
	const terminate = () => child.kill("SIGTERM");
	process.on("SIGINT", interrupt);
	process.on("SIGTERM", terminate);
	try {
		process.exitCode = await child.exited;
	} finally {
		process.off("SIGINT", interrupt);
		process.off("SIGTERM", terminate);
	}
}
