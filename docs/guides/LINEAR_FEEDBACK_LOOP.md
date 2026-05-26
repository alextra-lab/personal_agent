# Captain's Log → Linear feedback loop (ADR-0040)

Operational guide for promoting Captain's Log entries to Linear and processing your label-based feedback.

## Prerequisites

1. **Service** running with Second Brain / scheduler (e.g. `AGENT_ENABLE_SECOND_BRAIN=true`).
2. **MCP gateway** enabled (`AGENT_MCP_GATEWAY_ENABLED=true`) with **Linear** authorized in your Docker MCP setup.
3. **One-time labels** in your Linear workspace (team-scoped): run when MCP is up:

   ```bash
   uv run python -m personal_agent.captains_log.linear_client
   ```

   This ensures feedback labels exist so the poller can classify issues.

## Configuration

All variables use the `AGENT_` prefix (see `AppConfig` in `src/personal_agent/config/settings.py`).

| Variable | Role |
|----------|------|
| `AGENT_PROMOTION_PIPELINE_ENABLED` | Weekly promotion of promotable CL entries to Linear (default `true`). |
| `AGENT_FEEDBACK_POLLING_ENABLED` | Daily poll of Linear for label changes (default `true`). |
| `AGENT_FEEDBACK_POLLING_HOUR_UTC` | Hour (0–23 UTC) for the daily feedback job (default `7`). |
| `AGENT_ISSUE_BUDGET_THRESHOLD` | Skip promotion when non-archived issues exceed this count (default `200`). |
| `AGENT_PROMOTION_INITIAL_CAP` | Max new issues per promotion run (default `5`). |
| `AGENT_LINEAR_TEAM_NAME` | Linear team name (default `FrenchForest`). |
| `AGENT_LINEAR_PROMOTION_PROJECT` | Project name for new issues (default `2.3 Homeostasis & Feedback`). |
| `AGENT_FEEDBACK_SUPPRESSION_DAYS` | How long rejections suppress similar fingerprints (default `30`). |
| `AGENT_FEEDBACK_MAX_REEVALUATIONS` | Cap on Deepen / Too Vague rounds per issue (default `2`). |
| `AGENT_FEEDBACK_DEFER_REVISIT_DAYS` | Defer window placeholder (default `90`). |

Copy commented defaults from `.env.example` into `.env` and uncomment to override.

## Schedules

- **Promotion**: Fixed in code as **Sunday, 10:00 UTC** (`PROMOTION_WEEKDAY`, `PROMOTION_HOUR_UTC` in `src/personal_agent/brainstem/scheduler.py`). Changing this requires a code edit today; env-driven promotion time is not implemented yet.
- **Feedback polling**: **Daily** at `AGENT_FEEDBACK_POLLING_HOUR_UTC`.
- **Insights** (related): daily / weekly hours via `AGENT_INSIGHTS_*` in `.env.example`.

## Telemetry

- Poller state: `telemetry/feedback_poller_state.json` (gitignored).
- Feedback history: under `telemetry/feedback_history/` (gitignored).

## Giving feedback on agent-filed issues

The agent creates issues two ways:
1. **Promotion pipeline** — `CL-*.json` entries promoted automatically once `seen_count ≥ 3` and age ≥ 7 days. Label: `Improvement`.
2. **Direct tool call** — agent calls `create_linear_issue` during a conversation. Label: `agent-filed`.

**AgentFeedback labels work on ALL `PersonalAgent` issues**, including both types above. The `FeedbackPoller` queries by `label="PersonalAgent"`, which covers the union.

### How to give feedback

Apply an `AgentFeedback/*` label to the issue in Linear. Do not leave a comment — comments are human-readable but not yet machine-processed (Phase 3 work, FRE-183).

| Label | What happens |
|-------|-------------|
| `AgentFeedback/Approved` | Agent may proceed with this change if/when implemented |
| `AgentFeedback/Rejected` | Entry suppressed for `AGENT_FEEDBACK_SUPPRESSION_DAYS` days |
| `AgentFeedback/Deepen` | Agent generates a more detailed proposal (up to `AGENT_FEEDBACK_MAX_REEVALUATIONS` times) |
| `AgentFeedback/Too Vague` | Same as Deepen — triggers a more specific proposal |
| `AgentFeedback/Duplicate` | Entry suppressed; poller links it to the canonical issue |
| `AgentFeedback/Defer` | Entry suppressed for `AGENT_FEEDBACK_DEFER_REVISIT_DAYS` days |

### Triage tips

- Use Linear's **filter by label** to see all pending agent proposals: filter `label = PersonalAgent` + `state = Needs Approval`.
- Bulk-label from mobile: Linear's iOS app supports label editing on individual issues.
- If an issue is clearly wrong (eval artifact, fictional project), apply `AgentFeedback/Rejected` — or cancel it directly.

## Normative design

- Spec: [SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md](../specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md)
- ADR: [ADR-0040-linear-async-feedback-channel.md](../architecture_decisions/ADR-0040-linear-async-feedback-channel.md)
