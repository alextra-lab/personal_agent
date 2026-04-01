# Self-Improvement Feedback Loop Specification

**Version**: 0.1  
**Date**: 2026-04-01  
**Status**: Draft  
**ADR**: ADR-0040 (Linear as Async Feedback Channel)  
**Extends**: ADR-0030 (Captain's Log Dedup & Self-Improvement Pipeline)

---

## 1. Purpose

Wire the existing Captain's Log promotion pipeline to Linear (replacing dry-run mode) and build a feedback loop where the project owner reviews agent proposals via Linear labels and the agent responds, learns, and improves.

**Scope boundary**: This spec covers analysis, proposal creation, human feedback, and agent response. It explicitly does **not** cover autonomous implementation — the agent proposes and refines, the human decides and implements.

---

## 2. Current State

### What works

| Component | Location | Status |
|-----------|----------|--------|
| Captain's Log reflection | `captains_log/reflection.py` | Generates `CaptainLogEntry` with `ProposedChange` after each task |
| Categorization + dedup | `captains_log/models.py`, `manager.py` | `ChangeCategory`, `ChangeScope`, fingerprint fields exist (ADR-0030) |
| Promotion pipeline | `captains_log/promotion.py` | `PromotionPipeline` scans, filters, formats — but `create_issue_fn=None` (dry-run) |
| Scheduler integration | `brainstem/scheduler.py:441` | Calls `promotion_pipeline.run()` weekly |
| Insights engine | `insights/engine.py` | Weekly analysis, cost anomalies, delegation scaffold |
| Linear MCP | Docker MCP toolkit (`MCPGatewayAdapter`) | `save_issue`, `get_issue`, `list_issues`, `save_comment`, `list_comments`, `create_issue_label` |

### What's missing

1. `PromotionPipeline` has no `create_issue_fn` — promotions are logged but never sent to Linear
2. No feedback labels exist in Linear for the agent to read
3. No scheduler job reads Linear for human responses
4. No mechanism for the agent to re-analyze or refine a proposal based on feedback
5. No feedback metrics in the insights engine

---

## 3. Linear Budget Constraints

**Free tier**: 250 non-archived issues, 2 teams, 5,000 API requests/hour. Archived issues are preserved indefinitely and remain searchable — they just don't count toward the 250 limit.

**Current usage** (as of 2026-04-01):
- 156 non-archived issues (94 slots remaining)
- 112 are Done/Canceled (archivable → would free to 206 slots)
- 0 issues created by the promotion pipeline (all dry-run)

### The core tension

Archiving frees slots, but feedback history (labels applied, comments, re-evaluation threads) is valuable training data for the meta-learning layer. We can't treat archived issues as disposable — they contain the human signal that makes this loop useful.

### History preservation strategy

Before archiving any issue, the agent captures its full feedback metadata to a local file:

**Location**: `telemetry/feedback_history/<issue_identifier>.json` (gitignored)

**Schema**:

```python
class FeedbackRecord(BaseModel):
    """Preserved feedback history for an archived Linear issue."""

    issue_id: str
    issue_identifier: str
    title: str
    category: ChangeCategory | None
    scope: ChangeScope | None
    fingerprint: str | None
    feedback_label: str
    feedback_date: datetime
    comments: list[dict[str, str]]
    created_at: datetime
    seen_count: int
    time_to_feedback_hours: float | None
    original_description: str
```

This means the insights engine can analyze feedback patterns from local history even after the Linear issue is archived. The Linear archive remains available for reference, but the agent doesn't depend on it for analytics.

### Budget rules

- **Capture before archive**: Every terminal-state transition captures feedback metadata locally first
- **Archive on Rejected/Duplicate**: Free the slot immediately after capture
- **Approved stays active**: Represents validated work — archive only when moved to Done
- **Deferred stays active**: Until revisit date (default 90 days), then capture and archive
- **Monitor count**: Daily feedback poll checks non-archived count; pause promotion if > 200
- **Initial promotion cap**: 5 issues per pipeline run (down from default 20)
- **Upgrade signal**: If budget becomes a recurring constraint and the loop is proving valuable, upgrade to Basic ($10/mo, unlimited issues)

---

## 4. Label Protocol

### 4.1 Label taxonomy

Create a parent label group **AgentFeedback** in Linear with child labels:

```
AgentFeedback (group, color: #95A2B3)
├── Approved    (#0E7D1C, green)    — human: "valuable, track it"
├── Rejected    (#EB5757, red)      — human: "noise, suppress it"
├── Deepen      (#F2C94C, amber)    — human: "re-analyze with stronger model"
├── Too Vague   (#F2994A, orange)   — human: "be more specific"
├── Duplicate   (#95A2B3, gray)     — human: "already proposed"
└── Defer       (#6B7280, slate)    — human: "right idea, wrong time"
```

Agent-set response labels (not in AgentFeedback group):

```
Re-evaluated  (#2F80ED, blue)   — agent posted deeper analysis
Refined       (#9B51E0, purple) — agent posted more specific proposal
```

### 4.2 Label creation

Use `create_issue_label` MCP tool at setup time. The spec implementation should include a one-time setup function that creates these labels if they don't exist (idempotent check via `list_issue_labels` first).

```python
FEEDBACK_LABELS = {
    "AgentFeedback": {"color": "#95A2B3", "is_group": True},
    "Approved": {"color": "#0E7D1C", "parent": "AgentFeedback"},
    "Rejected": {"color": "#EB5757", "parent": "AgentFeedback"},
    "Deepen": {"color": "#F2C94C", "parent": "AgentFeedback"},
    "Too Vague": {"color": "#F2994A", "parent": "AgentFeedback"},
    "Duplicate": {"color": "#95A2B3", "parent": "AgentFeedback"},
    "Defer": {"color": "#6B7280", "parent": "AgentFeedback"},
}

RESPONSE_LABELS = {
    "Re-evaluated": {"color": "#2F80ED"},
    "Refined": {"color": "#9B51E0"},
}
```

---

## 5. Architecture

### 5.1 Data flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                         Brainstem Scheduler                          │
│                                                                      │
│  Weekly:                          Every cycle:                       │
│  ┌─────────────────────┐          ┌──────────────────────────┐      │
│  │ Promotion Pipeline   │          │ Feedback Polling Job      │      │
│  │ (scan CL → promote)  │          │ (read Linear → respond)   │      │
│  └──────────┬──────────┘          └──────────┬───────────────┘      │
│             │                                 │                      │
└─────────────┼─────────────────────────────────┼──────────────────────┘
              │                                 │
              ▼                                 ▼
┌──────────────────────┐          ┌──────────────────────────┐
│  Linear MCP           │          │  Linear MCP               │
│  save_issue()         │◄────────►│  list_issues()            │
│  (create proposal)    │          │  get_issue()              │
│                       │          │  save_comment()           │
│                       │          │  save_issue() (update)    │
└──────────┬───────────┘          └──────────┬───────────────┘
           │                                  │
           ▼                                  ▼
┌──────────────────────────────────────────────────────────────┐
│                          LINEAR                               │
│                                                               │
│  ┌─────────────────┐     Human reviews      ┌─────────────┐ │
│  │ Proposal Issue    │ ──────────────────────► │ Label added  │ │
│  │ (Needs Approval)  │   on phone/desktop     │ (feedback)   │ │
│  └─────────────────┘                         └─────────────┘ │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
┌──────────────────────────┐
│  Insights Engine          │
│  (meta-learning from      │
│   acceptance/rejection    │
│   patterns)               │
└──────────────────────────┘
```

### 5.2 Component responsibilities

| Component | File | Responsibility |
|-----------|------|----------------|
| `PromotionPipeline` | `captains_log/promotion.py` | Scan CL entries, create Linear issues |
| `FeedbackPoller` | `captains_log/feedback.py` (new) | Poll Linear for label changes, dispatch handlers |
| `FeedbackHandler` | `captains_log/feedback.py` (new) | Per-label response logic (approve, reject, deepen, etc.) |
| `BrainstemScheduler` | `brainstem/scheduler.py` | Host both promotion and feedback polling jobs |
| `InsightsEngine` | `insights/engine.py` | Consume feedback metrics, detect meta-patterns |
| `LinearClient` | `captains_log/linear_client.py` (new) | Thin wrapper around `MCPGatewayAdapter` Linear tools for type safety |

---

## 6. Implementation Details

### 6.1 Wiring the promotion pipeline

**New file**: `src/personal_agent/captains_log/linear_client.py` — Thin typed wrapper around the `MCPGatewayAdapter` for Linear-specific calls. Uses the Docker MCP toolkit's Linear tools (no Cursor dependency).

```python
class LinearClient:
    """Type-safe wrapper around MCPGatewayAdapter for Linear tools.

    Args:
        gateway: MCPGatewayAdapter instance (reuses the agent's existing gateway).
    """

    def __init__(self, gateway: MCPGatewayAdapter) -> None: ...

    async def create_issue(self, title: str, team: str, description: str,
                           priority: int, labels: list[str], state: str,
                           project: str) -> str | None:
        """Create a Linear issue. Returns identifier (e.g. "FF-123") or None."""
        ...

    async def get_issue(self, issue_id: str) -> dict[str, Any]: ...
    async def list_issues(self, **filters: Any) -> list[dict[str, Any]]: ...
    async def update_issue(self, issue_id: str, **fields: Any) -> None: ...
    async def add_comment(self, issue_id: str, body: str) -> None: ...
    async def list_comments(self, issue_id: str) -> list[dict[str, Any]]: ...
    async def create_label(self, name: str, color: str, **kwargs: Any) -> None: ...
```

**Configuration prerequisite**: The Linear MCP in the Docker toolkit must be authorized with a Linear API key. ~~This is a one-time setup in the Docker MCP config.~~ **Done** — already authorized.

**Change**: In `brainstem/scheduler.py`, create a `LinearClient` from the existing `MCPGatewayAdapter` and inject its `create_issue` method as `create_issue_fn` into `PromotionPipeline`.

Update `PromotionPipeline` defaults in `promotion.py`:
- Change default `state` from `"Backlog"` to `"Needs Approval"`
- Add `"PersonalAgent"` to default labels
- Reduce `max_existing_linear_issues` to 5 for initial rollout

### 6.2 Feedback poller

**New file**: `src/personal_agent/captains_log/feedback.py`

```python
@dataclass
class FeedbackEvent:
    """A detected label change on a Linear issue."""

    issue_id: str
    issue_identifier: str
    label: str
    issue_title: str
    updated_at: str


class FeedbackPoller:
    """Polls Linear for feedback labels on agent-created proposals.

    Args:
        linear_client: Wrapper for Linear MCP calls.
        state_path: Path to persist last-processed timestamps.
    """

    async def check_for_feedback(self) -> list[FeedbackEvent]:
        """Query Linear for recently-updated PersonalAgent issues.

        Returns:
            Feedback events for issues with new AgentFeedback labels.
        """
        ...

    async def process_feedback(self, events: list[FeedbackEvent]) -> None:
        """Dispatch each feedback event to the appropriate handler.

        Args:
            events: Detected feedback events to process.
        """
        ...
```

**Feedback label detection**: The poller calls `list_issues(team="FrenchForest", label="PersonalAgent", updatedAt=<since_last_check>)`. For each returned issue, it checks whether any AgentFeedback label is present that hasn't been processed yet.

**State persistence**: Store `{issue_id: last_processed_label}` in a JSON file at `telemetry/feedback_poller_state.json` (gitignored). This prevents re-processing on restart.

### 6.3 Feedback handlers

Each handler is a function that receives a `FeedbackEvent` and the `LinearClient`:

#### Approved

```python
async def handle_approved(event: FeedbackEvent, client: LinearClient) -> None:
    """Move issue to Approved state. Record positive signal."""
    await client.update_issue(event.issue_id, state="Approved")
    # Record in insights engine: positive feedback for this category
    ...
```

#### Rejected

```python
async def handle_rejected(event: FeedbackEvent, client: LinearClient) -> None:
    """Archive issue. Suppress similar proposals for configured duration."""
    issue = await client.get_issue(event.issue_id)

    # 1. Extract fingerprint from issue description (embedded at creation time)
    fingerprint = _extract_fingerprint(issue)

    # 2. Write to suppression file (Guard 2 in §6.4)
    if fingerprint:
        _add_suppression(
            fingerprint=fingerprint,
            issue_id=event.issue_identifier,
            duration_days=settings.feedback_suppression_days,
        )

    # 3. Capture feedback metadata locally (§3 history preservation)
    _save_feedback_record(issue, feedback_label="Rejected")

    # 4. Move to Canceled and archive to free budget slot
    await client.update_issue(event.issue_id, state="Canceled")

    # 5. Record negative signal in insights engine
    ...
```

#### Deepen

```python
async def handle_deepen(event: FeedbackEvent, client: LinearClient) -> None:
    """Re-analyze with stronger model. Post richer analysis as comment."""
    # 1. Read the original proposal from the issue description
    issue = await client.get_issue(event.issue_id)

    # 2. Re-evaluate using insights_role (stronger model)
    #    - Pull more telemetry context
    #    - Generate a more detailed analysis
    deeper_analysis = await _reanalyze_proposal(issue, model_role="insights_role")

    # 3. Post as structured comment (see ADR-0040 §6 for format)
    await client.add_comment(event.issue_id, _format_deepened_comment(deeper_analysis))

    # 4. Update labels: remove Deepen, add Re-evaluated
    await client.update_issue(
        event.issue_id,
        labels=_replace_label(issue.labels, remove="Deepen", add="Re-evaluated"),
        state="Needs Approval",
    )
    ...
```

#### Too Vague

```python
async def handle_too_vague(event: FeedbackEvent, client: LinearClient) -> None:
    """Refine proposal with specific files/configs. Post as comment."""
    issue = await client.get_issue(event.issue_id)

    # Search codebase for relevant files based on proposal scope
    refined = await _refine_proposal(issue)

    await client.add_comment(event.issue_id, _format_refined_comment(refined))
    await client.update_issue(
        event.issue_id,
        labels=_replace_label(issue.labels, remove="Too Vague", add="Refined"),
        state="Needs Approval",
    )
    ...
```

#### Duplicate

```python
async def handle_duplicate(event: FeedbackEvent, client: LinearClient) -> None:
    """Find original, link, and close as duplicate."""
    issue = await client.get_issue(event.issue_id)

    # Search for related issues by title/description similarity
    original = await _find_original_issue(issue, client)

    if original:
        await client.update_issue(
            event.issue_id,
            state="Duplicate",
            duplicateOf=original.id,
        )
    else:
        await client.add_comment(
            event.issue_id,
            "Could not find the original issue automatically. "
            "Please link it manually or add more context.",
        )
    # Record dedup miss for fingerprint tuning
    ...
```

#### Defer

No immediate action. The issue stays in its current state. The poller records the deferral timestamp. A future enhancement could revisit deferred issues after a configurable period (default 90 days).

### 6.4 Proposal re-emergence prevention

**Problem**: The existing write-time fingerprint dedup (`manager._find_entry_by_fingerprint()`) only matches `AWAITING_APPROVAL` entries. Once an entry is promoted (status → `APPROVED`) or rejected and archived, a future reflection with the same fingerprint creates a new CL entry, accumulates `seen_count`, and gets promoted again — producing a duplicate Linear issue.

```
Reflection: "add retry logic" (fingerprint: abc123)
  → CL entry (AWAITING_APPROVAL) → merged 5x → promoted → APPROVED
  → Rejected in Linear → archived

Two weeks later: reflection "add retry logic" again
  → _find_entry_by_fingerprint() finds nothing (old entry is APPROVED)
  → New CL entry created → accumulates → promoted again
  → DUPLICATE Linear issue
```

**Solution**: Three complementary guards, each catching what the previous one misses.

#### Guard 1: Expand CL write-time dedup (cheapest, catches most)

Modify `_find_entry_by_fingerprint()` in `manager.py` to match **any** status, not just `AWAITING_APPROVAL`:

```python
def _find_entry_by_fingerprint(self, fingerprint: str) -> pathlib.Path | None:
    for json_file in sorted(self.log_dir.glob("CL-*.json"), reverse=True):
        try:
            data = _json.loads(json_file.read_text(encoding="utf-8"))
        except Exception:
            continue

        # ADR-0040: match ANY status, not just AWAITING_APPROVAL.
        # If the entry is APPROVED/promoted, we still want to merge
        # (increment seen_count) rather than create a new promotable entry.
        pc = data.get("proposed_change")
        if pc and pc.get("fingerprint") == fingerprint:
            return json_file

    return None
```

And update `_merge_into_existing()`: if the matched entry is `APPROVED` (already promoted), merge the observation (increment `seen_count`) but **do not** reset status back to `AWAITING_APPROVAL`. The entry stays promoted. This preserves the data (the agent keeps noticing this pattern) without triggering re-promotion.

#### Guard 2: Suppression file for rejected proposals

When a proposal is `Rejected`, the feedback handler writes to a suppression file:

**Location**: `telemetry/feedback_history/suppressed_fingerprints.json`

```python
{
    "abc123deadbeef01": {
        "suppressed_until": "2026-05-15T00:00:00Z",
        "reason": "Rejected via Linear feedback",
        "issue_id": "FF-234",
        "rejected_at": "2026-04-15T10:30:00Z"
    }
}
```

The CL manager's `save_entry()` checks this file **before** the fingerprint scan. If the fingerprint is suppressed and the suppression hasn't expired, the entry is silently dropped (or logged and discarded):

```python
# In save_entry(), before the fingerprint dedup scan:
if fingerprint and self._is_suppressed(fingerprint):
    log.info(
        "captains_log_proposal_suppressed",
        fingerprint=fingerprint,
        reason="rejected_via_feedback",
    )
    return None  # or return a sentinel path
```

Suppression duration is configurable (`feedback_suppression_days`, default 30). After expiry, the proposal can re-emerge — this is intentional, because the codebase may have changed enough that the proposal is now valid.

#### Guard 3: Promotion-time Linear search (last resort)

Before creating a Linear issue, the promotion pipeline queries Linear to check for an existing issue with the same fingerprint embedded in the description:

```python
async def _check_linear_duplicate(self, fingerprint: str) -> str | None:
    """Search Linear for an existing issue containing this fingerprint.

    Returns:
        Issue identifier if found, None otherwise.
    """
    results = await self._linear_client.list_issues(
        team="FrenchForest",
        label="Improvement",
        query=fingerprint,
    )
    return results[0]["id"] if results else None
```

The promotion pipeline includes the fingerprint in the Linear issue description (it's already in the CL entry data). If a match is found, the pipeline links the CL entry to the existing issue instead of creating a new one.

#### Summary of guards

| Guard | Where | Catches | Cost |
|-------|-------|---------|------|
| Expanded fingerprint match | `manager.save_entry()` | Re-emergence after promotion | Disk scan (existing, cheap) |
| Suppression file | `manager.save_entry()` | Re-emergence after rejection | JSON file read (very cheap) |
| Linear search | `promotion.run()` | Anything that slipped through guards 1-2 | 1 API call per promotion (negligible at daily rate) |

### 6.5 Re-evaluation loop guard

To prevent infinite feedback loops (Deepen → Re-evaluated → Deepen → Re-evaluated → ...), cap re-evaluations at **2 per issue**. Track the count in comments or a dedicated field. After 2 re-evaluations, the agent posts a comment: "Maximum re-evaluation depth reached. Please provide specific guidance in a comment or approve/reject."

### 6.5 Scheduler integration

Add a daily feedback check to `BrainstemScheduler._lifecycle_loop()`, running once per day (same pattern as the daily insights analysis):

```python
# Daily feedback polling (ADR-0040)
if (
    getattr(settings, "feedback_polling_enabled", True)
    and now.hour == self.feedback_polling_hour_utc
    and (
        self._last_feedback_date is None
        or self._last_feedback_date.date() != today
    )
):
    try:
        events = await self.feedback_poller.check_for_feedback()
        if events:
            await self.feedback_poller.process_feedback(events)
        self._last_feedback_date = now
        log.info(
            "feedback_polling_completed",
            events_count=len(events),
        )
    except Exception as poll_err:
        log.warning(
            "feedback_polling_failed",
            error=str(poll_err),
            exc_info=True,
        )
```

### 6.6 Issue budget monitoring

Add to the promotion pipeline's `run()` method:

```python
async def _check_issue_budget(self) -> bool:
    """Verify Linear issue count is within budget.

    Returns:
        True if promotion should proceed, False if budget is exhausted.
    """
    issues = await self._linear_client.list_issues(
        team="FrenchForest", includeArchived=False, limit=1
    )
    # The response includes total count or we count non-archived
    # If count > ISSUE_BUDGET_THRESHOLD (default 200), pause
    ...
```

### 6.7 Meta-learning integration

Add to `InsightsEngine`:

```python
async def analyze_feedback_patterns(self, days: int = 30) -> list[Insight]:
    """Analyze human feedback on agent proposals.

    Reads feedback events from the poller state file and correlates
    with proposal categories to detect systematic patterns.

    Args:
        days: Lookback window.

    Returns:
        Insights about proposal quality and feedback patterns.
    """
    # Metrics to compute:
    # - acceptance_rate_by_category: {category: approved/(approved+rejected)}
    # - deepen_frequency: how often proposals need re-analysis
    # - refinement_success_rate: after Too Vague → Refined, what % get Approved?
    # - time_to_review: median time between creation and first feedback label
    # - rejection_clusters: common themes in rejected proposals
    ...
```

---

## 7. Configuration

Add to `config/settings.py`:

```python
# Feedback loop (ADR-0040)
feedback_polling_enabled: bool = Field(
    default=True,
    description="Enable Linear feedback polling in scheduler",
)
feedback_polling_hour_utc: int = Field(
    default=7,
    ge=0,
    le=23,
    description="UTC hour for daily feedback polling (default 7 AM UTC)",
)
feedback_suppression_days: int = Field(
    default=30,
    ge=1,
    description="Days to suppress re-promotion of rejected proposals",
)
feedback_max_reevaluations: int = Field(
    default=2,
    ge=1,
    description="Max re-evaluations per issue (Deepen/Too Vague)",
)
feedback_defer_revisit_days: int = Field(
    default=90,
    ge=7,
    description="Days before a Deferred issue is captured and archived",
)
issue_budget_threshold: int = Field(
    default=200,
    ge=50,
    le=250,
    description="Pause promotion when non-archived issues exceed this count",
)
promotion_initial_cap: int = Field(
    default=5,
    ge=1,
    description="Max issues created per promotion pipeline run",
)
```

---

## 8. Telemetry

### Events

| Event | When | Fields |
|-------|------|--------|
| `promotion_issue_created` | Issue sent to Linear | `issue_id`, `title`, `category`, `scope`, `seen_count` |
| `feedback_polling_check` | Each poll cycle | `issues_checked`, `events_found` |
| `feedback_event_processed` | Handler completes | `issue_id`, `label`, `handler`, `success`, `duration_ms` |
| `feedback_deepen_completed` | Deepen re-analysis done | `issue_id`, `model_used`, `confidence_delta` |
| `feedback_refine_completed` | Too Vague refinement done | `issue_id`, `files_identified`, `specificity_score` |
| `feedback_rejection_recorded` | Rejection suppression set | `issue_id`, `fingerprint`, `suppression_until` |
| `feedback_history_captured` | Metadata saved before archive | `issue_id`, `feedback_label`, `category` |
| `feedback_issue_archived` | Issue archived to free budget | `issue_id`, `slots_remaining` |
| `issue_budget_warning` | Non-archived count > threshold | `current_count`, `threshold` |
| `issue_budget_promotion_paused` | Promotion skipped due to budget | `current_count`, `threshold` |

### Dashboards (Kibana)

- **Proposal quality**: acceptance rate over time, by category
- **Feedback latency**: time-to-first-feedback distribution
- **Model suitability signal**: Deepen frequency over time (declining = model is improving or adequate; rising = model needs upgrading)
- **Budget health**: non-archived issue count trend

---

## 9. Error Handling

| Failure | Behavior |
|---------|----------|
| Linear MCP unavailable | Skip polling/promotion cycle, log warning, retry next cycle |
| Issue creation fails | Log error, do not mark CL entry as promoted, retry next run |
| Feedback handler fails | Log error for specific event, continue processing remaining events |
| Re-analysis LLM call fails | Post comment explaining failure, keep original label, retry next cycle |
| State file corrupted | Reset to empty state (may re-process some events, handlers are idempotent) |

---

## 10. Testing Strategy

### Unit tests

- `test_feedback.py`: Label detection, handler dispatch, state persistence, loop guard
- `test_promotion_wired.py`: `create_issue_fn` integration (mock MCP response)
- `test_budget_monitor.py`: Threshold logic, pause behavior
- `test_feedback_metrics.py`: Acceptance rate calculation, deepen frequency

### Integration tests

- Promotion → Linear: create real issue (or mock MCP), verify state and labels
- Deepen flow: create issue → add Deepen label → verify comment posted and labels updated
- Budget pause: set threshold to 0 → verify promotion skipped with correct log event
- Rejection suppression: reject → verify same fingerprint blocked for configured days

### Manual validation

- Create 3–5 real proposals via the promotion pipeline
- Apply each feedback label from the Linear mobile app
- Verify agent responses within one scheduler cycle
- Review comment quality (Deepen, Too Vague responses)

---

## 11. File Changes Summary

| File | Change |
|------|--------|
| `src/personal_agent/captains_log/feedback.py` | **New**: `FeedbackPoller`, `FeedbackHandler`, handlers, `FeedbackRecord` |
| `src/personal_agent/captains_log/linear_client.py` | **New**: Thin wrapper around `MCPGatewayAdapter` for Linear tools |
| `src/personal_agent/captains_log/promotion.py` | Modify: accept MCP-backed `create_issue_fn`, default state → `Needs Approval`, add Linear-side dedup check |
| `src/personal_agent/captains_log/manager.py` | Modify: expand `_find_entry_by_fingerprint()` to match any status; add suppression file check |
| `telemetry/feedback_history/suppressed_fingerprints.json` | **New** (gitignored): rejected proposal suppression registry |
| `src/personal_agent/brainstem/scheduler.py` | Modify: inject `create_issue_fn`, add daily feedback polling job |
| `src/personal_agent/insights/engine.py` | Modify: add `analyze_feedback_patterns()` using local history |
| `src/personal_agent/config/settings.py` | Modify: add feedback loop settings |
| `telemetry/feedback_history/` | **New directory** (gitignored): preserved feedback records |
| `tests/personal_agent/captains_log/test_feedback.py` | **New**: unit + integration tests |
| `tests/personal_agent/captains_log/test_promotion_wired.py` | **New**: wired promotion tests |

---

## 12. Implementation Sequence

### Phase 1: Wire and ship

| Step | Task | Effort | Test |
|------|------|--------|------|
| 0 | ~~Configure Linear MCP authorization in Docker MCP toolkit~~ | — | **Done** — already authorized |
| 1 | Create `linear_client.py` wrapper around `MCPGatewayAdapter` | S | Unit: mock MCP responses |
| 2 | Wire `create_issue_fn` into scheduler/promotion | S | Unit: verify issue creation call |
| 3 | Update `PromotionPipeline` defaults (state, labels, cap) | S | Unit: verify defaults |
| 4 | Create feedback labels in Linear (one-time setup) | S | Manual: verify in Linear UI |
| 5 | Add issue budget monitoring | S | Unit: threshold logic |
| 5a | Expand `_find_entry_by_fingerprint()` to match any status (not just AWAITING_APPROVAL) | S | Unit: verify merge into APPROVED entry doesn't re-promote |
| 5b | Add suppression file check to `save_entry()` | S | Unit: suppressed fingerprint → entry silently dropped |
| 5c | Add Linear-side dedup check in promotion pipeline | S | Unit: mock duplicate found → skip creation |

### Phase 2: Feedback loop

| Step | Task | Effort | Test |
|------|------|--------|------|
| 6 | Build `FeedbackPoller` with state persistence | M | Unit: detection, idempotency |
| 7 | Implement `Approved` handler | S | Unit + integration |
| 8 | Implement `Rejected` handler (archive + suppress) | S | Unit + integration |
| 9 | Implement `Deepen` handler (model escalation + comment) | M | Unit + integration |
| 10 | Implement `Too Vague` handler (refinement + comment) | M | Unit + integration |
| 11 | Implement `Duplicate` handler | S | Unit + integration |
| 12 | Add feedback polling to scheduler | S | Integration |

### Phase 3: Meta-learning

| Step | Task | Effort | Test |
|------|------|--------|------|
| 13 | Add `analyze_feedback_patterns()` to insights engine | M | Unit: metric calculation |
| 14 | Feed feedback metrics into weekly analysis | S | Integration |
| 15 | Add Kibana dashboard configs | S | Manual verification |

### Phase 4: Evaluate (4+ weeks)

| Step | Task | Effort | Test |
|------|------|--------|------|
| 16 | Run pipeline with real proposals | — | Manual: review quality |
| 17 | Measure acceptance rate, deepen frequency | — | Dashboard review |
| 18 | Decide: tune model tier, adjust criteria | — | ADR amendment |

---

## 13. Open Questions

1. ~~**MCP invocation from scheduler**~~: **Resolved.** Linear MCP is available in the Docker MCP toolkit. The agent's `MCPGatewayAdapter` (`src/personal_agent/mcp/gateway.py`) can invoke Linear tools directly from background processes without Cursor. The `linear_client.py` wrapper calls Linear tools through the gateway adapter. Authorization config for the Linear MCP in the Docker toolkit needs to be set up (API key in Docker MCP config).

2. **Model for Deepen re-analysis**: Use `insights_role` (currently same tier as `captains_log_role`), or explicitly escalate to a higher tier? The spec assumes escalation but the exact model should be configurable.

3. **Feedback on non-agent issues**: Should the poller ignore issues that weren't created by the promotion pipeline? Yes — filter by `Improvement` + `PersonalAgent` labels to avoid processing manually-created issues.

4. **Comment parsing**: Phase 3+ could parse freeform comments from the owner as additional feedback. Deferred — labels are sufficient for v1.

5. ~~**Linear API from background process**~~: **Resolved.** Linear MCP runs in the Docker MCP toolkit, authenticated via its own config (not Cursor's session). The agent service can call Linear tools independently.

---

## 14. Success Criteria

After 4 weeks of operation:

- [ ] At least 10 proposals promoted to Linear via the pipeline
- [ ] Project owner has applied feedback labels to at least 5 proposals
- [ ] Acceptance rate is tracked and visible in insights output
- [ ] At least 1 Deepen cycle has produced a noticeably better re-analysis
- [ ] No issue budget warnings (staying under 200 non-archived)
- [ ] The project owner can articulate whether the proposals are useful

---

## 15. Links

- ADR-0040: `docs/architecture_decisions/ADR-0040-linear-async-feedback-channel.md`
- ADR-0030: `docs/architecture_decisions/ADR-0030-captains-log-dedup-and-self-improvement-pipeline.md`
- Promotion pipeline: `src/personal_agent/captains_log/promotion.py`
- Scheduler: `src/personal_agent/brainstem/scheduler.py`
- Insights engine: `src/personal_agent/insights/engine.py`
- Captain's Log models: `src/personal_agent/captains_log/models.py`
- Cognitive Architecture Redesign v2 §7: `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`
