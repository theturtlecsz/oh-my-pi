#!/usr/bin/env node
// task-observer-first-tool for OMP (pi-yaml-hooks, tool.before.*)
// Structural activation enforcement: the first tool call of a MAIN session is
// blocked until skill://task-observer has been read. Lineage: obs #152 -> #157
// -> #160 -> #175 -> #177 (five recurrences of prose/notify activation failing
// on plan-approved and interrupt-opened sessions). OMP-154 closes the
// first-tool transcript-persistence gap.
//
// Design:
// - Marker file per session id in tmpdir; once written the guard is a single
//   stat() forever after.
// - Main-session identity is observed, not declared: a main session owns either
//   a TOP-LEVEL `<timestamp>_<sid>.jsonl` transcript or its same-named
//   TOP-LEVEL `<timestamp>_<sid>/` session directory under
//   ~/.omp/agent/sessions/<project>/. The directory exists before the first
//   tool result persists the transcript.
// - Subagent transcripts are `<Name>.jsonl` nested inside a parent's session
//   directory and have no matching top-level entry. pi-yaml-hooks `scope: main`
//   is NOT used: OMP's parent-session lineage resolves task-spawned sessions as
//   roots, so scope:main matched the OMP-89 auditor subagent (observed
//   2022-08-22, OMP-94 correction).
// - Subagents and unknown layouts fail open without writing a marker, so they
//   are reclassified on later calls. Once the main session writes its loaded
//   marker, task-spawned sessions sharing its id take the marker fast-path.
// - Only a read whose tool_args.path is exactly
//   skill://task-observer/references/session-start.md or skill://task-observer
//   passes before the marker exists; prefix matching would unblock on range
//   selectors loading a truncated file.
// - Fail open (exit 0) on missing session id, unparseable payload, or an
//   envelope without a usable tool_name: a shape change in the hook contract
//   must degrade to no-enforcement, never to blocking every tool with no
//   unblock path.

import fs from "node:fs"
import os from "node:os"
import path from "node:path"

const sessionId = String(process.env.PI_SESSION_ID ?? "")
if (!sessionId) process.exit(0)

const sanitizedSid = sessionId.replace(/[^\w.-]/g, "_")
const marker = path.join(os.tmpdir(), `task-observer-loaded.${sanitizedSid}`)
if (fs.existsSync(marker)) process.exit(0)

if (!hasMainSessionLayout(sessionId)) process.exit(0)

let payload
try {
  payload = JSON.parse(await readStdin())
} catch {
  process.exit(0)
}

const name = typeof payload?.tool_name === "string" ? payload.tool_name : ""
if (!name) process.exit(0) // envelope shape changed — degrade open, never brick

const toolPath = String(payload.tool_args?.path ?? "")
if (name === "read" && (toolPath === "skill://task-observer/references/session-start.md" || toolPath === "skill://task-observer")) {
  try { fs.writeFileSync(marker, "loaded\n") } catch {}
  process.exit(0)
}

console.error(
  "task-observer-first-tool: this session has not loaded the Task Observer yet (obs #152->#177 lineage). " +
  'Read it first — call the read tool with {"path":"skill://task-observer/references/session-start.md","i":"Loading Task Observer session start"} — ' +
  "then retry this exact tool call. Applies once per session."
)
process.exit(2)

function hasMainSessionLayout(sessionId) {
  // A main session owns either a top-level `<timestamp>_<sid>.jsonl` transcript
  // or a top-level `<timestamp>_<sid>/` directory in some project dir under
  // the sessions root. Nested-only and unknown layouts return false and are
  // deliberately left unmarked so a later tool call can reclassify them.
  const root = path.join(os.homedir(), ".omp", "agent", "sessions")
  let projects
  try {
    projects = fs.readdirSync(root)
  } catch {
    return false
  }

  const transcriptSuffix = `_${sessionId}.jsonl`
  const directorySuffix = `_${sessionId}`
  for (const project of projects) {
    let entries
    try {
      entries = fs.readdirSync(path.join(root, project), { withFileTypes: true })
    } catch {
      continue
    }

    if (entries.some((entry) => (
      entry.isFile() && entry.name.endsWith(transcriptSuffix)
    ) || (
      entry.isDirectory() && entry.name.endsWith(directorySuffix)
    ))) return true
  }

  return false
}

async function readStdin() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  return Buffer.concat(chunks).toString("utf8")
}
