# FRE-1463 — Widen the sub-agent tool grant beyond `run_python`

Ticket: https://linear.app/frenchforest/issue/FRE-1463
Blocker: FRE-1461 (merged `a760994e`, deployed, `Done`)
Related: FRE-1388 (the principal) · FRE-1302 · FRE-1303 · FRE-1338 · FRE-1360 · FRE-1466 · FRE-1138

Revision 2, after codex plan-review. Three stated reasons were factually wrong or stale and are
replaced below. The review's diff is recorded in §7.

---

## 1. The decision

AC-1 forbids one decision for four tools. Each of the four gets its own answer and its own
recorded reason. Two are granted. Two are refused.

| Tool | Decision | Reason (recorded in `config/governance/tools.yaml`) |
|------|----------|------------------------------------------------------|
| `web_search` | **GRANT** | Its absence is measured to produce confabulation, not silence. On 2026-09-08 two research sub-agents were refused `web_search`, then wrote a ranked agency table and a pricing verification from parametric recall (FRE-1463 comment, session `36b4ac05`). Default egress is the self-hosted SearXNG instance (ADR-0034), so a call bills nothing. Only the opt-in `categories="exa"` reaches a paid vendor. |
| `search_memory` | **GRANT** | An internal read with no egress and no spend, and strictly narrower for a sub-agent than for the primary. The sub-agent builds its `TraceContext` with no `user_id` and no `authenticated` flag (`sub_agent.py:570`, `sub_agent.py:684`), so `MemoryService.query_claims` returns `[]` on its missing-identity guard (`memory/service.py:2963`) and the FRE-229 visibility filter reveals no group-visibility rows. What a sub-agent can read is entities, turn summaries and session context — the public residue. |
| `fetch_url` | **REFUSE** | Its destination is model-selected and arbitrary: the tool reaches any http(s) host the model names, which `web_search` cannot do. FRE-1360 still delivers that text to the model as user text with no tool-result channel, and `web_search` plus `fetch_url` is the complete search-then-fetch chain. Revisit when FRE-1360 lands. |
| `recall_personal_history` | **REFUSE** | It cannot work. The executor raises `ToolExecutionError("missing_user_id ...")` when `ctx.user_id` is absent (`tools/personal_history.py:115-121`), and the sub-agent's `TraceContext` never carries one. A grant today hands a sub-agent a tool whose only outcome is an error that reads "this is a bug; report it". Revisit when sub-agent identity threading lands. |

`run_python` keeps its grant, and its entry now carries the owner's 2026-09-04 reason in the same
per-tool form.

### Three premises this plan corrects

1. **`web_search` does not bill per call.** The ticket states "`web_search` in particular bills per
   call". `tools/web.py:249` queries the self-hosted SearXNG instance. Only `categories="exa"`
   reaches Exa, a managed vendor. AC-4 is answered against that fact, not against the premise.
2. **No search-spend ledger exists.** Nothing in `cost_gate/` or `telemetry/` records a per-search
   cost. The measurable quantity for AC-4 is the count of `web_search` calls in a turn, and the
   subset that used `categories="exa"`.
3. **FRE-1303 is already closed.** `_recall_personal_history_entitlement`
   (`grounding/source_registry.py:726`) classifies a result carrying assistant-authored fields as
   `AGENT_DERIVED`, so the self-citation-at-EXTERNAL loop no longer exists. That tool is refused on
   the missing-identity ground alone.

### Two things this ticket does not fix

- **A sub-agent's tool results never enter the per-turn source registry.**
  `_register_tool_source` is called from `orchestrator/executor.py:1562` only, and it needs an
  `ExecutionContext`; `sub_agent.py` has none and never registers. So the ADR-0138 citation
  contract does not reach any sub-agent tool result. This is already true of `run_python` and does
  not distinguish the four, so it is not a reason to refuse any of them. Follow-up ticket in §5.
- **A sub-agent's tool results are counted but not capped.** `tool_result_chars_absorbed` measures;
  nothing bounds it. Granting `web_search` moves more text into the sub-agent loop. That is
  FRE-1138's ungoverned within-turn growth, which the FRE-1463 comment already places out of scope.
  This plan adds no cap.

---

## 2. The shape change

AC-1 requires "the grant is stated per tool with its reason, not as a list", and requires a refused
tool to be an entry rather than an absence. A list of names cannot carry either. So
`sub_agent_tools` becomes a mapping from tool name to a decision record.

Before:

```yaml
sub_agent_tools:
  - "run_python"
```

After:

```yaml
sub_agent_tools:
  run_python:
    granted: true
    reason: "..."
  web_search:
    granted: true
    reason: "..."
  fetch_url:
    granted: false
    reason: "..."
```

