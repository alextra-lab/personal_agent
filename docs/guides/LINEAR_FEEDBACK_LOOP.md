# Captain's Log → Linear feedback loop (ADR-0040)

Operational guide for promoting Captain's Log entries to Linear and processing your label-based feedback.

## Prerequisites

1. **Service** running with Second Brain / scheduler (e.g. `AGENT_ENABLE_SECOND_BRAIN=true`).
2. **Linear API key** in `AGENT_LINEAR_API_KEY`. The Linear client calls the Linear API directly. The MCP gateway is not used. Without the key the service logs `linear_client_unavailable`, and promotion and feedback polling do not run.
3. **One-time labels** in your Linear workspace (team-scoped): run with the key set:

   ```bash
   uv run python -m personal_agent.captains_log.linear_client
   ```

   This ensures feedback labels exist so the poller can classify issues.

## Configuration

All variables use the `AGENT_` prefix (see `AppConfig` in `src/personal_agent/config/settings.py`).

| Variable | Role |
|----------|------|
| `AGENT_PROMOTION_PIPELINE_ENABLED` | Promotion of promotable CL entries to Linear. The promotion handler runs on each `consolidation.completed` event. When the flag is `false`, the handler returns and nothing is filed (default `true`). |
| `AGENT_INSIGHTS_ENABLED` | Insights analysis on each `consolidation.completed` event. When the flag is `false`, the handler returns before it reads telemetry (default `true`). |
| `AGENT_INSIGHTS_WIRING_ENABLED` | Whether insights results are published to the event bus and saved as Captain's Log proposals. When the flag is `false`, the analysis still runs and indexes to `agent-insights-*` (default `true`). |
| `AGENT_FEEDBACK_POLLING_ENABLED` | Daily poll of Linear for label changes. When the flag is `false`, the daily job does not call Linear (default `true`). |
| `AGENT_FEEDBACK_POLLING_HOUR_UTC` | Hour (0–23 UTC) for the daily feedback job (default `7`). |
| `AGENT_SESHAT_OPEN_TICKET_CAP` | Skip promotion at or above this many open tickets that **the promotion pipeline filed** (default `10`). The count uses the `pipeline-filed` label. Tickets that the in-turn tool filed do not count. The in-turn tool refuses to file at the same cap, over the same count (FRE-1354, FRE-1611). |
| `AGENT_PROMOTION_MIN_SEEN_COUNT` | Corroboration bar a reinforced proposal must reach to stay promotable (default `3`). |
| `AGENT_PROMOTION_INITIAL_CAP` | Max new issues per promotion run (default `5`). |
| `AGENT_LINEAR_TEAM_NAME` | Linear team name (default `FrenchForest`). |
| `AGENT_LINEAR_PROMOTION_PROJECT` | Project name for new issues (default `Linear Async Feedback Channel`). Must exist — promotion refuses loudly otherwise. |
| `AGENT_FEEDBACK_SUPPRESSION_DAYS` | How long rejections suppress similar fingerprints (default `30`). |
| `AGENT_FEEDBACK_MAX_REEVALUATIONS` | Cap on Deepen / Too Vague rounds per issue (default `2`). |
| `AGENT_FEEDBACK_DEFER_REVISIT_DAYS` | Defer window placeholder (default `90`). |
| `AGENT_OUTCOME_INGESTION_ENABLED` | Daily ticket-outcome ingestion into the realized-value signal, ADR-0105 D7 (default `true`). |
| `AGENT_OUTCOME_INGESTION_HOUR_UTC` | Hour (0–23 UTC) for the daily outcome-ingestion job (default `8`). |
| `AGENT_SIGNAL_WINDOW_DAYS` | Trailing window (days) the realized-value signal is computed over (default `90`). |
| `AGENT_DECIDED_SKIP_DAYS` | Days a terminal outcome keeps skipping new proposals of the same source, category and scope (default `90`, FRE-1610). |
| `AGENT_SIGNAL_SMOOTHING_PRIOR` | Additive-smoothing prior in `v = Σweights / (n + prior)` (default `2.0`). |
| `AGENT_SIGNAL_PRIORITY_CLAMP` | Bound on promotion-ranking modulation (default `0.5`). |
| `AGENT_SIGNAL_SUPPRESSION_THRESHOLD` | `v` at/below this value triggers suppression (default `-0.4`). |
| `AGENT_SIGNAL_SUPPRESSION_MIN_N` | Minimum in-window outcomes before suppression can trigger (default `5`). |
| `AGENT_SIGNAL_SUPPRESSION_COOLDOWN_DAYS` | Suppression cooldown once triggered (default `30`). |

Copy commented defaults from `.env.example` into `.env` and uncomment to override.

## Schedules

