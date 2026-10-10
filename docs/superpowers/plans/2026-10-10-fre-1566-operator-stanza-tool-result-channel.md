# FRE-1566 — Operator stanza: graph facts leave the system prompt (ADR-0140 T2)

Ticket: FRE-1566 · ADR-0140 T2 / AC-4 · ADR-0081 D4 · FRE-1150 (identity precedence) ·
FRE-1360 (`orchestrator/untrusted_channel.py`).

## Owner decisions (2026-10-10)

1. **Name source: authentication, never the graph.** The system prompt keeps
   `You are assisting {name}.` and the FRE-1150 authority rule. `{name}` comes from
   `settings.owner_name` when the email is `settings.agent_owner_email`, else
   `users.display_name`, else the email local-part. The `:Person.name` value never reaches
   the model. (Owner first, after codex review: `bootstrap_owner_identity` seeds the owner's
   graph name from `owner_name`, so the owner keeps the name it gets today.)
2. **Profile facts ride the `memory_recall` tool result.** The graph-derived fields
   (location, pronouns, role, languages) are prepended to the per-turn memory section. That
   section already reaches the model as a harness tool exchange (FRE-1360). No new exchange.

## Facts the plan rests on

- `get_or_provision_user_person` returns `:Person` facts. `update_person_name_if_default` and
  extraction can change `name`, so the graph name is agent-writable (ADR-0081 D4).
- `ctx.operator_stanza` is used at one model-facing site only: the `step_llm_call` system
  prompt (`executor.py` ~6619).
- The HYBRID planner never reads `ctx.operator_*`. Its history briefing drops tool messages
  (`planner_history_text`, pinned by FRE-1360's
  `test_ac2_planner_history_never_renders_tool_messages`). The profile facts in prior turns'
  `memory_recall` results therefore never reach the planner. The planner request is unchanged,
  so ADR-0154 D7 does not need a re-score. A test pins this.

## Steps

1. **Failing tests first** — new file
   `tests/personal_agent/orchestrator/test_fre1566_operator_identity_channel.py`:
   - AC-1: seed `:Person` facts `{"name": NAME_MARKER, "location": LOC_MARKER}` through a mock
     MemoryService, run `_populate_operator_identity`, drive the real `step_llm_call` with the
     FRE-1360 `_drive_loop` helper, read the wire form. Assert `NAME_MARKER` is in no message.
     Assert `LOC_MARKER` is only in the `memory_recall` tool result (FRE-1360's
     `_assert_marker_only_in_tool_results`).
   - AC-2: the same wire carries `You are assisting <auth name>.` and the authority rule in the
     system message.
   - Name resolution: display name wins · owner email (case-insensitive) → `settings.owner_name`
     · otherwise the local-part · the graph name is never used.
   - Profile placement: the profile block sits at the head of the memory tool result, before
     the recall content and the ADR-0148 state line.
   - Planner: `planner_history_text` over a history whose `memory_recall` results carry a
     profile marker returns bytes identical to the same history without the profile.
   Run: `make test-file FILE=tests/personal_agent/orchestrator/test_fre1566_operator_identity_channel.py`
   → expect failures.
2. **`orchestrator/prompts.py`**:
   - `_authenticated_name(email, display_name) -> str` — the resolution in decision 1.
   - `OperatorIdentity` gains `profile: str = ""`.
   - `get_owner_identity`: keep every existing gate unchanged (no service / user_id / email,
     empty facts, no graph name → empty identity). Render the header and the authority rule
     with the authenticated name. Move the detail lines out of `stanza` into `profile`
     (`Known facts about {name} (from memory):` + the existing capped, whitelisted lines).
     `assertion` keeps its shape (header + authority rule).
3. **`orchestrator/types.py`**: `ExecutionContext.operator_profile: str = ""`.
4. **`orchestrator/executor.py`**:
   - `_populate_operator_identity` sets `ctx.operator_profile`.
   - `step_llm_call`: after the ADR-0148 state-line block, prepend `ctx.operator_profile` to
     `memory_section`. The section then rides the existing `memory_recall` exchange.
5. **Existing tests** (`test_owner_stanza.py`, `test_identity_precedence.py`): inputs only.
   Tests that relied on the graph name for the rendered name now pass the name as the
   authenticated display name. Detail-line assertions move from `.stanza` to `.profile`. No
   assertion about precedence is weakened.
6. **Docs**: `scripts/render_prompt_corpus.py` descriptions of `operator_stanza`; a closure
   note on ADR-0140 AC-4; a one-line note on the ADR-0081 D4 `operator_stanza` contingency.
7. **Gates**: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` ·
   `pre-commit run --all-files`.

## Acceptance criteria → proof

| AC | Proof |
|----|-------|
| AC-1 graph identity facts not in system/user text | `test_fre1566_…::TestAc1…` — name marker absent from the wire, profile marker only in the `memory_recall` tool result |
| AC-2 precedence holds, rule stays in system | `test_identity_precedence.py` passes; `TestAc2…` asserts the rule + auth name in the system message |
| AC-3 cache hit rate measured | Post-deploy, read by master: `orchestrator.primary` cached-prefix hit rate, comparable windows before and after |
| Planner unchanged (master's gate note) | planner byte-identity test |

## Codex plan review — dispositions

- Graph gates drop the auth stanza when Neo4j fails — **kept as is.** Existing behaviour,
  pinned by the FRE-1150 tests AC-2 names. With Neo4j down, recall is absent too.
- Owner name precedence — **adopted** (decision 1).
- Profile repeats once per persisted turn — **accepted**; recall persists the same way.
- Planner test compares the full `planner_request_messages` — **adopted**.
- Tests for profile-only, a non-populated memory state, name edge cases (blank display name,
  owner email in another case, email without `@`) — **adopted**.

## Cache consequence (for AC-3)

The system prefix changes once at deploy: the detail lines leave, and the name source can
change for a user whose graph name differs from the authenticated name. After deploy the
prefix no longer varies with graph writes, so it can only become more stable. Each turn's
`memory_recall` result grows by the profile lines (a few dozen tokens, persisted in history).