A tool absent from the mapping stays refused, exactly as today. The mapping adds the ability to
state a refusal *and its reason*, which is what AC-1 asks for.

**The one bug this shape can introduce** is a caller that iterates the mapping and treats every key
as granted. The codex review audited every caller: exactly two production sites read the set as a
grant list — `sub_agent_tools.py:77` and `expansion_controller.py:81` — and both move to a shared
`granted_sub_agent_tool_names()` accessor in Steps 4 and 5. No other site touches the field.

---

## 3. Steps

Each step names its verification command.

### Step 1 — failing tests for the new shape

Files: `tests/personal_agent/governance/test_sub_agent_tools.py`,
`tests/test_config/test_governance_loader.py`.

Write, before any source change:

- AC-1: the shipped config carries an entry for each of `run_python`, `web_search`,
  `search_memory`, `fetch_url`, `recall_personal_history`; each entry has a non-empty `reason`;
  the granted flags are `True, True, True, False, False`.
- AC-1 (empty reason): a decision record with an empty or whitespace-only `reason` fails
  validation. A reason that can be blank is a list wearing a mapping's clothes.
- AC-3: `evaluate_sub_agent_tool_grant(["fetch_url"], Mode.NORMAL, config)` denies `fetch_url` and
  the `denial_reason` names the tool and carries the recorded reason.
- AC-3 (absent tool): a name with no entry at all is still denied and still named.
- AC-5: for each of `web_search` and `search_memory`, `Mode.ALERT` and `Mode.DEGRADED` deny it.
- The leak guard: a refused entry never appears in `granted_sub_agent_tool_names()` nor in the
  planner surface.

*Verify:* `make test-file FILE=tests/personal_agent/governance/test_sub_agent_tools.py` **and**
`make test-file FILE=tests/test_config/test_governance_loader.py` — the new cases fail, the old
ones pass.

### Step 2 — schema

File: `src/personal_agent/governance/models.py`.

Add a frozen `SubAgentToolDecision` model with `granted: bool` and `reason: str` constrained to a
non-empty, non-whitespace value. Change `GovernanceConfig.sub_agent_tools` to
`dict[str, SubAgentToolDecision]`, default `{}`. Add a method
`granted_sub_agent_tool_names() -> tuple[str, ...]` returning the granted keys in declaration
order, so no caller reimplements the filter.

*Verify:* `make mypy`.

### Step 3 — loader

File: `src/personal_agent/config/governance_loader.py:104`.

Change the default from `[]` to `{}`. Pydantic validates the mapping.

*Verify:* `make test-file FILE=tests/test_config/test_governance_loader.py`.

### Step 4 — grant evaluation

File: `src/personal_agent/governance/sub_agent_tools.py`.

- `evaluate_sub_agent_tool_grant` reads `config.granted_sub_agent_tool_names()` for the allowed set.
- The denial reason names each denied tool and appends the recorded reason when the tool has an
  explicit entry. A tool with no entry keeps today's wording.
- Update the module docstring: the grant set is now a per-tool decision record, and it names the
  2026-09-08 decisions.

*Verify:* `make test-file FILE=tests/personal_agent/governance/test_sub_agent_tools.py`.

### Step 5 — planner surface

File: `src/personal_agent/orchestrator/expansion_controller.py:81`.

`_current_sub_agent_tool_surface` returns `list(config.granted_sub_agent_tool_names())`.

*Verify:* `make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py`.

### Step 6 — the config itself

File: `config/governance/tools.yaml`.

Replace the list with the five entries and their reasons. Rewrite the header comment: the file
still records the 2026-09-04 owner decision, and now also records the 2026-09-08 per-tool
decisions.

*Verify:* `make test-file FILE=tests/test_config/test_governance_loader.py`.

### Step 7 — update the existing constructors

Files: `tests/personal_agent/orchestrator/test_sub_agent_approval.py:599`,
`tests/personal_agent/orchestrator/test_expansion_controller.py:991,1224`,
`tests/personal_agent/governance/test_sub_agent_tools.py:22-35`.

Each builds a `GovernanceConfig` with `sub_agent_tools=["run_python"]`. Move them to the mapping.

*Verify:* `make test`.

### Step 8 — docs

`grep -rn "sub_agent_tools" docs/` and update anything that states the list shape.

### Step 9 — gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

---

## 4. Acceptance criteria

