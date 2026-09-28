# Runtime Defaults Lockdown Report

Evaluation of default execution behaviors, trust policies, and network exposure for unattended agent operations.

## Overview

Upstream defaults prioritize frictionless developer ergonomics, defaulting to auto-approval (`yolo`), implicit project trust (`isProjectTrusted: () => true`), and loading workspace-local extensions. In unattended and automated multi-tenant environments, these defaults introduce risk when operating on untrusted repositories or pull request checkouts.

See [README.md](./README.md) for report scope, column definitions, and upstream divergence taxonomy.

## Proposed Runtime Locks

| ID | Lock | Location | Affected runs | Breaks | Upstream divergence |
| --- | --- | --- | --- | --- | --- |
| L-RT-01 | Default tools.approvalMode to "write" off-TTY | [settings-schema.ts](../../../packages/coding-agent/src/config/settings-schema.ts): `tools.approvalMode` | Headless CLI sessions, non-interactive invocations, unattended automated jobs | Automated scripts without explicit approval flags fail or prompt on exec tools instead of auto-executing | adds |
| L-RT-02 | Children inherit parent approval mode instead of forcing yolo | [executor.ts](../../../packages/coding-agent/src/task/executor.ts): `runSubprocess` overrides | Subagent tasks, delegated worker invocations under non-yolo parent sessions | Subagents spawned by interactive or restricted parents prompt or abort rather than proceeding automatically | adds |
| L-RT-03 | Set isProjectTrusted to false outside trusted-path list | [agent-session.ts](../../../packages/coding-agent/src/session/agent-session.ts) and [runner.ts](../../../packages/coding-agent/src/extensibility/extensions/runner.ts): `isProjectTrusted: () => true` | Agent sessions running in untrusted repositories, unknown workspaces, pull request checkouts | Workspace-level capabilities, scripts, and extensions requiring project trust are disabled unless workspace is explicitly trusted | adds |
| L-RT-04 | Skip cwd .omp extensions in untrusted workspaces | [loader.ts](../../../packages/coding-agent/src/extensibility/extensions/loader.ts): `discoverExtensionPaths` | Workspaces containing local .omp/extensions on untrusted paths | Local extensions located in untrusted project directories will not be automatically loaded or executed | adds |
| L-RT-05 | Bind metaharness to 127.0.0.1 with bearer token authentication | [server.ts](../../../packages/metaharness/src/server.ts): `Bun.serve` / `ManagerServer.start` | Metaharness HTTP server, local benchmark dashboard and runners | Unauthenticated network callers and requests missing bearer token parameter or header are rejected | modified (applied: OMP-396-s02) |
