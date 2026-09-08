# FRE-1467 — Thread the turn's identity into a sub-agent's TraceContext

Ticket: [FRE-1467](https://linear.app/frenchforest/issue/FRE-1467) · Tier-1:Opus · Urgent · stream:build1
Related: FRE-1463 (the grant set this reopens) · FRE-1388 (the sub-agent principal) · FRE-343 · FRE-229 / FRE-673 (visibility) · ADR-0064 · ADR-0126

## 1 — The defect

`orchestrator/sub_agent.py` builds two `TraceContext` values for every expansion-
controller worker:

- line 570 — the sub-agent's own inference call.
- line 684 — every tool dispatch that sub-agent makes.

Both are `TraceContext(trace_id=trace_id, session_id=session_id)`. Neither carries
`user_id`, `authenticated` or `eval_mode`. The primary's own context
(`executor.py:3879`) carries all five fields.

The identity is not available at those lines: `run_sub_agent` never receives it.
`ExpansionController.execute` never receives it either. The chain has to be threaded
from `executor.py`, which holds `ctx.user_id` and `ctx.authenticated`.

## 2 — What the ticket asks

| AC | Demand | How this plan answers it |
|----|--------|--------------------------|
| AC-1 | Both constructions carry the turn's `user_id` and `authenticated`. Fails if only one is threaded. | Collapse the two constructions into **one** context, built once in `run_sub_agent` and passed into `_run_tool_loop`. One object cannot be half-threaded, so the failure mode the AC names stops being reachable. |
| AC-2 | `recall_personal_history` returns turns to a sub-agent in a **real turn**, not a unit test. | Not decidable from this seat: the observation needs the change deployed, and build never deploys. Delivered as a post-deploy runbook step for master, with the exact command and expected output. Stated as a gap, not claimed as met. |
| AC-3 | Name every sub-agent-granted tool whose behaviour changes, and what each newly returns. | §5 below, transcribed into the module docstring and into `config/governance/tools.yaml`. |
| AC-4 | Revisit the grant set against the new reads. | §6 below. `recall_personal_history` is granted; `search_memory`'s stale justification is rewritten. |

## 3 — Design decision: one context, not two

The ticket names two call sites. The straight reading is "add two kwargs twice". That
leaves the exact defect class the AC's failure clause describes — two independent
constructions that can drift apart.

Instead: `run_sub_agent` builds a single `TraceContext` and hands it to
`_run_tool_loop` as a parameter. `trace_id` and `session_id` stay as separate
parameters because roughly a dozen log calls in that function read them directly, and
replacing those is churn outside this ticket.

The context is a frozen dataclass and `span_id` is resolved live on access, so one
instance is safe to reuse across every round and every tool call.

## 4 — Change list

### 4.1 `src/personal_agent/orchestrator/sub_agent.py`

1. Import `TraceContext` at module level (it is imported inside `_run_tool_loop`
   today). `telemetry/trace.py` imports only `uuid` and `opentelemetry`, so there is
   no cycle.
2. `run_sub_agent` gains `user_id: UUID | None = None` and
   `authenticated: bool = False`. Defaults keep every existing caller and the 60-odd
   direct test calls working.
3. `run_sub_agent` builds:
   ```python
   trace_ctx = TraceContext(
       trace_id=trace_id,
       user_id=user_id,
       session_id=session_id,
       eval_mode=eval_mode,
       authenticated=authenticated,
   )
   ```
   and passes it to `_run_tool_loop`.
4. `_run_tool_loop` gains `trace_ctx: TraceContext` and uses it at both former
   construction sites. The function-local import is removed.
5. Module docstring gains the AC-3 read list.

**`eval_mode` is a fold-in, and it is stated.** It is already a `run_sub_agent`
parameter and it is on the primary's context. It changes nothing today — the only
tool that reads `ctx.eval_mode` is `create_linear_issue` (`tools/linear.py:436`),
which no sub-agent holds. It is included because this change is what makes it
load-bearing: before it, a sub-agent's memory read returned nothing, so substrate
routing during an eval run did not matter. After it, the read is real.

### 4.2 `src/personal_agent/orchestrator/expansion_controller.py`

1. `execute` gains `user_id: UUID | None = None`, `authenticated: bool = False`.
2. `execute` passes them to `_run_planner` and `_run_dispatch`.
3. `_run_dispatch` gains the two parameters and passes them to `run_sub_agent` and to
   `_maybe_redispatch_on_gap`.
4. `_maybe_redispatch_on_gap` gains the two parameters and passes them to its own
   `run_sub_agent` call. This is the second worker call in the dispatch phase; missing
   it produces exactly the AC-1 failure — a tool that works on the first dispatch and
   not on the replacement.
5. `_run_planner` threads identity into its own `TraceContext` (line 461). Fold-in,
   stated: the planner makes no tool calls, so nothing about its behaviour changes.
   It is included so that "which LLM call inside expansion carries the turn's
   identity" has one answer instead of two.

### 4.3 `src/personal_agent/orchestrator/executor.py`

The `controller.execute(...)` call at line 5036 gains
`user_id=ctx.user_id, authenticated=ctx.authenticated`.

### 4.4 `config/governance/tools.yaml`

Rewrite the `search_memory` reason and flip `recall_personal_history` — see §6.

## 5 — AC-3: the widened read, stated

Granted set after this change: `run_python`, `web_search`, `search_memory`,
`recall_personal_history`.

**`search_memory` — changes.** Four separate widenings, all in
`tools/memory_search.py`:

1. `query_claims` (`memory/service.py:2963`) returns `[]` while `user_id is None or
   not authenticated`. With identity it returns the user's current `:Claim` rows —
   personal facts — ranked by embedding similarity.
2. `query_claims_history` — the same guard, on the supersession chain. Reached only
   when the model passes `include_history=true`.
3. `query_stance_history` — gated on `authenticated` alone. Scoped to the harness
   owner's `Person {is_owner: true}` sentinel, not to the connecting `user_id`
   (`memory/service.py:3063`), so identity unlocks only its gate, not a per-user
   slice.
4. The FRE-229 visibility filter (`memory/service.py:205-210`) reads
   `(visibility IS NULL OR = 'public' OR (= 'group' AND $vis_authenticated) OR =
   'private:' + $vis_user_id)`. With no identity, `$vis_user_id` is `""`, so only
   null/public rows match. With identity, **`group` rows and this user's `private:`
   rows** join the entity-match and broad-recall results.

**`recall_personal_history` — changes.** It raises
`ToolExecutionError("missing_user_id …")` on every call today
(`tools/personal_history.py:115-121`). With identity it returns the user's own past
turns in the window: `turn_id`, timestamp, session id, `user_message` and
`assistant_response` (each capped at 400 characters — the same bound `search_memory`
applies to a matched turn), `summary`, discussed entities, and an optional
topic-match flag.

**`web_search` — no change.** It reads `ctx.trace_id` and no identity field.

**`run_python` — no change.** Same: `ctx.trace_id` only.

**Net.** The sub-agent's read stops being narrower than the primary's and becomes
**equal** to it. That is the honest description, and it is what AC-1 requires: AC-1
asks for the *same* values the primary carries.

## 6 — AC-4: the grant set, re-answered

FRE-1463 refused `recall_personal_history` on one ground: it cannot work. That ground
is gone. A refusal now needs a *different* ground, and there is not a coherent one.

**Grant `recall_personal_history`.** The read is scoped to the connecting user's own
turns and costs nothing. It applies the same 400-character caps to `user_message` and
`assistant_response` that `search_memory` applies to a matched turn, so it is not a
raw-transcript tool. What it adds over `search_memory` is **reach**: up to 50 turns
across up to 365 days, unranked, plus `assistant_response`.

Three arguments against, all recorded rather than dismissed.

1. **Disclosure.** The Neo4j query reaches no external host, but the returned rows
   enter the sub-agent's message stream, go to the configured sub-agent provider —
   which the executor's own comment says may be a cloud deployment
   (`executor.py:5002`) — and a sub-agent also holding `web_search` can place them in
   a model-authored query that SearXNG forwards to upstream engines.
2. **Injection, the other direction.** A sub-agent holding `web_search` absorbs
   untrusted web text while FRE-1360 is open, and could be steered to pull personal
   history into its summary.
3. **No deterministic intent gate.** The tool's own description says to use it only
   when the user explicitly refers to their personal history, and a sub-agent's task
   is planner-authored.

None of the three distinguishes this tool from what a sub-agent already reaches. The
channel in (1) and (2) already carries the sub-agent's context slice — a slice of the
actual conversation — and `search_memory`'s own turn previews. For (3), the sub-agent
reads the full tool description in its tool definitions and holds the turn's context,
so the control is exactly as strong as it is for the primary.

The one genuine widening is the **time window**, from this conversation to a year of
them. That is stated in the config entry and flagged for the owner at the gate, rather
than settled silently here. FRE-1388 remains where a narrower sub-agent principal
would be defined.

**Rewrite `search_memory`'s reason.** Its current entry justifies the grant *by* the
missing identity: "the sub-agent's TraceContext carries no user_id … so
query_claims returns []". This change deletes that justification. The new ground is
the one above: an internal read of the connecting user's own graph, no egress, no
spend, returned to that same user, equal to what the primary already reads on that
user's behalf in the same turn.

**This is the discretionary decision in this PR.** AC-1 forces the `search_memory`
widening whether or not `recall_personal_history` is granted, so the escalation of the
sub-agent principal from "narrower than the primary" to "equal to the primary" is the
approved ticket's own consequence. The `recall_personal_history` grant is the part
that is a judgement, and it is flagged for the owner at the master gate.

## 7 — Tests

New file: `tests/personal_agent/orchestrator/test_sub_agent_identity.py`.

Each test asserts an **outcome** — a value observed at a boundary — not a wiring shape.
The one exception is the same-object test, which asserts an implementation invariant
on purpose; see §11 item 17.

| Test | Asserts |
|------|---------|
| `test_sub_agent_llm_call_carries_identity` | Real `run_sub_agent(user_id=…, authenticated=True)` with a mock client → `respond`'s `trace_ctx` has that `user_id` and `authenticated is True`. AC-1, site 1. |
| `test_sub_agent_tool_dispatch_carries_identity` | Same call, mock client returning one tool call → the `trace_ctx` reaching `dispatch_tool_call` has that `user_id` and `authenticated is True`. AC-1, site 2. |
| `test_sub_agent_llm_and_tool_contexts_are_the_same_object` | Both boundaries receive the identical context. This is the AC-1 failure clause turned into an assertion — two constructions could diverge; one cannot. |
| `test_sub_agent_context_carries_eval_mode` | `eval_mode=True` reaches the dispatch context. The §4.1 fold-in. |
| `test_recall_personal_history_no_longer_raises_for_a_sub_agent_context` | The **real** `recall_personal_history_executor` (not a mock) called with the context `run_sub_agent` now builds → does not raise `missing_user_id`. Partial AC-2: the code-level half. The live half is master's. |
| `test_redispatch_on_gap_carries_identity` | `_maybe_redispatch_on_gap` → its `run_sub_agent` call receives `user_id`/`authenticated`. The second worker call the AC-1 failure clause is about. |
| `test_expansion_execute_threads_identity_to_dispatch` | `ExpansionController.execute(user_id=…, authenticated=True)` → the patched `run_sub_agent` receives both. End-to-end through the controller. |
| `test_executor_passes_identity_to_expansion` | Source guard on `executor.py`, matching the existing `test_trace_ctx_identity.py` convention: `user_id=ctx.user_id` and `authenticated=ctx.authenticated` appear on the `controller.execute` call. |
| `test_granted_sub_agent_tools_include_recall_personal_history` | The shipped `config/governance/tools.yaml` grants it. AC-4. |
| `test_sub_agent_tool_reasons_do_not_cite_missing_identity` | No `sub_agent_tools` reason still claims the sub-agent carries no `user_id`. AC-3/AC-4: catches the stale justification that this change falsifies. |

TDD order: write the file, confirm every test fails for the stated reason, then
implement §4.

## 8 — Verification

```bash
make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent_identity.py
make test-file FILE=tests/personal_agent/orchestrator/test_trace_ctx_identity.py
make test-file FILE=tests/personal_agent/orchestrator/test_sub_agent.py
make test-file FILE=tests/personal_agent/orchestrator/test_expansion_controller.py
make test-file FILE=tests/test_governance
make test
make mypy && make ruff-check && make ruff-format
pre-commit run --all-files
```

Expected: all pass. `make test` in this worktree reports one unrelated failure,
`test_exa_content_mode_and_length` — `docker/searxng/settings.yml.example` carries
git's `assume-unchanged` bit, so the owner's local Exa settings are invisible to
`git status` while pytest reads them. CI reads the committed blob and is green.

## 9 — Post-deploy runbook (AC-2, master's step)

After deploy, fire one owner turn that forces HYBRID and names personal history, then
read the sub-agent's tool result. Three checks, not one:

1. A `recall_personal_history_called` log line carrying a real `user_id`, and no
   `missing_user_id` error in the sub-agent's tool result.
2. Every returned turn belongs to that user — the `user_id` on the result matches the
   caller's. "It stopped erroring" is not "it returned the right rows".
3. An unauthenticated turn still retrieves nothing. The guard must not have been
   widened by accident.

Exact commands and expected output go in the handoff comment.

## 10 — Diff class

Not a production write path, not destructive, no schema change. It **is** governance
code (`config/governance/tools.yaml`) and it widens a read of personal data.
**Escalate** — flagged for the owner's `/code-review ultra` before merge.

## 11 — Codex plan-review disposition

Codex reviewed §1-§7 against the source. Every finding is recorded here with what was
done. Two were already fixed before the review landed; both are marked as such.

**Accepted and fixed in the diff:**

1. *"`web_search` and `run_python` read no field of `ctx`" is false — both read
   `ctx.trace_id`.* Correct. The module docstring now says "no **identity** field",
   naming `ctx.trace_id`.
2. *`query_stance_history` is scoped to the harness owner's `Person {is_owner: true}`
   sentinel, not to the connecting `user_id`.* Correct and verified at
   `memory/service.py:3063`. Calling it part of "the connecting user's own graph" was
   wrong. The docstring and the config reason now say identity unlocks only its
   `authenticated` gate there.
3. *"No egress" is false for the complete path.* Correct, and the most useful finding.
   The Neo4j query reaches no external host, but the rows enter the sub-agent's
   message stream, go to the configured sub-agent provider — possibly cloud — and a
   sub-agent also holding `web_search` can place them in a query SearXNG forwards
   upstream. Both config reasons now state this.
4. *The memory-to-web disclosure direction was not discussed, only web-to-memory
   injection.* Correct. Recorded in the `recall_personal_history` reason.
5. *The `recall_personal_history` output list was incomplete.* Corrected: `turn_id`,
   timestamp, session id, `assistant_response`, entities, topic-match flag.
6. *The planner context omits `eval_mode` though `execute` receives it.* Correct.
   Added, with a test.
7. *`test_redispatch_on_gap_forwards_identity` greps source instead of executing.*
   Correct. Replaced with a real `_run_dispatch` run in which the first worker states
   a gap and the assertion is on the replacement call's kwargs.
8. *No planner-context test.* Added.
9. *The runbook checks only that `missing_user_id` disappears.* Correct. §9 now also
   checks that returned turns belong to the caller and that an unauthenticated turn
   retrieves nothing.
10. *Plan §1's "every sub-agent" is too broad — `generate_query_paraphrases`
    (`memory/service.py:246`) and `artifact_draft_executor`'s fallback context
    (`tools/artifact_tools.py:1466`) also build identity-free contexts.* Correct.
    Dispositioned rather than fixed: neither grants a tool, so neither can produce
    the defect this ticket is about. Not ticketed — no behaviour changes, and the
    cost is attribution only. §1's claim is narrowed to `sub_agent.py`.

**Already fixed before the review landed** (Codex read a mid-flight tree):

11. *`test_recall_personal_history_no_longer_raises_for_a_sub_agent_context` is
    vacuous — it hand-builds the context.* True of the first draft. It now captures
    the context from a real `run_sub_agent` dispatch.
12. *`test_executor_passes_identity_to_expansion` greps the whole module for a string
    that predates the change, and never checks `user_id`.* True of the first draft.
    Now scoped to the `controller.execute` call and checks both kwargs.
13. *`UUID` import missing.* Both modules import it.

**Corrected, not accepted:**

14. *"Returns raw slices of user and assistant messages, not summarized facts", so
    `search_memory` cannot be called broader.* Half right. Both tools cap turn text at
    400 characters via `mark_truncated` (`personal_history.py:191-192`,
    `memory_search.py:204`), so the sensitivity gap is smaller than stated. The real
    difference is **reach**: up to 50 turns across up to 365 days with no relevance
    filter, and `assistant_response`, which `search_memory`'s matched turns omit. The
    config reason now says reach, not raw text.
15. *"No deterministic explicit-user-intent gate", the tool's own contract being
    violated.* The premise holds — the planner authors the task — but the conclusion
    does not follow: `_build_tool_defs` hands the sub-agent the tool's full
    description, including the "use ONLY when the user explicitly refers to their
    personal history" clause, and the sub-agent holds the turn's context slice. The
    control is exactly as strong as it is for the primary. Recorded in the config
    reason as a stated weakness rather than a refusal ground.
16. *"Section 6 should evaluate these rather than state no coherent refusal ground
    exists."* Accepted as a criticism of the writing; the verdict stands after
    evaluation. Neither disclosure argument distinguishes `recall_personal_history`
    from what a sub-agent already reaches — the same channel carries its context
    slice and `search_memory`'s turn previews today. The one genuine widening is the
    time window, and that is now stated in the config entry and flagged for the owner.

**Noted, no action:**

17. *The same-object test asserts an implementation invariant, not an externally
    meaningful property.* Fair. Two equal contexts would also be correct. It is kept
    because AC-1's failure clause is about two constructions drifting apart, and it is
    labelled as such rather than as an outcome assertion. §7's "every test asserts an
    outcome" is overstated by this one test.