- **Promotion**: **Event-driven.** It runs after each `consolidation.completed` event (ADR-0041 Phase 3). There is no weekday or hour setting. A run files at most `AGENT_PROMOTION_INITIAL_CAP` tickets and stops at the open-ticket cap.
- **Feedback polling**: **Daily** at `AGENT_FEEDBACK_POLLING_HOUR_UTC`.
- **Outcome ingestion** (ADR-0105 D7): **Daily** at `AGENT_OUTCOME_INGESTION_HOUR_UTC` — sweeps promoted tickets awaiting an outcome, classifies shipped/owner-rejected/canceled-as-noise from Linear workflow state + labels, and updates the realized-value signal the next promotion run reads back to rank and suppress proposals.
- **Insights** (related): **Event-driven.** It runs after each `consolidation.completed` event. The daily and weekly hour settings were removed in FRE-896.

## Telemetry

- Poller state: `telemetry/feedback_history/feedback_poller_state.json` (gitignored).
- Feedback history: `telemetry/feedback_history/<issue_identifier>.json` (gitignored).
- Rejection suppression: `telemetry/feedback_history/suppressed_fingerprints.json` (gitignored).
- Funnel refusals: the index `agent-captains-funnel-events-YYYY-MM` records each refusal by the ticket cap (`throttled_budget`) and each misconfigured project.

## Giving feedback on agent-filed issues

The agent creates issues two ways:
1. **Promotion pipeline** — `CL-*.json` entries promoted automatically once `seen_count ≥ AGENT_PROMOTION_MIN_SEEN_COUNT` (default 3) and age ≥ 7 days. Labels: `PersonalAgent`, `Improvement`, `agent-filed`, `pipeline-filed`.
2. **Direct tool call** — agent calls `create_linear_issue` during a conversation. Labels: `PersonalAgent`, `agent-filed`. The tool refuses to file a title that matches an existing issue in any state except `Duplicate`. A `Canceled` issue blocks the title.

Only the `pipeline-filed` label counts toward `AGENT_SESHAT_OPEN_TICKET_CAP`.

**AgentFeedback labels work on ALL `PersonalAgent` issues**, including both types above. The `FeedbackPoller` lists issues with `label="PersonalAgent"` that changed in the last 3 days (up to 50), which covers the union.

### How to give feedback

Apply an `AgentFeedback/*` label to the issue in Linear. Do not leave a comment — comments are human-readable but not yet machine-processed (Phase 3 work, FRE-183).

| Label | What happens |
|-------|-------------|
| `AgentFeedback/Approved` | The poller moves the issue to the `Approved` state |
| `AgentFeedback/Rejected` | Fingerprint suppressed for `AGENT_FEEDBACK_SUPPRESSION_DAYS` days; the issue is canceled |
| `AgentFeedback/Deepen` | Agent comments a more detailed analysis, adds `Re-evaluated`, and returns the issue to `Needs Approval` (up to `AGENT_FEEDBACK_MAX_REEVALUATIONS` times) |
| `AgentFeedback/Too Vague` | Same as Deepen, with a more specific proposal and the `Refined` label |
| `AgentFeedback/Duplicate` | **Does not suppress.** The poller sets the issue to `Duplicate` of the issue with the same fingerprint, which keeps the idea alive. If no original is found, it comments |
| `AgentFeedback/Defer` | **Does not suppress.** The poller records the date only; the revisit window is a placeholder |

### What a cancel does (FRE-1610)

Cancel a ticket in Linear and the next daily outcome-ingestion run suppresses its fingerprint for
`AGENT_FEEDBACK_SUPPRESSION_DAYS` days. The `Rejected` label is not needed. State `Duplicate` does not
suppress.

A terminal outcome (`Done`, `Canceled`, `Duplicate`) also marks the kind decided. A decided kind skips new
proposals with the same source, category and scope for `AGENT_DECIDED_SKIP_DAYS` days, counted from the
outcome. A proposal with another scope is not skipped. After the window, nothing is skipped. A proposal
that was not written during the window starts again at `seen_count` 1, so it must earn the normal bar
(3 sightings, 7 days) with new evidence. The promotion scan applies the same rule to entries already on disk.

### Triage tips

- Use Linear's **filter by label** to see all pending agent proposals: filter `label = PersonalAgent` + `state = Needs Approval`.
- Bulk-label from mobile: Linear's iOS app supports label editing on individual issues.
- If an issue is clearly wrong (eval artifact, fictional project), apply `AgentFeedback/Rejected` — or cancel it directly. Both suppress the fingerprint.

## Normative design

- Spec: [SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md](../specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md)
- ADR: [ADR-0040-linear-async-feedback-channel.md](../architecture_decisions/ADR-0040-linear-async-feedback-channel.md)