| AC | What proves it | Where |
|----|----------------|-------|
| **AC-1** — per-tool grant with a reason | The shipped config has an entry per tool, each with a non-empty reason, and the four decisions differ. The test reads the real `config/governance/tools.yaml`, not a fixture. | Build time |
| **AC-2** — a granted tool reaches a sub-agent, observed at the call | Unit: the granted names reach the planner surface and pass the grant evaluation. Live: two seeded turns after deploy, then read each sub-agent capture's `tools_used`. | Unit at build time; **live after deploy** |
| **AC-3** — a refused tool is refused and the reason surfaced | Test: `fetch_url` and `recall_personal_history` are denied and the denial names the tool. The in-loop refusal already returns `"{tool_name} is not available to this sub-agent."` into the sub-agent's own message history (`sub_agent.py:598-601`), and `refused_tool_attempts` rides up to the primary's report (`sub_agent.py:351`). A test pins both. | Build time |
| **AC-4** — fan-out cost measured, not estimated | The turn's total `web_search` call count across all sub-agents, and how many used `categories="exa"` (the only billed path). Stated as a turn total. | **Live after deploy** |
| **AC-5** — ALERT/DEGRADED revocation still holds | Test per newly granted tool in both modes, at both the grant evaluation and the planner surface. | Build time |

### On AC-3's audience

The codex review read AC-3 as requiring the recorded reason to reach the primary's synthesis
output, and called the plan short. The AC's own failure clause settles it the other way: "*Fails
if* the request silently returns nothing — the sub-agent then reports an absence it cannot
distinguish from an empty result." The audience is the sub-agent. That surface exists today at
`sub_agent.py:598-601`. Threading reasons into synthesis is a separate change with no criterion
asking for it, so this plan pins the existing surface with tests rather than building a new one.

### AC-2 and AC-4 need a live turn against the deployed config

A build seat cannot fire one (owner directive: never fire a live-gateway turn without an explicit
OK), and the config is not live until master deploys. Both are handed over as a post-deploy runbook
with exact commands and expected output, written into the handoff comment. The runbook uses two
seeded prompts rather than one, because a single research turn does not guarantee the planner asks
for both newly granted tools:

- A research prompt for `web_search` (the 2026-09-08 Mallorca question reproduces the fan-out).
- A recall prompt for `search_memory` ("what have we discussed about X before").

---

## 5. Follow-up tickets to file

1. **A sub-agent's `TraceContext` carries no identity.** `sub_agent.py:570` and `sub_agent.py:684`
   build `TraceContext(trace_id=..., session_id=...)`, dropping `user_id` and `authenticated`.
   Today that fails safe, and it is the reason `recall_personal_history` is refused here. Threading
   identity widens what every sub-agent tool can read about the user, so it needs its own decision
   rather than a fold-in.
2. **A sub-agent's tool results never register a source.** `_register_tool_source`
   (`executor.py:1523`) is the only registration site and needs an `ExecutionContext` that
   `sub_agent.py` does not have. The ADR-0138 citation contract therefore does not reach any
   sub-agent tool result — including `run_python`'s today. Granting `web_search` puts retrieved
   external text on that unregistered path, which makes this worth its own ticket.

---

## 6. Risk and diff class

**Diff class: escalated.** Governance code in the turn path, and a grant widening. Flagged for the
owner's `/code-review ultra` before merge.

---

## 7. Codex plan-review disposition

| Finding | Disposition |
|---------|-------------|
| `fetch_url` "unbounded body" is false — the cap is 10,000 default / 50,000 max chars (`fetch.py:84,288`) | **Accepted.** Verified. Reason rewritten around the arbitrary model-selected destination. |
| `recall_personal_history`'s FRE-1303 clause is stale — closed by `_recall_personal_history_entitlement` (`source_registry.py:726`) | **Accepted.** Verified. Clause dropped; the missing-identity ground stands alone. |
| `search_memory`'s reason covers only the Claims sub-path; a turns-only result keeps `EXTERNAL` (`source_registry.py:565`) | **Accepted, and it goes further than the finding.** Sub-agent tool results never reach the source registry at all, so no entitlement rule applies on that path. Recorded in §1 and filed as follow-up 2. It does not distinguish the four tools, so it changes no decision. |
| `reason` has no non-empty constraint | **Accepted.** Step 2 constrains it; Step 1 tests it. |
| Refusal reasons stop at a log line and never reach synthesis | **Declined, with reason.** See §4, "On AC-3's audience". |
| Step 1 edits two test files but verifies one | **Accepted.** Both are named now. |
| The AC-2/AC-4 runbook has no concrete commands, and one turn cannot guarantee both tools are used | **Accepted.** Two seeded prompts; exact commands go in the handoff comment. |
| Caller audit found no missed site | **Confirmed.** Two production readers, both covered. |
| Nothing over-built | **Noted.** |
