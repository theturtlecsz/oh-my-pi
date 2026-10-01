export function bunVersionMismatch(running: string, packageManager: string): string | null {
	const runningMatch = /^v?(\d+)\.(\d+)/.exec((running ?? "").trim());
	if (!runningMatch) {
		return `running bun version ${running} has no major.minor`;
	}
	const runningMajorMinor = `${runningMatch[1]}.${runningMatch[2]}`;

	const trimmedSpec = (packageManager ?? "").trim();
	if (!trimmedSpec.startsWith("bun@")) {
		return `bun ${running} is running but package.json packageManager is ${packageManager || "<empty>"} (expected bun@<major>.<minor>)`;
	}

	const specAfterBun = trimmedSpec.slice("bun@".length);
	const specMatch = /(\d+)\.(\d+)/.exec(specAfterBun);
	if (!specMatch) {
		return `bun ${running} is running but package.json packageManager is ${packageManager} (missing major.minor version)`;
	}

	const specMajorMinor = `${specMatch[1]}.${specMatch[2]}`;
	if (runningMajorMinor !== specMajorMinor) {
		return `bun ${running} is running but package.json packageManager is ${packageManager} (major.minor ${runningMajorMinor} != ${specMajorMinor})`;
	}

	return null;
}
