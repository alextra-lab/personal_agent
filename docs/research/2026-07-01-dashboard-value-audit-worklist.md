# Dashboard Value Audit — Worklist (2026-07-01)

**Project:** Telemetry Surface Audit (ADR-0090). **Skill:** `create-visualization`.
**Loop:** kibana session (Sonnet), self-paced, **PR-per-dashboard, master-gated.**

The plumbing triage (FRE-535) already retired dead-*event* panels. This is the second-order pass:
does each surviving dashboard let a human make a **correct decision** from **real data**, or does it
render and mislead (the FRE-593-v1 failure)? "Renders with data" ≠ "useful."

## How the loop uses this list

- Take the **next un-PR'd** dashboard below. Rebuild/redesign it via the `create-visualization` skill
  to its DoD. Open **one** PR. **Stop at PR — never merge, never deploy, never retire on your own.**
- **Per-item DoD:** renders (cache-busted) · accurate · a human can read the **stated decision** from
  **REAL data** (the four-gate scrutiny). **Step 0 = inspect the raw event, including its TIME FIELD —
  do NOT assume `@timestamp`** (see caveat below).
- **Retirement is not a loop action.** If a dashboard enables no decision even after a redesign, say so
  in the PR as a *retire-proposal with evidence*; master + owner decide. Never delete on a coarse signal.
- Output **DONE** when every dashboard has a PR. Use a **max-iterations** cap; on a stall, document the
  blocker and stop rather than thrash.

## Verified-data caveat (why this list exists in this form)

The initial data-existence probe used `@timestamp` and produced **false "empty" verdicts** for indices
that use a different time field — `agent-insights-*` and `agent-captains-reflections-*` use **`timestamp`**
(both are alive, producing *today*), and `agent-monitors-*` differ too. **Confirm each dashboard's real
time field in step 0.** The triage tells you *where to look*, never *what to delete*.

## Worklist (ordered: concrete known work first, then the value-scrutiny wave)

### A — Known concrete work
1. **prompt-cost-cache** [2p, Lens] — **FRE-406**: Lens is broken (missing `visualizationType`) *and* needs
   the value pass. Decision it must enable: *"per-callsite prompt cost, and is the static-prefix cache
   eroding?"*
2. **delegation_outcomes** [1p] — **redesign the query.** It filters `delegation_package_created` (rare —
   17 docs ever); the live signal is `delegation_pattern_analysis_*` (1,832 docs). Decision: *"what
   delegation patterns occur, and what are their outcomes?"*
3. **insights_engine** [3p] — **verify it renders** (time field = `timestamp`; alive, 465 docs/30d). Likely
   a data-view time-field mismatch. Decision: *"what cross-session insights exist, and are they actionable?"*
4. **reflection_insights** [4p] — **verify it renders** (`timestamp`; alive, 192 docs/30d). Decision: *"what
   self-improvement proposals exist, and their status?"*

### B — Value-scrutiny wave (live on `agent-logs-*`; confirm each tells a true, useful story)
5. **turn_session_artifact** [11p] — sprawling; likely **consolidate**. Decision: *"per turn/session: cost,
   artifacts produced, envelope integrity."*
6. **monitors_joinability_slm** [10p, ~59 docs] — 10 panels on little data; likely **over-built →
   consolidate**. Decision: *"is telemetry joinable / is the SLM healthy?"*
7. **cost_budget** [7p] — Decision: *"am I within budget, what drives spend, am I near a cap?"*
8. **llm_performance** [8p] — Decision: *"model-call health — latency, tokens, error rate, by model/role."*
9. **traversal_gate** [6p] — Decision: *"are gate decisions firing correctly; tool-loop-gate trip rate."*
10. **expansion_decomposition** [6p] — Decision: *"when does it expand/decompose; do sub-agents succeed?"*
11. **intent_classification** [4p] — Decision: *"intent distribution + confidence; are misclassifications visible?"*
12. **task_analytics** [3p] — Decision: *"entities/tasks created — volume, types."*
13. **extraction_retry_health** [3p] — Decision: *"extraction/consolidation health — retry/failure rates."*
14. **request_timing** [2p] — Decision: *"where does turn latency go (stage breakdown)?"*
15. **request_traces** [4p, ~13 docs] — low volume; Decision: *"trace one request end-to-end."* Assess whether
    the volume supports the view or it should fold into another.
16. **system_health** [4p] — already renders (master-verified); quick value confirm. Decision: *"system
    healthy — CPU/mem, error events, consolidation activity."*

**Exclude:** `context_occupancy` (done, FRE-593), `data_views` (shared index-patterns — infra, not a dashboard).

## Gate (master)

Every PR gets the real-data scrutiny (four gates, incl. **verified-against-real-data**). Owner makes the
"is it useful" call. Master merges + imports (standing Kibana-import class). Retire-proposals are decided
here, never by the loop.
