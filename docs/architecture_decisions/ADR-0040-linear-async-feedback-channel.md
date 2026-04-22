# ADR-0040: Linear as Async Feedback Channel for Self-Improvement

**Status**: Accepted (Phases 1–2 implemented April 2026; Phase 3 meta-learning pending)  
**Date**: 2026-04-01  
**Deciders**: Project owner  
**Extends**: ADR-0030 (Captain's Log Dedup & Self-Improvement Pipeline)  
**Related spec**: `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md`

---

## Context

ADR-0030 built the first half of the self-improvement pipeline: Captain's Log entries are categorized, deduplicated, and — when they meet promotion criteria — pushed to Linear as backlog issues. The pipeline code exists (`captains_log/promotion.py`) but runs in **dry-run mode** because `PromotionPipeline` is instantiated without a `create_issue_fn` (scheduler.py line 89).

The spec's §7.2 ("Enhanced Loop") envisions fully autonomous self-implementation: the agent reads Approved Linear issues, composes a `DelegationPackage`, delegates to Claude Code, captures the outcome, and feeds it back as a `TaskCapture`. That vision has multiple unmet prerequisites:

1. **Unknown proposal quality**: No evaluation has assessed whether Captain's Log proposals are actionable, correct, or useful. The insights engine runs but nobody has inspected its output systematically.
2. **Unknown model suitability**: `captains_log_role` is currently `gpt-5.4-nano`. Whether that tier produces good reflections is untested.
3. **No external agent delegation**: `DelegationPackage` types exist but no code invokes an external agent. Stage C delegation (programmatic invocation) is unbuilt.
4. **No feedback path**: Issues go into Linear but the agent never reads them back. There is no return channel for human judgment to flow into the system.

The critical missing piece is not autonomy — it is **feedback**. The agent generates proposals but has no signal about whether they are valuable.

### The opportunity

Linear is already the project's task management tool, accessible from any device (mobile, desktop, web). It provides:

- **Labels**: structured metadata the agent can read programmatically
- **States**: a workflow engine (Backlog → Todo → In Progress → Done / Canceled)
- **Comments**: freeform Markdown the agent can read and write
- **Audit trail**: every state/label change is timestamped
- **Direct GraphQL API**: `LinearClient` calls `https://api.linear.app/graphql` directly via httpx and a Personal Access Token (`AGENT_LINEAR_API_KEY`). This replaced an earlier MCP-gateway wrapper (FRE-243) which was incompatible with VPS deployments lacking Docker Desktop's DCR OAuth socket. Filter queries that pass a team UUID must declare `$teamId: ID!` (not `String!`) — the Linear schema's `IDComparator.eq` field expects `ID` type (FRE-255).

Instead of building a custom approval UI, we can define a **structured feedback protocol** over Linear's existing primitives. The project owner triages proposals from their phone; the agent reads the feedback and responds.

---

## Decision

Use Linear as a **bidirectional async feedback channel** between the project owner and the agent's self-improvement pipeline. Define a label-based protocol where each label triggers a specific agent behavior. The agent polls Linear periodically (via the brainstem scheduler) for feedback on its proposals and acts accordingly.

### 1. Wire the promotion pipeline to Linear

Inject the Linear MCP's `save_issue` tool as `create_issue_fn` in the `PromotionPipeline`, replacing the current dry-run default. Promoted issues use the `PersonalAgent` and `Improvement` labels and are created in state `Needs Approval`.

### 2. Feedback label protocol

Create a parent label group **"AgentFeedback"** with the following child labels. Each label is an instruction the agent can interpret without a synchronous chat session.

| Label | Color | Human meaning | Agent behavior |
|-------|-------|---------------|----------------|
| **Approved** | `#0E7D1C` (green) | "This is valuable, track it" | Move to `Approved` state. Record positive signal in insights engine. Issue stays in backlog for future manual or automated implementation. |
| **Rejected** | `#EB5757` (red) | "This is noise" | Move to `Canceled` state (frees issue budget). Record negative signal: suppress proposals with same fingerprint for 30 days. Feed rejection pattern into insights engine. |
| **Deepen** | `#F2C94C` (amber) | "Interesting but shallow — re-analyze" | Agent re-evaluates the proposal using a stronger model (escalate from `captains_log_role` to `insights_role` or higher). Posts the richer analysis as a comment on the same issue. Removes `Deepen` label, adds `Re-evaluated` label. Issue returns to `Needs Approval` for re-review. |
| **Too Vague** | `#F2994A` (orange) | "I can't act on this — be specific" | Agent makes the proposal more concrete: identifies specific files, config values, expected outcomes. Posts the refined proposal as a comment. Removes `Too Vague` label, adds `Refined` label. Issue returns to `Needs Approval`. |
| **Duplicate** | `#95A2B3` (gray) | "You already said this" | Agent searches for the original issue, links them via `relatedTo`, and moves this issue to `Duplicate` state. Logs a dedup miss for fingerprint tuning. |
| **Defer** | `#6B7280` (slate) | "Right idea, wrong time" | No immediate action. Issue stays in `Backlog`. Agent does not suppress or re-propose. Revisit in 90 days (configurable). |

**Response labels** (set by the agent, read-only for the human):

| Label | Meaning |
|-------|---------|
| **Re-evaluated** | Agent has posted a deeper analysis after `Deepen` feedback |
| **Refined** | Agent has posted a more specific proposal after `Too Vague` feedback |

### 3. Feedback polling job

Add a new scheduled job to the brainstem scheduler: `_check_linear_feedback()`.

- **Frequency**: Once daily (configurable, default same hour as the daily insights run). This is not latency-sensitive — the project owner triages proposals asynchronously and the agent responds within 24 hours.
- **Mechanism**: `list_issues(team="FrenchForest", label="PersonalAgent", updatedAt="-P1D")` to find recently-updated PersonalAgent issues. For each issue, check if a feedback label was added since the agent's last check.
- **Idempotency**: Track processed feedback events by storing `{issue_id: last_processed_label_timestamp}` to avoid re-processing.
- **Rate budget**: ~2–4 API calls per check (1 `list_issues` + 1–3 `get_issue` for changed items). At once-daily polling, this is negligible against the 5,000/hour limit.

### 4. Meta-learning from feedback

Every feedback label applied by the human is itself a data point. The insights engine gains a new data source:

- **Acceptance rate by category**: If 80% of `OBSERVABILITY` proposals are `Rejected`, down-weight that category or improve the reflection prompt for it.
- **Deepen frequency as model signal**: High `Deepen` rate suggests `captains_log_role` model tier is too weak for the reflection task. This directly answers the open question about model suitability.
- **Rejection pattern analysis**: Cluster rejected proposals to identify systematic blind spots.
- **Time-to-review**: How long proposals sit before feedback. Long delays may indicate the protocol is too noisy.
- **Refinement success rate**: After `Too Vague` → `Refined`, does the proposal get `Approved` or `Rejected`? Measures the agent's ability to self-correct.

These metrics feed into the weekly insights analysis and the Captain's Log proposals themselves, creating a second-order learning loop: the agent proposes improvements to its own proposal process.

### 5. Linear free tier budget management

The free plan allows **250 non-archived issues**. Current usage: **156 issues** (112 of which are Done/Canceled and archivable). The 250 limit counts only non-archived issues — archived issues are preserved indefinitely and remain searchable in Linear.

**The core tension**: Archiving frees budget slots, but the feedback history on proposals (labels applied, comments, re-evaluations) is valuable data. We need to preserve that history without burning slots.

#### History preservation strategy

Before archiving any issue, the agent **captures feedback metadata** to a local JSON file (`telemetry/feedback_history/`):

```python
{
    "issue_id": "FF-234",
    "title": "[observability] Add structured logging to delegation path",
    "category": "observability",
    "scope": "orchestrator",
    "fingerprint": "abc123...",
    "feedback_label": "Rejected",
    "feedback_date": "2026-04-15T10:30:00Z",
    "comments": [...],
    "created_at": "2026-04-08T09:00:00Z",
    "seen_count": 5,
    "time_to_feedback_hours": 168
}
```

This means the insights engine can analyze feedback patterns from the local history even after the Linear issue is archived. The Linear archive remains available for reference if needed, but the agent doesn't depend on it.

#### Budget protection measures

- **Archive after capture**: When the agent processes a terminal feedback label (`Rejected`, `Duplicate`), it captures the metadata locally, then archives the Linear issue to free the slot.
- **Approved issues stay active**: `Approved` issues remain non-archived — they represent validated work items the project owner may act on. Archive only after the owner moves them to `Done`.
- **Deferred issues stay active**: `Defer` issues remain non-archived until their revisit date (default 90 days), then are captured and archived.
- **Issue count monitoring**: The daily feedback polling job checks the non-archived issue count. If it exceeds a configurable threshold (default: 200), the agent pauses promotion and logs a warning.
- **Promotion cap**: `PromotionCriteria.max_existing_linear_issues` (default 20 per pipeline run) already limits creation rate. Reduce to 5 for initial rollout.
- **Upgrade signal**: If the budget becomes a recurring constraint and the feedback loop is proving valuable, that's the signal to upgrade to Basic ($10/mo, unlimited issues). The spec tracks this as a success metric.

### 6. Agent comments as structured communication

When the agent responds to feedback (Deepen, Too Vague), it posts a Markdown comment on the issue. Comments follow a consistent format:

```markdown
## Agent Re-evaluation

**Trigger**: Deepen label applied on YYYY-MM-DD
**Model used**: [model name and tier]

### Updated Analysis
[Richer analysis here]

### Specific Files
- `src/personal_agent/path/to/file.py` (lines X–Y)

### Proposed Change (Revised)
- **What**: [more specific]
- **Why**: [with evidence]
- **How**: [concrete steps]

### Confidence
[Higher/Lower] than original (X.XX → Y.YY)

---
*This comment was generated by the agent's feedback loop (ADR-0040).*
```

This gives the project owner a clear, reviewable response without needing to open a chat session.

---

## Alternatives Considered

### A. Custom web UI for proposal review

Build a dedicated web interface for reviewing and triaging agent proposals.

*Pros*: Purpose-built UX, richer interaction model, no dependency on third-party tool.  
*Cons*: Significant implementation effort. Another service to maintain. Not accessible everywhere without deployment. Reinvents what Linear already provides. Premature — we don't yet know if the proposals are worth reviewing at all.

### B. Chat-based approval (synchronous)

Require the project owner to review proposals during chat sessions (the current `linear-implement-gate` rule).

*Pros*: Zero new infrastructure. Direct conversation allows nuanced feedback.  
*Cons*: Only works when the owner is actively in a session. Proposals accumulate between sessions. No mobile triage. No structured feedback taxonomy — just freeform conversation. Doesn't scale.

### C. Email/notification-based review

Send proposals via email or push notification; parse replies for approval.

*Pros*: Truly asynchronous. Works on any device.  
*Cons*: Parsing unstructured email is fragile. No audit trail. No label taxonomy. Building an email integration is more work than using Linear MCP tools that already exist.

### D. Full autonomous implementation (spec §7.2)

Skip the feedback loop and go directly to autonomous self-implementation for approved issues.

*Pros*: Maximum autonomy. Closes the loop completely.  
*Cons*: Premature. No evidence that proposals are valuable. No external agent delegation infrastructure. No safety validation. Risk of the agent implementing bad suggestions. **This ADR is the prerequisite** — prove proposal quality through feedback before attempting autonomous action.

---

## Consequences

**Positive:**
- Closes the feedback gap: human judgment flows back into the self-improvement pipeline
- Uses existing infrastructure (Linear MCP, scheduler, promotion pipeline) — low implementation cost
- Works asynchronously from any device — the owner can triage proposals on mobile
- Structured feedback becomes training data: the agent learns what the human values
- Naturally answers open questions (model suitability, proposal quality) through real usage data
- Archive-on-reject keeps the free tier budget healthy
- Foundation for future autonomy: once feedback data shows high acceptance rates for a category, that category could be auto-approved

**Negative:**
- Adds dependency on Linear API availability for the feedback loop (mitigated: polling is best-effort, failures don't break the system)
- Feedback taxonomy adds cognitive overhead for the owner (mitigated: start with 4 labels, expand only if needed)
- Polling once daily is less responsive than webhooks (acceptable — this is not latency-sensitive; webhooks would require an HTTP endpoint)
- Risk of "notification fatigue" if too many proposals are promoted (mitigated: strict promotion criteria, configurable caps)

---

## Acceptance Criteria

- [ ] `PromotionPipeline` wired to Linear MCP `save_issue` (no longer dry-run)
- [ ] Promoted issues created with state `Needs Approval` and labels `PersonalAgent`, `Improvement`
- [ ] AgentFeedback label group with child labels created in Linear: `Approved`, `Rejected`, `Deepen`, `Too Vague`, `Duplicate`, `Defer`
- [ ] Response labels created: `Re-evaluated`, `Refined`
- [ ] Feedback polling job in brainstem scheduler reads label changes
- [ ] Agent responds to `Deepen` by re-analyzing with a stronger model and posting a comment
- [ ] Agent responds to `Too Vague` by refining the proposal and posting a comment
- [ ] Agent responds to `Rejected` by archiving issue and recording suppression
- [ ] Agent responds to `Duplicate` by linking to original and moving to Duplicate state
- [ ] Issue count monitoring pauses promotion when approaching 250 limit
- [ ] Feedback metrics (acceptance rate, deepen frequency, time-to-review) tracked in insights engine
- [ ] Unit tests for feedback label detection, response dispatching, and issue budget monitoring
- [ ] Integration test: create proposal → apply Deepen label → agent posts re-evaluation comment

---

## Implementation Phasing

### Phase 1: Wire and ship (1–2 sessions)

1. Inject `create_issue_fn` into `PromotionPipeline` via Linear MCP
2. Create the label taxonomy in Linear
3. Add feedback polling job to scheduler
4. Implement `Approved` and `Rejected` handlers (simplest path)
5. Add issue budget monitoring

### Phase 2: Enriched feedback (1–2 sessions)

6. Implement `Deepen` handler (model escalation + comment posting)
7. Implement `Too Vague` handler (refinement + comment posting)
8. Implement `Duplicate` handler (search + link + state change)

### Phase 3: Meta-learning (1 session)

9. Feed feedback signals into insights engine
10. Track acceptance rate, deepen frequency, rejection patterns
11. Surface meta-insights in weekly analysis ("your observability proposals are rarely approved")

### Phase 4: Evaluate and tune

12. Run for 4+ weeks with real proposals
13. Assess: Are proposals improving? Is rejection rate declining? Is Deepen frequency dropping?
14. Decide: adjust model tier, tune promotion criteria, consider auto-approval for high-acceptance categories

---

## Open Questions

1. **Comment parsing**: Should the agent interpret freeform comments from the owner (beyond labels)? Powerful but adds NLP complexity. Defer to Phase 3+.
2. **Webhook vs. polling**: Linear supports webhooks on the free tier. Webhooks would give real-time response but require an HTTP endpoint. At once-daily polling, latency is fine — revisit only if the project owner wants faster turnaround on Deepen/Too Vague responses.
3. **Multi-round feedback**: What if the owner applies `Deepen`, reviews the re-evaluation, and then applies `Too Vague`? The protocol should support chained feedback without infinite loops (cap at 2 re-evaluations per issue).
4. **Notification to owner**: Should the agent notify the owner (via `notify-reminder.sh`) when new proposals are promoted? Useful for timely triage but risks noise.

---

## Addendum: Event Bus Integration (April 2026)

ADR-0041 (Event Bus via Redis Streams) materially improved the internal wiring of this feedback channel. The original design assumed tightly-coupled inline processing; the implemented architecture uses durable event-driven decoupling.

### Changes from original design

| Aspect | Original design (this ADR) | Implemented (post ADR-0041) |
|--------|---------------------------|----------------------------|
| **Promotion triggering** | Weekly scheduler job | `consolidation.completed` event → `cg:promotion` consumer (near-real-time) |
| **Feedback side-effects** | Inline in handler code | Poller processes labels, publishes `feedback.received` → decoupled `cg:insights` and `cg:feedback` consumers |
| **Suppression updates** | Direct call in `handle_rejected` | Event-driven via `build_feedback_suppression_handler()` on `feedback.received` |
| **Insights signals** | Direct call to insights engine | Event-driven via `build_feedback_insights_handler()` on `feedback.received` |
| **Captain's Log reflection** | Direct call after promotion | `promotion.issue_created` event → `cg:captain-log` consumer |

### What remains poll-based

Linear polling (`FeedbackPoller.check_for_feedback()`) runs daily via the brainstem scheduler because Linear does not push to us. All *internal* processing downstream of the poll is event-driven.

### Webhook opportunity

Linear supports webhooks on the free tier. With the event bus consumer infrastructure now in place, adding a thin HTTP webhook receiver that publishes `FeedbackReceivedEvent` directly to `stream:feedback.received` would eliminate the 24-hour polling latency. This is architecturally trivial but deferred until the feedback loop proves valuable during Phase 4 evaluation.

### Implementation files (post Event Bus)

| File | Role |
|------|------|
| `events/models.py` | `FeedbackReceivedEvent`, `PromotionIssueCreatedEvent`, `ConsolidationCompletedEvent` |
| `events/pipeline_handlers.py` | Consumer builders: insights, promotion, captain-log reflection, feedback suppression |
| `service/app.py` | Wires `LinearClient` → scheduler + event bus consumers at startup |

---

## Links and References

- ADR-0030: Captain's Log Dedup & Self-Improvement Pipeline (predecessor)
- ADR-0041: Event Bus via Redis Streams (internal decoupling)
- ADR-0019: Development Tracking System (Linear integration patterns)
- `src/personal_agent/captains_log/promotion.py` — promotion pipeline
- `src/personal_agent/captains_log/feedback.py` — feedback poller + handlers
- `src/personal_agent/captains_log/linear_client.py` — typed Linear GraphQL client (direct httpx, no MCP dependency)
- `src/personal_agent/tools/linear.py` — native Tier-1 `create_linear_issue` / `find_linear_issues` tool (FRE-224)
- `src/personal_agent/mcp/linear_issue_args.py` — `save_issue` argument normalizer (handles team aliases and UUID team IDs)
- `src/personal_agent/captains_log/suppression.py` — fingerprint suppression
- `src/personal_agent/events/pipeline_handlers.py` — event bus consumer handlers
- `src/personal_agent/brainstem/scheduler.py` — scheduler (polling job host)
- `src/personal_agent/insights/engine.py` — insights engine (meta-learning integration)
- `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` §7 — self-improvement vision
- Linear GraphQL API: `https://api.linear.app/graphql` — authenticated via `AGENT_LINEAR_API_KEY` PAT
- Linear free tier: 250 issues (excluding archived), 5,000 API requests/hour, 3,000,000 complexity points/hour
