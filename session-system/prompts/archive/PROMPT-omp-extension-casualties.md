# PROMPT — omp extension casualties: port, fix, or drop (HOME-25)

Paste into a fresh session. Successor to the 2026-08-10 omp-linear-weave
session (weave SHIPPED + live-proven; see memory
`omp-linear-weave-live-20260810`). RESEARCH FIRST — nothing installed/changed
without "g". omp-only focus stands (owner ruling 2026-08-10).

## What happened

Four ADHD-suite extensions are blocked by omp 17.x fork drift from upstream
pi. All captured as HOME-25 (project: The Bookends). omp validates extension
imports at install — failures were clean, nothing half-loaded.

## The casualties, with exact evidence

1. **@narumitw/pi-btw** (side-question padded room) — install REJECTED:
   `Export named 'TuiAltScreen' not found in module .../legacy-pi-tui-shim.ts`
2. **pi-continue** (mid-turn compaction survival) — install REJECTED:
   `Export named 'getSupportedThinkingLevels' not found in module .../legacy-pi-ai-shim.ts`
3. **@llblab/pi-telegram** (phone operator surface) — installs, but throws
   every session: `systemPrompt.split is not a function` (omp passes
   `systemPrompt: string[]` where pi passed a string). Currently DISABLED
   (`omp plugin disable` state, visible in `omp plugin list` as ⦸). Bot token
   also never configured — needs owner + @BotFather regardless.
4. **pi-web-ui** (browser session UI) — NOT installed: pins upstream
   `@earendil-works/pi-coding-agent ^0.83.0`; omp SDK is `@oh-my-pi/...` 17.x.
   Running it as-published means installing a parallel upstream-pi stack —
   contradicts the omp-only ruling.

## Options per casualty (research, then grill the owner)

- **Shim-gap pair (btw, continue)**: the missing exports live in omp's
  `src/extensibility/legacy-pi-*-shim.ts`. Options: (a) PR/patch omp's shims
  to add the exports (check what the real implementations would map to in omp
  17.x — may be trivial re-exports), (b) fork the extension and swap the
  imports to omp-native APIs, (c) file upstream compat issues on both repos
  and wait, (d) drop (omp has built-in todo phases; /capture already covers
  part of btw's job).
- **pi-telegram**: single-line-looking bug (systemPrompt array vs string) —
  fork + `Array.isArray` guard may be the whole fix. It's the highest-value
  casualty (phone surface + proactive push). Needs owner's BotFather token
  when it works.
- **pi-web-ui**: heaviest. Options: (a) fork + port to omp SDK
  (`createAgentSession` exists in omp's sdk.ts — API similarity unverified),
  (b) skip and revisit whether omp grows a native web surface, (c) accept a
  parallel plain-pi stack (owner previously ruled against). Time-box any port
  probe; this is a project, not a slice.

## Standing laws

- Every owner decision through AskUserQuestion, recommended option first,
  deciding data inside the dialog. Plain language.
- Nothing installed/changed without "g". Read-only probes free.
- Registered local patches: any forked/patched extension goes in the
  infra patch register (infra-work-rules memory).
- Adversarial pass before recommending a port lane (months-later reviews of
  the target repos, open compat issues, maintainer responsiveness).
- Capture-don't-chase: findings become Linear issues in their surface.

## State pointers

- Weave extension: `~/.omp/agent/extensions/linear-now.ts` (live; don't break
  it — it owns footer key `linear-now`, commands /now //done //capture
  //linear, tool `linear`)
- Plugins: `omp plugin list` → pi-pomodoro + rpiv-voice enabled,
  pi-telegram disabled, pi-yaml-hooks (idle-notify only)
- omp source for shim inspection:
  `/home/thetu/node_modules/@oh-my-pi/pi-coding-agent/src/extensibility/`
- Linear: HOME-25 holds the full capture; HOME-22 close-proposed (owner
  verdict pending); `now` label exists (#f2c94c)
- Key: `~/.config/linear.env`

## First move

Probe the two shim files for how close the missing exports are to existing
omp equivalents (read-only), check both extension repos for existing omp
compat issues, THEN open the AskUserQuestion round: which casualty first,
port vs patch vs drop per item.
