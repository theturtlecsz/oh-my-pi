── /center — centering snapshot @ {{takenAt}} ──
Scope: {{scope}}. Every fact below is a fresh Work Ledger read taken for this run; nothing else is known to be current.

NOW: {{#if nowLine}}{{nowLine}}{{else}}unset — no focus is selected{{/if}}
{{#if progressLine}}PROGRESS: {{progressLine}}
{{/if}}READY ({{readyTotal}} total):
{{#each readyRows}}- {{this}}
{{/each}}{{#if readyMore}}… and {{readyMore}} more
{{/if}}{{#unless readyRows}}(none)
{{/unless}}STUCK ON CHRIS ({{waitingTotal}} total):
{{#each waitingRows}}- {{this}}
{{/each}}{{#if waitingMore}}… and {{waitingMore}} more
{{/if}}{{#unless waitingRows}}(none)
{{/unless}}{{#if activityUnavailable}}WHAT JUST MOVED: unavailable this run ({{activityUnavailable}})
{{else}}WHAT JUST MOVED ({{activityTotal}} total):
{{#each activityRows}}- {{this}}
{{/each}}{{#if activityMore}}… and {{activityMore}} more
{{/if}}{{#unless activityRows}}(none)
{{/unless}}{{/if}}
Write Chris a centering orientation in plain household language under exactly these four headings, in this order:

**Where I am** — the current focus and scope, one or two sentences.{{#unless nowLine}} No focus is selected: say so plainly and point Chris to /now — never select work yourself.{{/unless}}
**What's next** — what is ready to work on, from the READY list; name honest counts when the list is truncated.
**Stuck on you** — the decisions waiting on Chris, from the STUCK ON CHRIS list.
**What just moved** — the most recent recorded activity.{{#if activityUnavailable}} The recent-activity read is unavailable this run — say so plainly instead of guessing.{{/if}}

Open the reply with "Centering @ {{takenAt}}". After the four sections, recommend exactly ONE next action with a short plain reason grounded only in the facts above.
This turn is read-only and chat-only: every tool is disabled. Do not attempt tool calls, Work Ledger writes, filings, or follow-up work — give the orientation and stop.
