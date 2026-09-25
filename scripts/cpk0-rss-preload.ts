#!/usr/bin/env bun
/**
 * scripts/cpk0-rss-preload.ts — CPK-0 RSS capture preload (OMP-204-s03).
 *
 * When `CPK0_RSS_OUT` is set, writes the process's peak resident set size (RSS)
 * to that path from a synchronous `exit` handler. Used by scripts/cpk0-baseline.ts
 * to measure the memory overhead of the coding-agent CLI over a bare Bun runtime.
 */

import * as fs from "node:fs";

const out = process.env.CPK0_RSS_OUT;
if (out) {
	process.on("exit", () => {
		fs.writeFileSync(out, String(process.memoryUsage().rss));
	});
}
