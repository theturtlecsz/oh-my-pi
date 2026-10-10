# WebUI research panel

`omp-webui` is not in this repository. Its panel imports `readResearchViews`,
`stopResearchCampaign`, and `openResearchEvidence` from
`@oh-my-pi/pi-work-client`. CLI and WebUI use these same helpers on a
`WorkClient(baseUrl, workspaceId, () => token)`. The client bearer identifies
the principal; stop takes no actor argument.

The panel keeps no campaign cache. Every read fetches `client.research(workId)`;
campaign state, revision, trials, graph, and evidence come from that service
view. Only a current research projection supplies usage and worker activity.
Stale, offline, or absent projections yield zero usage and empty worker activity.

The daemon would serve:

| Method | Route | Helper |
| --- | --- | --- |
| GET | `/api/research/:workId` | `readResearchViews(client, workId, projection?)` |
| POST | `/api/research/:workId/stop` | `stopResearchCampaign(client, {workId, campaignId, expectedRevision, reason, operationId})` |
| GET | `/api/research/sources/:sourceId` | `openResearchEvidence(client, sourceId, projectId?)` |

Stop re-reads the campaign and refuses unknown campaigns or stale revisions
before posting `cancel_research_campaign`. After stop, refresh the view to show
the service's state. Evidence opens the service source, including
`content_base64` when available.

Review annotations stay in `CandidateReviewSession`, bound to their expected
revision, and issue no ledger commands. Candidate summaries use each trial's
last observation; only PASS metrics contribute to the best metric.

`researchA11y` defines tab order through campaign, graph, evidence, usage,
workers, and review; arrows navigate graph nodes. Shortcuts `r`, `s`, and `e`
invoke summarize, stop, and evidence respectively. Touch targets are at least
44 pixels.
