# Plan: Fix Agent-Filed Linear Issue Feedback Loop + Eval Isolation

## Context

Agent-filed Linear issues have three problems:
1. **Eval sessions create real tickets** — the recovery harness (`scripts/eval/recovery_harness.py`) feeds synthetic prompts through the normal `/chat` API with `channel=CHAT`. The agent treats them as real conversations and files tickets about fictional projects (e.g., "Bulk User Import with Alembic and src/models/" — none of which exist).
2. **No dedup enforcement** — `create_linear_issue` says "use find_linear_issues first" but doesn't enforce it. Result: 4 Mermaid chart issues in 30 seconds.
3. **No user attribution** — all issues show as created by the PAT owner. The requesting user/session is invisible.

### Telemetry findings
- **FRE-357/358** (Bulk Import): Created during eval run on May 11. Synthetic prompts about a fictional FastAPI+Alembic project. **Not user-initiated.**
- **FRE-359-362** (4 security issues, May 11 22:03 UTC): Created within 1 second, no conversation agent session exists — likely a Claude Code session. **Not from conversation agent.**
- **FRE-382/383/384** (Neo4j hallucination + notes_search, May 24): User explicitly requested these during a conversation (messages 11, 15, 27 of session d8d223cb). **User-initiated.**
- **FRE-316/317/318** (Mermaid triplicates, May 4): Created in rapid succession from a regular session. **Dedup failure.**

---

## Part 0: Create Linear Tickets for Deferred Items

The feedback loop is the heart of this project. Create tickets for each deferred item so they are tracked and schedulable:

1. **Revive Captain's Log reflection pipeline** — Priority: Urgent. CL-*.json file writes stopped after April 27. This blocks the entire ADR-0040 feedback loop. Diagnose root cause and restore.
2. **Human approval gate for `create_linear_issue`** — Priority: High. Build the `requires_approval` infrastructure so issue creation requires user confirmation via PWA.
3. **Comment-reading for feedback (Phase 3)** — Priority: Medium. ADR-0040 Phase 3 meta-learning: agent interprets freeform comments, not just labels. Ties to FRE-183/184.
4. **Eval session isolation for all side-effects** — Priority: High. Extend eval isolation beyond `create_linear_issue` to cover memory writes, graph writes, and other side-effects.

Check for existing tickets first (`find_linear_issues` or `list_issues`) to avoid duplicates. All tickets: state `Needs Approval`, labels `PersonalAgent` + appropriate tier.

---

## Part 1: Immediate Ticket Triage

| Ticket | Title | Action | Reason |
|--------|-------|--------|--------|
| FRE-357 | Bulk user import (CSV) | **Cancel** | Eval artifact. Fictional project. |
| FRE-358 | Bulk user import (CSV) #2 | **Cancel** | Eval artifact. Duplicate of FRE-357. |
| FRE-382 | Security: unauthorized Neo4j write | **Cancel** | User confirmed no write occurred. Duplicate of FRE-383. |
| FRE-383 | Agent hallucinated Neo4j write | **Keep, downgrade to High, → Needs Approval** | Accurate framing. Root cause not yet identified. |
| FRE-384 | notes_search tool error | **Keep, → Needs Approval** | Bug not yet confirmed. |
| FRE-360 | Port isolation | **Keep, downgrade to Medium** | Cloudflare tunnel mitigates. |
| FRE-361 | ES unauthenticated | **Keep, downgrade to High** | Same Cloudflare context. |
| FRE-359 | No owner identity verification | **Keep as-is** | Valid. |
| FRE-362 | Docker network flat | **Keep as-is** | Valid. |

---

## Part 2: Critical Finding — Feedback Loop is Dormant

The designed feedback loop (ADR-0040) has **never run in production**:

| Component | Expected | Actual |
|-----------|----------|--------|
| Captain's Log reflection | Produces entries after each task | Last entry: April 27. Nothing in 30 days. |
| Brainstem scheduler | Runs consolidation cycle, triggers promotion | Zero scheduler events in ES. |
| Promotion pipeline | Promotes entries (seen_count >= 3, age >= 7d) to Linear | Never executed. |
| Feedback poller | Reads AgentFeedback labels daily | Never executed (no poller_state.json). |
| Feedback history | Captures archived issue metadata | Directory empty. |

