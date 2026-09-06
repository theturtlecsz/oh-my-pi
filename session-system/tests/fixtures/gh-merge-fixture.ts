/** Local-only GitHub merge simulator. The real helper invokes this through a
 * child-local PATH; no global mocks, credentials, or network are used. */
import * as path from "node:path";
import { runGit } from "../../extensions/workflow/git";

interface FixtureConfig {
	remote: string;
}

const root = process.cwd();
const config: FixtureConfig = await Bun.file(path.join(root, ".git", "merge-fixture.json")).json();
const remote = path.resolve(config.remote);
if (remote !== path.join(path.dirname(root), "remote.git")) {
	throw new Error("merge fixture refuses a remote outside its local container");
}

const [command, action, branch, ...options] = process.argv.slice(2);
if (command !== "pr" || action !== "merge" || !branch || !options.includes("--merge")) {
	throw new Error("unsupported fixture command");
}

function git(...args: string[]): string {
	const result = runGit(remote, args);
	if (!result.ok) throw new Error(result.err);
	return result.out;
}

const currentHead = git("rev-parse", `refs/heads/${branch}`);
const matchIndex = options.indexOf("--match-head-commit");
// Model GitHub's conditional mutation. Deliberately allow an unconditional
// merge when the option is absent so dropping it reproduces the original bug.
if (matchIndex !== -1 && options[matchIndex + 1] !== currentHead) {
	process.stderr.write("PR head no longer matches expected commit\n");
	process.exit(1);
}
const priorMain = git("rev-parse", "refs/heads/main");
const tree = git("rev-parse", `${currentHead}^{tree}`);
const merged = git("commit-tree", tree, "-p", priorMain, "-p", currentHead, "-m", "fixture merge");
git("update-ref", "refs/heads/main", merged, priorMain);
process.stdout.write(`Merged fixture PR ${branch}\n`);
