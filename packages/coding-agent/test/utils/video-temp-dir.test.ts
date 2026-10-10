import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { describe, expect, it, spyOn } from "bun:test";
import {
	buildChangedFrameContactSheetPng,
	buildVideoContactSheetPng,
	extractVideoFramePng,
} from "@oh-my-pi/pi-coding-agent/utils/video";
import { TempDir } from "@oh-my-pi/pi-utils/temp";

describe("video temp dirs", () => {
	it("creates temporary directories under os.tmpdir()", async () => {
		const recordedDirs: TempDir[] = [];
		const recordedPaths: string[] = [];
		const realCreate = TempDir.create.bind(TempDir);
		const spy = spyOn(TempDir, "create").mockImplementation(async (prefix?: string) => {
			const dir = await realCreate(prefix);
			recordedDirs.push(dir);
			recordedPaths.push(dir.path());
			return dir;
		});

		try {
			const missingMp4 = path.join(os.tmpdir(), "missing-video.mp4");
			const outputPath = path.join(os.tmpdir(), "missing-video-change-sheet.png");

			await extractVideoFramePng(missingMp4, { kind: "frame", frame: 0 }).catch(() => {});
			await buildVideoContactSheetPng(missingMp4, {
				durationSec: 1,
				fps: 1,
				width: undefined,
				height: undefined,
				videoCodec: undefined,
				audioCodec: undefined,
				formatName: undefined,
			}).catch(() => {});
			await buildChangedFrameContactSheetPng(missingMp4, {
				outputPath,
				fps: 1,
				threshold: 0.1,
			}).catch(() => {});

			expect(recordedPaths).toHaveLength(3);
			const tmpDir = os.tmpdir();
			for (const recordedPath of recordedPaths) {
				expect(recordedPath.startsWith(tmpDir)).toBe(true);
			}
			expect(path.basename(recordedPaths[0]!)).toStartWith("omp-video-frame-");
			expect(path.basename(recordedPaths[1]!)).toStartWith("omp-video-sheet-");
			expect(path.basename(recordedPaths[2]!)).toStartWith("omp-video-change-sheet-");

			const cwdFiles = await fs.readdir(process.cwd());
			const prefixes = ["omp-video-frame-", "omp-video-sheet-", "omp-video-change-sheet-"];
			const leaked = cwdFiles.filter(file => prefixes.some(prefix => file.startsWith(prefix)));
			expect(leaked).toEqual([]);
		} finally {
			for (const dir of recordedDirs) {
				await dir.remove().catch(() => {});
			}
			spy.mockRestore();
		}
	});
});
