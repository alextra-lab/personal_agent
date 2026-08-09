# FRE-1008: Prompt static-prefix and dynamic hashes are computed from the same input

**Ticket:** [FRE-1008](https://linear.app/frenchforest/issue/FRE-1008/prompt-static-prefix-and-dynamic-hashes-are-computed-from-the-same)
**Backing:** ADR-0078 D4 (revised 2026-05-29), interacting with ADR-0081 D1 (Volatility-gradient layout, FRE-434)

## Root cause (more precise than the ticket's own description)

The ticket names `client.py:550-551` / `litellm_client.py:718-719` as the two broken call sites
(line numbers have since drifted to ~563-567 / ~783-787). Those *are* broken, but there is a
second, higher-volume defect the ticket's live sample almost certainly mostly reflects:
**`executor.py`'s own `orchestrator.primary` identity construction (current lines 4730-4769)**,
which explicitly bypasses the fallback by passing `prompt_identity=_prompt_identity` — yet is
itself broken under the *current* architecture:

- ADR-0078 D4 was written against the pre-ADR-0081 assembly, where `memory_section` was spliced
  directly into `system_prompt` (the "two f-string splices" described in ADR-0078 Context #4).
  Under that model, capturing `system_prompt` *before* the memory splice legitimately produced a
  different string than the final `system_prompt` used for `dynamic_hash`.
- ADR-0081 D1 (FRE-434) replaced that: `memory_section` (plus skill bodies, salient highlights,
  the artifact-planning note) is no longer spliced into `system_prompt` at all. It's assembled
  into `_volatile_block` (executor.py:4679-4688) and inlined into `ctx.messages` instead.
  `system_prompt` is **never mutated** after the `inner_system_before_memory` capture
  (executor.py:4659).
- Consequence: `_static_prefix = inner_system_before_memory` (line 4734) and
  `full_prompt=system_prompt or ""` (line 4767) are now **the same string** — the exact defect
  the ticket describes, just relocated from the two named fallback sites to the call site that
  was supposed to be the correct one. `_volatile_block`, the actual per-turn dynamic content, sits
  in scope four lines away and is never hashed.

The two named fallback sites (`client.py`, `litellm_client.py`) are a second, independent instance
of the same defect class, hit by every call site that does *not* build a component-aware
`PromptIdentity` (session_summary, entity_extraction, captains_log feedback/reflection, memory
paraphrase, artifact_tools, sub_agent, expansion_controller, skills routing). For two of these
(`session_summary.py:619`, `entity_extraction.py:1065`) `system_prompt=None` because the system
content is embedded in `messages` instead — this is the empty-string sample the ticket observed
live.

`gateway/chat_api.py`'s `derive_prompt_identity("gateway.chat", static_prefix=_SYSTEM_PROMPT,
full_prompt=_SYSTEM_PROMPT, ...)` is a **third** instance of "same value twice" but is *correct
by design* (ADR-0078 D4 says so explicitly) — it's one fixed persona string with no dynamic tail
at that emit point. Not touched by this fix; cited here only to explain why "same value twice" is
not automatically a bug everywhere.

## Design questions the ticket asks to be answered explicitly

**Q1 — does the taxonomy order reflect real assembly order?** Read executor.py:4607-4659
line-by-line: the STATIC/SEMI-STATIC assembly order is tool rules → tool awareness → base system
body → decomposition, all captured into `inner_system_before_memory` *before* the volatile tier is
computed. The boundary itself (where static ends) is correctly placed — it is captured by direct
code position, not inferred from `PROMPT_COMPONENT_TAXONOMY`'s iteration order, so the hashes never
depended on that order being accurate. Separately, `PROMPT_COMPONENT_TAXONOMY`'s literal tuple
order does **not** match true byte-assembly order (e.g. `tool_awareness` is listed first but
`tool_use_rules`/`tool_prompt` is assembled first in the string) — but `component_ids` is a
descriptive audit field that feeds neither hash (existing comment at executor.py:4754 already
notes this). Answer: boundary placement is correct; the taxonomy docstring's "mirrors assembly
order" claim is inaccurate and gets corrected in this diff (documentation only, no behavior
change, no reordering of the taxonomy tuple or the `_component_ids.append()` calls — reordering
either has its own separate blast radius on the corpus renderer / insights detector that this
ticket has no reason to touch).

**Q2 — is the full prompt available at the fallback call sites?** Yes, without disproportionate
plumbing. `executor.py` already computes `_volatile_block` in local scope four lines before
constructing `_prompt_identity` — the fix is to use it, not fetch anything new. For
`client.py`/`litellm_client.py`, `request_messages`/`api_messages` (the actual message list sent
to the model) is already a local variable at the point `derive_prompt_identity` is called — no
plumbing needed there either. Tool schemas are deliberately excluded from `dynamic_hash` at these
fallback sites: for every one of the nine non-taxonomy call sites, tools are either absent or a
single fixed schema per callsite (e.g. `digest_tool()`), so they carry no per-call signal and
hashing them adds cost without adding a distinguishing bit. This is the "honest narrower contract"
the ticket allows — stated here, in the module docstring, and in the PR description, not left
implicit.

## Revision after codex plan-review (2026-08-08)

Codex (`codex:rescue`, job `task-msktop34-ognq8h`) confirmed the ADR-0081 root-cause analysis but
**refuted the originally-proposed fix boundary** (hashing `static_prefix + volatile_block`) and
found two smaller real gaps. Verdicts, and how the fix below changes in response:

1. **ADR-0081 root cause — CONFIRM.** No change.
2. **`static_prefix + volatile_block` as the dynamic-hash boundary — REFUTE.** `request_messages`
   (what actually goes over the wire) also carries the current query, frozen history,
   assistant/tool messages, forced-synthesis/budget-warning content, and the `/no_think` suffix —
   none of which `_volatile_block` captures. Worse, `_inline_volatile_with_outcome` can return
   `NO_TARGET`/`ALREADY_WRAPPED`, so the precomputed `_volatile_block` string can diverge from what
   was actually spliced into `ctx.messages` on a given call — hashing the candidate, not the
   result. **Fix changes:** hash the actual `request_messages` (+ `tools`, a separate wire input)
   at the point they're handed to `llm_client.respond()`, not the intermediate `_volatile_block`
   variable. `inner_system_before_memory` stays exactly as the `static_prefix` input — that capture
   point was independently confirmed correct.
3. **Fallback design — PARTIAL.** The system-prompt-or-embedded-system-message static resolution is
   right. Two real gaps: (a) a naive blank-line join of message text loses role/message boundaries
   and drops non-text content blocks, so two structurally-different requests can hash identically —
   fixed by a role-tagged, block-type-aware serializer (below), shared by both the orchestrator and
   fallback paths per codex's "one canonical serializer" recommendation. (b) tool schemas were
   excluded entirely; `session_summary.py`'s conditional `digest_tool()` means enabling/disabling or
   changing that contract wouldn't move the hash — fixed by folding a stable serialization of
   `tools` into the same serializer wherever a callsite passes them.
4. **`gateway.chat` left untouched — PARTIAL.** Confirmed correct under ADR-0078 D4's own text
   *and* `PROMPT_MANAGEMENT_SPEC.md`'s explicit non-orchestrator-callsite contract — but codex is
   right that leaving it as the *only* remaining "same value twice, and that's fine" callsite,
   once every other fallback site starts differentiating, is an inconsistency worth naming rather
   than leaving implicit. Not fixed here (`gateway/chat_api.py` calls the Anthropic SDK directly, a
   different wire shape than the OpenAI-format `messages` this ticket's serializer targets — pulling
   it in is separate scope, its own risk surface, and not required by any of this ticket's own
   acceptance criteria). **Action:** file a `Backlog` ticket noting `gateway.chat`'s dynamic_hash has
   the same structural gap (persona-only, doesn't cover `anthropic_messages`) once this ticket lands,
   so the exception is visible rather than silently inherited as "the pattern."
5. **Test gaps — CONFIRM.** The originally-planned unit tests only prove the helpers compute
   correctly, not that `executor.py` wires the right bytes into them — and the plan's "empty
   volatile block → dynamic_hash == static_prefix_hash" test would, under the corrected design,
   encode *wrong* behavior (the user's query is part of `request_messages` on every real call, so
   dynamic_hash essentially never equals static_prefix_hash once request_messages is what's hashed).
   **Fix changes:** drop that test; add an integration test using the existing `_drive` fixture
   pattern (`tests/test_orchestrator/test_skill_index_split.py`) that drives `step_llm_call` with
   varying user queries and asserts the `prompt_identity` passed to the mocked `llm_client.respond`
   reflects it — proving the wiring, not just the helper.

**Two additional findings, folded in (both are the same class of defect this ticket exists to fix —
the audit surface silently misrepresenting what was actually sent):**
- `salient_highlights` contributes bytes to `_volatile_block` but has no `component_ids` entry
  (neither the executor's append list nor `PROMPT_COMPONENT_TAXONOMY`) — the component audit trail
  falsely implies it was never present. Add `"salient_highlights"` to both.
- Fallback callsites all collapse to `f"role.{role.value}"`, so distinct call sites sharing a
  `ModelRole` (memory paraphrasing, expansion planning, ordinary sub-agents can all be
  `role.sub_agent`) become indistinguishable in the `callsite` field — a real attribution gap, but
  a *different* one (naming granularity, touching `PROMPT_MANAGEMENT_SPEC.md`'s callsite registry
  across ~9 sites) than "the two hashes are computed from the same input." Not fixed here — filed
  as a separate `Backlog` ticket instead of folded in, since it doesn't affect hash correctness and
  is its own, larger unit of work.

## Fix (revised)

### 1. `src/personal_agent/llm_client/prompt_identity.py`

One shared serializer plus two thin, independently-testable wrappers (module docstring gets a
short addendum explaining all three, and the Q1 taxonomy-order correction described earlier):

```python
def _serialize_dynamic_content(
    request_messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Deterministic, structure-preserving serialization of an actual wire request.

    Used as the dynamic_hash input for every callsite with no component-aware
    split (orchestrator.primary's volatile tail, and every fallback callsite).
    A naive join of message text would let two structurally different
    requests collide on identical concatenated text (FRE-1008 codex
    plan-review finding); role tags and content-block types make the
    serialization sensitive to structure, not just text. Tool schemas are
    included when present — a call site that starts/stops sending tools, or
    changes a tool's schema, must move dynamic_hash (session_summary.py's
    conditional digest_tool() was the concrete gap that ruled out omitting
    this).
    """
    parts: list[str] = []
    for m in request_messages:
        role = m.get("role", "")
        content = m.get("content")
        block_types = (
            ",".join(sorted({b.get("type", "") for b in content if isinstance(b, dict)}))
            if isinstance(content, list)
            else ""
        )
        parts.append(f"[{role}|{block_types}]{get_text_content(content)}")
        if m.get("tool_calls"):
            parts.append(f"[tool_calls]{json.dumps(m['tool_calls'], sort_keys=True, default=str)}")
    if tools:
        parts.append(f"[tools]{json.dumps(list(tools), sort_keys=True, default=str)}")
    return "\n".join(parts)


def derive_orchestrator_prompt_identity(
    *,
    static_prefix: str,
    request_messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
    component_ids: tuple[str, ...] = (),
) -> PromptIdentity:
    """Build the orchestrator.primary PromptIdentity (ADR-0078 D4 + ADR-0081 D1).

    static_prefix is inner_system_before_memory, captured before any VOLATILE
    content is assembled (executor.py, unchanged by this fix — independently
    confirmed correct by codex plan-review). dynamic_hash must cover what is
    actually sent: request_messages (which, under ADR-0081, carries the
    per-turn volatile tail inlined into the current user turn) plus tools —
    not a precomputed candidate block, since _inline_volatile_with_outcome can
    return NO_TARGET/ALREADY_WRAPPED and diverge from what a precomputed
    volatile-block string would represent (FRE-1008 codex plan-review
    finding). Call with the SAME request_messages/tools values passed to
    llm_client.respond() on this call, so the hash matches the actual wire
    request, not an earlier draft of it.
    """
    full_prompt = f"{static_prefix}\n\n{_serialize_dynamic_content(request_messages, tools)}"
    return derive_prompt_identity(
        "orchestrator.primary",
        static_prefix=static_prefix,
        full_prompt=full_prompt,
        component_ids=component_ids,
    )


def derive_fallback_prompt_identity(
    callsite: str,
    *,
    system_prompt: str | None,
    request_messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None = None,
) -> PromptIdentity:
    """Build a PromptIdentity for call sites with no component-aware split (FRE-1008).

    Used by LocalLLMClient/LiteLLMClient when the caller passes no explicit
    prompt_identity (every callsite except orchestrator.primary and
    gateway.chat). static_prefix is the effective system content — system_prompt
    if given, else the first embedded system-role message (fixes the
    empty-string collapse seen live when callers fold system content into
    messages instead: session_summary.py, entity_extraction.py). dynamic_hash
    covers that plus every non-system message and any tools actually sent —
    the real per-call variation these single-persona call sites do carry,
    even though the persona itself is typically fixed.
    """
    static_content = system_prompt or _first_system_message_text(request_messages)
    non_system = [m for m in request_messages if m.get("role") != "system"]
    full_prompt = f"{static_content}\n\n{_serialize_dynamic_content(non_system, tools)}"
    return derive_prompt_identity(callsite, static_prefix=static_content, full_prompt=full_prompt)


def _first_system_message_text(request_messages: Sequence[Mapping[str, Any]]) -> str:
    for m in request_messages:
        if m.get("role") == "system":
            return get_text_content(m.get("content"))
    return ""
```

(`get_text_content` from `personal_agent.llm_client.message_content` — already used elsewhere for
this exact block-aware extraction; `json` and `Sequence`/`Mapping` imports added.)

### 2. `src/personal_agent/orchestrator/executor.py` (~line 4764)

Replace the direct `derive_prompt_identity(...)` call with
`derive_orchestrator_prompt_identity(static_prefix=_static_prefix, request_messages=request_messages, tools=tools, component_ids=tuple(_component_ids))`.
`request_messages` (finalized post no_think-injection/role-validation, line 4701-4707) and `tools`
(finalized post forced-synthesis override, line ~4574) are both already in scope and are the exact
values passed to `llm_client.respond()` five lines later (4844-4854) — same variables, no copy.

Also add `"salient_highlights"` to the `_component_ids.append()` sequence (guarded on
`ctx.salient_highlights` truthy, alongside the other volatile-tail components) and to
`PROMPT_COMPONENT_TAXONOMY` — it contributes bytes to the volatile tail today with no audit-trail
entry (codex plan-review finding). Update `test_prompt_identity_taxonomy.py`'s
`executor_component_ids` set and `test_executor_source_contains_expected_appends`'s expected-appends
tuple to match.

### 3. `src/personal_agent/llm_client/client.py` (~line 563) and `litellm_client.py` (~line 783)

Replace the fallback `derive_prompt_identity(f"role.{role.value}", static_prefix=system_prompt or "", full_prompt=system_prompt or "")`
with `derive_fallback_prompt_identity(f"role.{role.value}", system_prompt=system_prompt, request_messages=request_messages, tools=tools)`
(`api_messages` in litellm_client.py — same variable role, different name; `tools` is the method's
own `tools` parameter, already in scope, unmutated at this point in both files).

## Tests (TDD — written first, confirmed failing against current code, then made to pass)

`tests/personal_agent/llm_client/test_prompt_identity.py` (extend existing file):

1. `test_serialize_dynamic_content_distinguishes_structure_not_just_text` — two message lists whose
   concatenated text is identical but role/boundary structure differs (e.g. one message
   `"a" + "b"` vs two messages `"a"`, `"b"`) → different serialized output. Directly covers the
   collision gap codex flagged.
2. `test_serialize_dynamic_content_covers_tools` — identical messages, two different `tools`
   schemas → different serialized output. Covers the `digest_tool()` gap.
3. `test_orchestrator_identity_dynamic_hash_covers_request_messages` — same `static_prefix`, two
   different `request_messages` (simulating a different volatile tail actually inlined into the
   turn) → `static_prefix_hash` equal, `dynamic_hash` differs. PROOF REQUIRED test 1, against
   `derive_orchestrator_prompt_identity` (confirm it fails first against the *old* inline
   `derive_prompt_identity(static_prefix=X, full_prompt=X)` call, then passes against the new
   helper).
4. `test_orchestrator_identity_dynamic_hash_covers_tools` — same `static_prefix`/`request_messages`,
   different `tools` → `dynamic_hash` differs.
5. `test_fallback_identity_no_system_prompt_uses_embedded_system_message` — `system_prompt=None`,
   `request_messages=[{"role": "system", "content": X}, {"role": "user", "content": Y}]` →
   `static_prefix_hash == _short_hash(X)`, neither hash equals `_short_hash("")`. PROOF REQUIRED
   test 2.
6. `test_fallback_identity_dynamic_hash_covers_message_tail` — same system content, two different
   user messages → `static_prefix_hash` equal, `dynamic_hash` differs.
7. `test_fallback_identity_truly_empty_is_still_hashable` — no system prompt, no system message,
   empty messages → both hashes are `_short_hash("")` (documented degenerate case, not a crash).

Run: `make test-file FILE=tests/personal_agent/llm_client/test_prompt_identity.py`

**New integration test** (codex plan-review finding: unit tests alone don't prove `executor.py`
wires the right bytes through) — `tests/test_orchestrator/test_prompt_identity_wiring.py`, following
the `_drive`/mock-`llm_client.respond` pattern from `tests/test_orchestrator/test_skill_index_split.py`:

8. `test_dynamic_hash_differs_when_user_query_differs` — drive `step_llm_call` twice with the same
   mocked skill/memory fixtures but a different `ctx.user_message` / `ctx.messages` → the
   `prompt_identity` captured from the mocked `llm_client.respond` call has equal
   `static_prefix_hash` and different `dynamic_hash` across the two runs.
9. `test_dynamic_hash_differs_when_memory_context_differs` — same query, different
   `ctx.memory_context` (varying what `_render_memory_section_with_ids` produces) → same
   `static_prefix_hash`, different `dynamic_hash`. This is the ADR-0078 D4 P1 acceptance property,
   proven end-to-end this time instead of only at the helper level.

Run: `make test-file FILE=tests/test_orchestrator/test_prompt_identity_wiring.py`

Also update `tests/personal_agent/llm_client/test_prompt_identity_taxonomy.py`: add
`"salient_highlights"` to `executor_component_ids` (both test methods) per the Fix section's §2
addition.

## Proof required (from the ticket) — mapping to evidence

- "Two prompts differing only after the static boundary → static matches, dynamic differs": tests 3, 9.
- "A call with no system prompt does not produce the empty-string hash for both fields": test 5.
- "A live sample after deploy showing the two fields taking different values on real traffic":
  master's job post-deploy, not provable pre-merge. Handoff comment will give the exact query
  (sample recent `model_call_completed` events, compare `prompt_static_prefix_hash` vs
  `prompt_dynamic_hash`).

## Out of scope (named, not silently dropped)

- Reordering `PROMPT_COMPONENT_TAXONOMY` or the `_component_ids.append()` sequence to match true
  byte-assembly order — cosmetic only (feeds no hash), separate blast radius (corpus renderer,
  insights detector consume the taxonomy), no ticket criterion requires it.
- `gateway/chat_api.py`'s `gateway.chat` identity — correct under ADR-0078 D4's stated contract for
  non-orchestrator callsites, but shares this ticket's structural gap once every other fallback
  callsite starts differentiating (codex plan-review point 4). File a `Backlog` ticket rather than
  fold in: different wire shape (Anthropic SDK, not the OpenAI-format `messages` this fix targets),
  its own risk surface, not required by this ticket's criteria.
- Per-callsite naming granularity for the ~9 fallback callsites (`f"role.{role.value}"` collapses
  distinct call sites sharing a `ModelRole` — codex plan-review "anything else wrong" finding). File
  a `Backlog` ticket: touches `PROMPT_MANAGEMENT_SPEC.md`'s callsite registry across every fallback
  site, doesn't affect hash correctness, and is its own, larger unit of work.
