import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { spawnSync } from "node:child_process"
import {
	existsSync,
	mkdirSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	writeFileSync,
} from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const hookScript = fileURLToPath(
	new URL("../hooks/task-observer-first-tool.mjs", import.meta.url),
)
const sid = "omp-154-main"
const sessionName = `1750000000000_${sid}`
const arbitraryTool = JSON.stringify({
	tool_name: "bash",
	tool_args: {},
})
const digestRead = JSON.stringify({
	tool_name: "read",
	tool_args: {
		path: "skill://task-observer/references/session-start.md",
	},
})
const fullSkillRead = JSON.stringify({
	tool_name: "read",
	tool_args: {
		path: "skill://task-observer",
	},
})
let home: string
let tempDir: string

function projectRoot(): string {
	return join(home, ".omp", "agent", "sessions", "project")
}

function markerPath(): string {
	const sanitizedSid = sid.replace(/[^\w.-]/g, "_")
	return join(tempDir, `task-observer-loaded.${sanitizedSid}`)
}

function createDirectoryLayout(): void {
	mkdirSync(join(projectRoot(), sessionName), { recursive: true })
}

function createTranscriptLayout(): void {
	const transcript = join(projectRoot(), `${sessionName}.jsonl`)
	mkdirSync(dirname(transcript), { recursive: true })
	writeFileSync(transcript, "")
}

function runHook(input: string): {
	status: number | null
	stderr: string
} {
	const result = spawnSync("node", [hookScript], {
		env: {
			...process.env,
			HOME: home,
			TMPDIR: tempDir,
			PI_SESSION_ID: sid,
		},
		input,
		encoding: "utf8",
	})

	if (result.error) throw result.error

	return {
		status: result.status,
		stderr: result.stderr,
	}
}

beforeEach(() => {
	home = mkdtempSync(join(tmpdir(), "omp-154-home-"))
	tempDir = mkdtempSync(join(tmpdir(), "omp-154-tmp-"))
})

afterEach(() => {
	rmSync(home, { recursive: true, force: true })
	rmSync(tempDir, { recursive: true, force: true })
})

describe("task-observer-first-tool", () => {
	test("blocks an arbitrary first tool when the top-level session directory exists", () => {
		createDirectoryLayout()

		const result = runHook(arbitraryTool)

		expect(result.status).toBe(2)
		expect(result.stderr).toContain("task-observer")
		expect(existsSync(markerPath())).toBe(false)
	})

	test("loads task-observer from the digest path and writes the loaded marker", () => {
		createDirectoryLayout()

		const result = runHook(digestRead)

		expect(result.status).toBe(0)
		expect(readFileSync(markerPath(), "utf8")).toContain("loaded")
	})

	test("loads task-observer from the full skill episode path and writes the loaded marker", () => {
		createDirectoryLayout()

		const result = runHook(fullSkillRead)

		expect(result.status).toBe(0)
		expect(readFileSync(markerPath(), "utf8")).toContain("loaded")
	})

	test("allows an arbitrary tool through the loaded-marker fast-path", () => {
		createDirectoryLayout()
		expect(runHook(digestRead).status).toBe(0)

		rmSync(join(home, ".omp", "agent", "sessions"), { recursive: true, force: true })
		const result = runHook(arbitraryTool)

		expect(result.status).toBe(0)
		expect(readFileSync(markerPath(), "utf8")).toContain("loaded")
	})

	test("falls back to classifying a top-level transcript as a main session", () => {
		createTranscriptLayout()

		const result = runHook(arbitraryTool)

		expect(result.status).toBe(2)
		expect(existsSync(markerPath())).toBe(false)
	})

	test("fails open without a marker for missing and nested-only layouts", () => {
		const missingLayoutResult = runHook(arbitraryTool)

		expect(missingLayoutResult.status).toBe(0)
		expect(existsSync(markerPath())).toBe(false)

		const nestedTranscript = join(
			projectRoot(),
			"1750000000000_other-session",
			"Sub.jsonl",
		)
		mkdirSync(dirname(nestedTranscript), { recursive: true })
		writeFileSync(nestedTranscript, "")

		const nestedLayoutResult = runHook(arbitraryTool)

		expect(nestedLayoutResult.status).toBe(0)
		expect(existsSync(markerPath())).toBe(false)
	})

	test("fails open without a marker for malformed stdin in a main layout", () => {
		createDirectoryLayout()

		const result = runHook("not JSON")

		expect(result.status).toBe(0)
		expect(existsSync(markerPath())).toBe(false)
	})

	test("does not accept a task-observer range selector on full skill or digest", () => {
		createDirectoryLayout()

		const result1 = runHook(JSON.stringify({
			tool_name: "read",
			tool_args: {
				path: "skill://task-observer:1-50",
			},
		}))
		expect(result1.status).toBe(2)
		expect(existsSync(markerPath())).toBe(false)

		const result2 = runHook(JSON.stringify({
			tool_name: "read",
			tool_args: {
				path: "skill://task-observer/references/session-start.md:1-50",
			},
		}))
		expect(result2.status).toBe(2)
		expect(existsSync(markerPath())).toBe(false)
	})

	test("session-start digest is <= 2,048 bytes", () => {
		const digestFile = fileURLToPath(
			new URL("../skills/task-observer/references/session-start.md", import.meta.url),
		)
		const bytes = readFileSync(digestFile).length
		expect(bytes).toBeGreaterThan(0)
		expect(bytes).toBeLessThanOrEqual(2048)
	})
})
