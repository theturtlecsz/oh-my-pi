import * as fs from "node:fs";
import * as path from "node:path";

const repoRoot = path.resolve(import.meta.dir, "../../../..");

const SYSTEM_ENTRY_NAMES = ["bin", "lib", "lib64", "sbin", "lib32", "libx32"] as const;
type SystemEntryName = (typeof SYSTEM_ENTRY_NAMES)[number];

interface HostSystemLayout {
	symlinks: Array<{ name: SystemEntryName; target: string }>;
	realDirs: SystemEntryName[];
}

export function resolveHostSystemLayout(): HostSystemLayout {
	const symlinks: Array<{ name: SystemEntryName; target: string }> = [];
	const realDirs: SystemEntryName[] = [];
	for (const name of SYSTEM_ENTRY_NAMES) {
		const hostPath = `/${name}`;
		try {
			const stat = fs.lstatSync(hostPath);
			if (stat.isSymbolicLink()) {
				symlinks.push({ name, target: fs.readlinkSync(hostPath) });
			} else if (stat.isDirectory()) {
				realDirs.push(name);
			}
		} catch {
			// absent on this host
		}
	}
	return { symlinks, realDirs };
}

export function populateHostSystemLayout(root: string, layout: HostSystemLayout = resolveHostSystemLayout()): void {
	for (const { name, target } of layout.symlinks) {
		fs.symlinkSync(target, path.join(root, name));
	}
	for (const name of layout.realDirs) {
		fs.mkdirSync(path.join(root, name));
	}
}

/**
 * User and mount namespace whose `/work` is `root/work`. Host `/usr` is bound
 * in so the dynamic linker survives `pivot_root`; host system layout is mirrored
 * so the dynamic linker loader path exists; the resolved Bun binary and
 * checkout are bound under `/run` regardless of their host paths.
 */
export function pivotScript(
	root: string,
	layout: HostSystemLayout = resolveHostSystemLayout(),
	command = "exec /run/bun --no-env-file /run/checkout/packages/coding-agent/src/cli.ts orchestrator-worker",
	isolated = false,
): string {
	const realDirBinds = layout.realDirs.map(name => `mount --bind /${name} ${root}/${name}`).join("\n");
	return `
set -euo pipefail
mount --make-rprivate /
mount --bind /usr ${root}/usr
mount --rbind /proc ${root}/proc
mount --rbind /dev ${root}/dev
mount --rbind /sys ${root}/sys
${realDirBinds ? `${realDirBinds}\n` : ""}touch ${root}/run/bun
mount --bind ${fs.realpathSync(process.execPath)} ${root}/run/bun
mkdir ${root}/run/checkout
mount --bind ${repoRoot} ${root}/run/checkout
${isolated ? ["usr", "run/bun", "run/checkout", ...layout.realDirs].map(name => `mount -o remount,bind,ro ${root}/${name}`).join("\n") : ""}
mount --rbind ${root} ${root}
cd ${root}
pivot_root . old
${isolated ? "PATH=/usr/bin:/bin:/usr/sbin:/sbin umount -l /old" : ""}
cd /work
${command}
`;
}