**Root cause (confirmed):**
- The promotion pipeline (`promotion.py:199`) scans filesystem `CL-*.json` files
- Only 49 CL files exist, all from April 27 or earlier — **no new entries in 30 days**
- The `cg:promotion` consumer group processes every `consolidation.completed` event (1,106 of them) but always finds the same stale files with `seen_count=1` (below threshold of 3)
- The 1,165 `captain_log.entry_created` Redis events are from FRE-328 captures (different path: `captures/<date>/<uuid>.json`), not standard reflections
- Standard Captain's Log reflection stopped writing CL files after April 27
- Redis is healthy, consumer groups are active — the infrastructure works but the input pipeline is dry

**Impact:** The self-improvement loop (ADR-0040) has never produced a single promoted ticket. All 16 agent-filed tickets came from direct tool calls during conversations or eval runs.

**Recommended next step:** File a Linear ticket to diagnose why reflection stopped producing CL files after April 27 and revive the pipeline. This is a prerequisite for the feedback loop to function. The fixes below address the immediate agent-filed ticket quality issues.

---

## Part 3: Systemic Fixes (for agent-filed issues)

### Fix 1: Eval session isolation — refuse issue creation during evals

**Problem:** The eval harness sends prompts via `/chat` with `channel=CHAT`, making eval sessions indistinguishable from real ones. The agent then calls `create_linear_issue` about fictional projects.

**Fix:** Two changes:

**A. Mark eval sessions with a distinct channel.**

**File:** `scripts/eval/recovery_harness.py` — the `_chat()` function (line ~151) constructs request params. Add `"channel": "EVAL"` to the params dict. This flows through to `POST /chat` and into the sessions table.

**B. Refuse `create_linear_issue` in eval sessions.**

**File:** `src/personal_agent/tools/linear.py` — `create_linear_issue_executor()`

Before the GraphQL call, check the session channel via TraceContext. If channel is `EVAL` or `BENCHMARK`, refuse with:
```python
raise ToolExecutionError(
    "Issue creation is disabled in evaluation sessions. "
    "This avoids filing tickets about synthetic eval prompts."
)
```

The `TraceContext` needs to carry `channel` (or `session_id` to look it up). Check `telemetry/context.py` for what fields are available.

**Fallback:** If TraceContext doesn't carry channel info, add a config flag `linear_issue_creation_disabled: bool = False` that can be set via env var before eval runs: `AGENT_LINEAR_ISSUE_CREATION_DISABLED=true`.

### Fix 2: Mandatory dedup check before issue creation

**File:** `src/personal_agent/tools/linear.py` — `create_linear_issue_executor()`

After validation (line ~432), before the GraphQL mutation (line ~475):

1. Query Linear for non-archived issues with matching title: `containsIgnoreCase` on the title string
2. If any match found, refuse with `ToolExecutionError` citing the existing issue identifier and URL
3. Reuse the existing `_gql` helper — no need to call the full `find_linear_issues_executor`

```python
# Dedup check
dedup_data = await _gql(
    """query($filter: IssueFilter) {
      issues(filter: $filter, first: 5) {
        nodes { identifier title url }
      }
    }""",
    {"filter": {"title": {"containsIgnoreCase": title}, "team": {"name": {"eq": _TEAM_NAME}}}},
)
existing = (dedup_data.get("issues") or {}).get("nodes") or []
if existing:
    ids = ", ".join(n["identifier"] for n in existing)
    raise ToolExecutionError(
        f"Duplicate: similar issue(s) already exist: {ids}. "
        "Use find_linear_issues to review before creating."
    )
```

**Test:** `tests/test_tools/test_linear.py` — mock `_gql` response returning an existing issue, assert `ToolExecutionError`.

### Fix 3: Embed requesting user identity in issue description

**File:** `src/personal_agent/tools/linear.py` — `create_linear_issue_executor()`

The executor receives a `TraceContext`. Thread `session_id` through it to look up `user_id` → `display_name`/`email` from Postgres (or from a session cache if available in the orchestrator).

Prepend an immutable attribution block to the description:

```markdown
<!-- filed-by: user_id=1f7cc4bc display_name=Alex email=lextra@gmail.com session=e1728781 -->
**Filed by:** Alex (lextra@gmail.com) — session e1728781
```

The HTML comment is machine-readable for future processing. The visible line is human-readable. Both are immutable after creation.

**Files to modify:**
- `src/personal_agent/tools/linear.py` — add attribution prefix
- `src/personal_agent/telemetry/context.py` — verify `TraceContext` carries `session_id` and `user_id` (or add them)

**Fallback:** If user info unavailable, log a warning and set attribution to "unknown (session {session_id})".

### Fix 4: Strengthen tool description for LLM guidance

**File:** `src/personal_agent/tools/linear.py` — `create_linear_issue_tool.description` (lines 46-50)

Add to description:
- "You MUST call find_linear_issues with the proposed title before creating. If a matching issue exists, do not create a duplicate."
- "Only file issues about components in this project (src/personal_agent/). Do not reference files from other projects."
- "After creation, the owner reviews via AgentFeedback labels (Approved/Rejected/Deepen/Too Vague/Duplicate/Defer)."

### Fix 5: Document that AgentFeedback labels work on agent-filed issues

**Finding:** `FeedbackPoller.check_for_feedback()` (line 384 of `feedback.py`) queries by `label="PersonalAgent"`, which already covers agent-filed issues. The gap was purely a user onboarding issue.

**File:** `docs/guides/LINEAR_FEEDBACK_LOOP.md` — add section:
- AgentFeedback labels work on ALL PersonalAgent issues, including agent-filed
- To give feedback: apply a label, not a comment. Comments are human-readable but not yet machine-processed.

---

## Verification

1. **Eval isolation:** Run recovery harness with 1-2 prompts that would trigger issue creation → verify sessions have `channel=EVAL` and `create_linear_issue` is refused
2. **Dedup check:** Unit test: mock GraphQL returning existing issue → assert `ToolExecutionError`; mock empty response → assert creation proceeds
3. **User attribution:** Create a test issue via conversation → verify description contains `<!-- filed-by: ... -->`
4. **Existing tests:** `make test-file FILE=tests/test_tools/test_linear.py`
5. **Feedback loop e2e:** Apply `Rejected` label to an agent-filed issue → trigger `FeedbackPoller.check_for_feedback()` → verify handler fires

---

## Deferred Items (needed, not in this plan)

These are required for the full feedback loop to function but are separate work items:

| Item | Why deferred | Prerequisite for | Suggested ticket |
|------|-------------|------------------|-----------------|
| **Revive Captain's Log reflection** | Requires diagnosis of why CL-*.json writes stopped after April 27. Likely a code/config regression during a deploy. | Promotion pipeline, entire ADR-0040 feedback loop | **File immediately — this blocks the feedback loop** |
| **Human approval gate for `create_linear_issue`** | `requires_approval` infrastructure not yet built in the conversation agent | Preventing hallucinated/irrelevant issue creation at the tool level | File after PWA approval UI ships |
| **Comment-reading (NLP) for feedback** | ADR-0040 Open Question #1 explicitly defers to Phase 3+. Requires LLM call per issue which adds cost/latency. | Natural-language feedback loop (user comments → agent responds) | File as Phase 3 work (FRE-183) |
| **Content/path validation** | Complex NLP extraction from markdown descriptions. Partially mitigated by eval isolation + dedup + description guidance. | Preventing hallucinated file references in issue descriptions | Can be absorbed into the tool description fix (Fix 4) for now |
| **Eval session isolation for ALL side-effects** | This plan only blocks `create_linear_issue` in eval sessions. Other side-effects (memory writes, graph writes) may also need gating. | Full eval/prod isolation | File as follow-up to eval isolation |
