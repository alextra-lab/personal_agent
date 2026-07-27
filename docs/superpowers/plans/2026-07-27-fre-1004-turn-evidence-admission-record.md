# FRE-1004 — Admitted recall identities + assembled context, with explicit absence

**Ticket:** FRE-1004 (Approved, Tier-1:Opus, stream:build2) · parent FRE-999
**Backing:** ADR-0125 D3 (evidence items 5 and 6) + D4 · satisfies AC-3 in full · delivers the
explicit-absence half of AC-6
**Upstream:** FRE-1000 (Done) — item 4 present, item 6 confirmed missing
**Downstream:** FRE-1005 (usage edge) · FRE-1006 (seam, AC-6)
**Revision:** Rev 3 — Rev 2 revised after codex plan-review (four defects accepted, two rejected with
reasons); Rev 3 records what the pre-PR self-review then changed in the built code (§8)

---

## 1. What is broken (verified against source this session, not inherited)

| Claim | Evidence |
|---|---|
| The capture holds a boolean + a count, no identities | `captains_log/capture.py:69-70`, written from `bool(ctx.memory_context)` / `len(...)` at `orchestrator/executor.py:2404-2407` |
| Recall **scores are discarded at the gateway** | `request_gateway/context.py:194` returns `[c.payload for c in suggestions.candidates]`; `ProactiveMemoryCandidate.relevance_score` (`memory/proactive_types.py:32`) dies there |
| Budget trimming destroys the candidate set | `request_gateway/budget.py:270-295` sets `memory_context = None`; only `entities_dropped` reaches compaction telemetry — the ADR's "records what it discarded, not what it relied on" asymmetry, exactly |
| Rendering silently drops candidates | `executor.py:2112` keeps the first 15 entity items **with a non-blank description**; `executor.py:4041` drops every `type == "session"` item; `executor.py:4049` caps the task-assist path at **3** |
| The rendered block can fail to reach the wire | `_inline_volatile_into_last_user_message` (`executor.py:1219-1234`) is a no-op on an empty block, on non-string user content, and when no user message exists |
| Item 6 has no durable record | `prompt_manifest` is built inside `generate_reflection_entry` and discarded; `PROMPT_COMPONENT_TAXONOMY` (`llm_client/prompt_identity.py:47-57`) is nine presence flags; `full_prompt` is hashed and never persisted |

**Entity identity in this graph is the name.** `MERGE (e:Entity {name: $name})` (`memory/service.py:1080`, `:1947`)
and `entity_id` is set to the name (`service.py:376`, `:1979`). So `name` *is* the durable identifier that
resolves back to the claim record — which is what AC-6's joinability half will need. Episodes are identified by
`turn_id` / `conversation_id`; sessions by `session_id`; session facts by their source turn index.

---

## 2. The admission point — defined once, shared by both records

> **The admission point is the first primary model call of the turn, taken at its provider-neutral wire form:**
> ```
> wire = sanitise_messages([{"role": "system", "content": system_prompt}] + request_messages)[0]
> ```
> computed in the executor immediately before `llm_client.respond()` (`executor.py:4266`), using the **same
> public `sanitise_messages`** both clients call.

Three corrections this encodes, all of them found by review rather than assumed:

1. **`executor.py:4266` is not by itself the wire form.** Both clients independently perform the identical
   two-step pre-flight — prepend the system message, then sanitise — at `llm_client/client.py:349-355` and
   `llm_client/litellm_client.py:382-390`. `sanitise_messages` is lossy: it strips `<tool_code>` blocks,
   drops orphaned tool messages, can truncate to the last clean user turn, and can append a synthetic
   continuation (`history_sanitiser.py:80`, `:147`, `:208`, `:218`, `:244`). A manifest built before it can
   describe messages that never reached the provider. The executor therefore mirrors the same two steps, via
   the same function, and a **sync-guard test** asserts the two constructions agree.
2. **Provider decoration is additive and deliberately excluded.** The Anthropic path deep-copies and attaches
   `cache_control` markers (`litellm_client.py:151-179`) — request-local metadata and a `str`→`list` system
   promotion, never content removal. The provider-neutral wire form is therefore the correct manifest basis,
   and the record does not vary by provider.
3. **"First primary call", not "last".** ADR-0125 AC-3's admission point is *"the final serialized model input
   after all trimming and compaction"* of **context assembly**, and its forced mechanism is budget trimming —
   all of which resolve before the first call. Later calls in the tool/hybrid loop are continuations, not
   fresh assemblies (`executor.py:4412-4450`). One record, one call: `primary_call_index = 0`, with
   `primary_call_count` recorded at capture time so a reader knows the turn made N calls.

**Admission is decided structurally, never by searching rendered prompt text.** Identities are threaded from
the renderer; the inliner reports an explicit outcome; the manifest confirms the wire form. There is no
substring match anywhere in the decision path — Rev 1 used one, and it was wrong: rendered text need not
contain identifiers (AC-3 says so outright), and identical text can recur across turns, so occurrence proves
neither identity nor provenance.

A recalled item's disposition is exactly one of:

| Disposition | Determined by |
|---|---|
| `admitted` | the renderer emitted its identity **and** the inliner reported `INLINED` **and** the target user message survived into the wire form |
| dropped · `budget_trimmed` | it is in `recall_candidates` but `memory_context` is `None` (gateway Phase 2 dropped it, `budget.py:270`) |
| dropped · `not_rendered` | it survived the budget but the renderer excluded it (blank description, rank cap, wrong type) |
| dropped · `absent_from_final_input` | it was rendered but the block never reached the wire form |

---

## 3. Files

### 3.1 NEW — `src/personal_agent/captains_log/turn_evidence.py`

Pure; no I/O; no `personal_agent` imports (so `request_gateway` can import it without a cycle).

```python
class EvidenceState(StrEnum):
    PRESENT = "present"            # the record applies and holds content
    EMPTY = "empty"                # the record applies and is legitimately empty
    NOT_RECORDED = "not_recorded"  # a capture gap — the state the contract exists to expose

class MemoryItemKind(StrEnum):  ENTITY | EPISODE | SESSION | SESSION_FACT | UNKNOWN
class DropReason(StrEnum):      BUDGET_TRIMMED | NOT_RENDERED | ABSENT_FROM_FINAL_INPUT

def memory_item_identity(item: Mapping[str, Any]) -> tuple[MemoryItemKind, str]
    # THE single definition of identity for a memory-context dict.
    # entity → name · episode → conversation_id/turn_id · session → session_id.
    # An unrecognised shape returns (UNKNOWN, "") — never a guessed identity.

class RecallCandidateRecord(BaseModel, frozen=True):  kind, identity, score: float | None,
                                                      injected: bool   # rendered by its own producer
class RecalledMemoryRecord(BaseModel, frozen=True):   kind, identity, score, admitted: bool,
                                                      drop_reason: DropReason | None
class RecallAdmissionRecord(BaseModel, frozen=True):  state, candidate_count, admitted_count,
                                                      items: list[RecalledMemoryRecord]
class ContextMessageRecord(BaseModel, frozen=True):   index, role, origin_trace_id: str | None,
                                                      timestamp: str | None, chars
class AssembledContextRecord(BaseModel, frozen=True): state, message_count, system_prompt_chars,
                                                      conversation_slice: list[ContextMessageRecord],
                                                      skill_bodies: list[str],
                                                      memory_identities: list[str]
class TurnEvidence(BaseModel, frozen=True):           recall, assembled_context,
                                                      primary_call_index, primary_call_count

def build_recall_candidates(memory_context, scores_by_identity) -> tuple[RecallCandidateRecord, ...]
def build_turn_evidence(*, candidates, memory_context_present, rendered_identities, inline_outcome,
                        wire_messages, system_prompt, skill_bodies, call_index) -> TurnEvidence
def derive_evidence_presence(...) -> dict[str, EvidenceState]
```

`derive_evidence_presence` covers **all eight** D3 records, honestly:

| key | `present` | `empty` | `not_recorded` |
|---|---|---|---|
| `user_message` | non-empty | empty string | — |
| `assistant_response` | non-empty | turn produced none | — |
| `reasoning_trace` | — | — | **always** — `TaskCapture` has no reasoning field, and item 3 is ticket-excluded. Marking it makes the gap machine-visible instead of implicit; that is the entire point of explicit absence |
| `tool_calls` | `tool_results` non-empty | turn called no tools | — |
| `recalled_memory` | ≥1 candidate | recall ran, found nothing | evidence builder never ran |
| `assembled_context` | record built | — | evidence builder never ran |
| `identifiers` | trace + session + user all set | — | any missing |
| `model_and_params` | ≥1 `llm_call` step (carries `model_role`, joins to `model_call_completed` on `trace_id`) | no model call made | — |

### 3.2 `request_gateway/types.py`
`AssembledContext` gains `recall_candidates: tuple[RecallCandidateRecord, ...] = ()`.

### 3.3 `request_gateway/context.py`
Build candidates for all Stage-6 paths and **stop discarding the proactive scores**: line 194 keeps returning
payloads unchanged (so nothing the model sees, and no token-estimate drift), with the scores captured into a
parallel map first.

**Session-fact recall is included** (codex finding, accepted). `context.py:327-337` injects recall-controller
facts as a system message, deliberately bypassing `memory_context` — so Rev 1 would have omitted a live,
model-visible recalled-fact producer from a record whose whole purpose is completeness. They enter as
`SESSION_FACT` candidates, identity `turn:<source_turn>`, score `RecallCandidate.confidence`, `injected=True`
for the ones actually written into the section. They are admitted by construction at call 0: `_trim_history`
preserves system messages (`budget.py:172-179`), and budget phases 2 and 3 drop `memory_context` and
`tool_definitions`, never messages — asserted by its own test rather than assumed.

### 3.4 `request_gateway/budget.py`
`apply_budget` carries `recall_candidates` through **unchanged** when it nulls `memory_context` — that is the
point: the candidate survives so the drop becomes recordable rather than invisible.

### 3.5 `orchestrator/skills.py`
Add `get_skill_bodies(message, loaded_skills) -> tuple[str, tuple[str, ...]]`; `get_skill_block` becomes a
one-line delegate returning only the text. Its signature and return type are unchanged, so ~30 existing test
call sites are untouched.

### 3.6 `orchestrator/types.py`
`ExecutionContext` gains `recall_candidates: tuple[RecallCandidateRecord, ...] = ()` and
`turn_evidence: TurnEvidence | None = None`.

### 3.7 `orchestrator/executor.py`
1. Add `_render_memory_section_with_ids(entity_items) -> tuple[str, tuple[str, ...]]`;
   `_render_memory_section` becomes the text-only delegate (one existing test keeps working).
2. Task-assist branch (`:4049`) collects the identities of the ≤3 items it renders.
3. Add `_inline_volatile_with_outcome(messages, block) -> tuple[list, InlineOutcome]` with
   `InlineOutcome ∈ {INLINED, EMPTY_BLOCK, ALREADY_WRAPPED, NO_TARGET}`;
   `_inline_volatile_into_last_user_message` becomes the messages-only delegate (~15 existing test call sites
   untouched).
4. Set `ctx.recall_candidates` at each of the three sites assigning `ctx.memory_context` — `:3128` (gateway →
   `gw.context.recall_candidates`), `:3391` (broad), `:3437` (entity-match, derived in place).
5. Capture skill-body names beside `_skill_bodies_text` (the model-decided path already has
   `sorted(ctx.loaded_skills)`; keyword/hybrid use `get_skill_bodies`).
6. **Admission point** — after `_prompt_identity` is built (`:4217`) and before `respond()`, when
   `ctx.tool_iteration_count == 0` and `ctx.turn_evidence is None`: compute the wire form per §2 and build
   `TurnEvidence`. Written once per turn; later calls do not overwrite it.
7. `TaskCapture(...)` at `:2393` gains the three new fields. `memory_context_used` /
   `memory_conversations_found` stay — other consumers read them, and retiring them is not this ticket.

### 3.8 `captains_log/capture.py`
`TaskCapture` gains `recall_admission: RecallAdmissionRecord | None = None`,
`assembled_context: AssembledContextRecord | None = None`,
`evidence_presence: dict[str, EvidenceState] = Field(default_factory=dict)`. All defaulted, so legacy on-disk
files and `TaskCapture(**data)` reads keep working. Serialization verified empirically this session: `orjson`
handles nested models and `StrEnum` under python-mode `model_dump()`, and the round-trip and missing-field
paths both hold.

### 3.9 `docker/elasticsearch/captains-captures-index-template.json`
Explicit properties for the new fields. The template is `dynamic: true`, and leaving these to dynamic mapping
is how the field-count walk happens (FRE-947's lesson). `recall_admission.items` and
`assembled_context.conversation_slice` as `nested`; identities `keyword`; `score` `float`; states `keyword`.
**Name check:** `assembled_context.conversation_slice`, *not* `context_messages` — that property already
exists in this template for the sub-agent doc shape (`:129-136`).

---

## 4. Tests (TDD — each written failing first)

`tests/personal_agent/captains_log/test_turn_evidence.py`
1. `memory_item_identity` over entity / episode / session / session-fact / unknown shapes.
2. **AC-3 core:** candidates ⊋ admitted under forced budget trimming → the recorded admitted set is
   **exactly** the admitted set, and every dropped item is present with `drop_reason == budget_trimmed`.
3. Rank cap (16 entities, 15 rendered) → the 16th is `not_rendered`, not missing.
4. Blank-description entity → `not_rendered` (the live silent drop at `:2112`).
5. `InlineOutcome.NO_TARGET` → rendered items are `absent_from_final_input`, never `admitted`.
6. Scores survive end to end and are non-`None` on the proactive path.
7. `assembled_context` names specific turns by `origin_trace_id` and specific skills by name, and asserts the
   record carries **at least one identifier that is not a member of `PROMPT_COMPONENT_TAXONOMY`** — the
   mechanical form of "resolves finer than the nine-category taxonomy".
8. Explicit absence: all eight keys always present; a no-tool turn is `empty` while an unbuilt record is
   `not_recorded`, and the two are never equal.

`tests/personal_agent/request_gateway/test_recall_candidates.py`
9. Stage 6 emits candidates with scores on the proactive path; `apply_budget` preserves `recall_candidates`
   while nulling `memory_context`.
10. Adding candidates does **not** change `AssembledContext.token_count` (no model-visible drift).
11. `_trim_history` preserves system messages — the invariant session-fact admission rests on.

`tests/personal_agent/orchestrator/test_turn_evidence_capture.py`
12. **Wire-form sync guard:** the executor's manifest input equals what the client builds — asserted for a
    clean history and for one with an orphaned tool call, so a future divergence in either client's
    pre-flight fails CI rather than silently falsifying the record.
13. End to end: `TaskCapture` carries a populated `recall_admission` + `assembled_context`, and the record
    describes call 0 only (`primary_call_index == 0`) even when the turn makes several primary calls.

`tests/personal_agent/orchestrator/test_skills.py` — one added test that `get_skill_bodies` returns names
matching the bodies in its text; existing `get_skill_block` tests untouched.

Commands: `make test-file FILE=tests/personal_agent/captains_log/test_turn_evidence.py` → then
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

---

## 5. Acceptance criteria → proof

| Criterion | Proof |
|---|---|
| ADR-0125 **AC-3** — identities of items admitted to the final serialized model input; not the candidate set, not a count | tests 2–5; admission resolved against the wire form defined in §2 |
| AC-3 — compared against the manifest, not rendered prompt text | no substring match exists in the decision path (Rev 2 change); test 12 pins the manifest to the real wire form |
| AC-3 — trimmed items distinguishable from used ones | tests 2–5, three distinct `DropReason` values plus `admitted` |
| AC-3 — scores recorded | test 6 (fixes the `context.py:194` discard) |
| **Item 6** — assembled context at item-identity granularity | test 7: conversation slice by `origin_trace_id`, skills by name |
| Item 6 — resolves finer than the nine-category taxonomy | test 7's non-membership assertion |
| **Explicit absence across all eight records** (AC-6's half) | test 8 |

---

## 6. Codex findings — disposition

| # | Finding | Disposition |
|---|---|---|
| 1 | `executor.py:4266` is not the wire form; both clients prepend + sanitise afterwards | **Accepted** — §2 rewritten; independently confirmed at `client.py:349-355` / `litellm_client.py:382-390` before the review returned. Sync guard added (test 12) |
| 2/3/4/5 | The verbatim fragment-presence check is not an identity manifest and is defeated by double-wrap refusal, compression, and sanitisation | **Accepted** — the substring check is removed entirely; admission is now structural end to end |
| 6 | First-admission recall + latest-call context describes two different model calls | **Accepted** — one record, one call (call 0). See §2 point 3 |
| 7 | Session-fact recall bypasses `memory_context` and was omitted | **Accepted** — folded in (§3.3) |
| 8 | Sub-agents can replay an earlier `<turn_context>` memory block | **Not in this PR** — `SubAgentCapture` is a separate durable surface (FRE-505) with its own admission point; a sub-agent call is not a turn. Genuinely sequenceable work → one Needs-Approval follow-up ticket, flagged to master |
| 9 | Reflection recall is another direct injection | **Not mine** — ADR-0125 D2/AC-2 removes that call site; owned by the AC-2 ticket, not duplicated here |
| 5 (budget) / 6 (serialization) | No defect found | Confirmed independently: sibling-field placement leaves `str(item)` accounting untouched, and the orjson/Pydantic round-trip was verified empirically |

## 7. Out of scope (deliberate)

- Evidence item 3 (reasoning trace) — ticket-excluded; recorded as `not_recorded` so the gap is explicit.
- The usage edge / supersession join — FRE-1005.
- AC-6's mechanical coverage and negative control — FRE-1006.
- FRE-1008 (defective prompt hashes) — untouched, and explicitly not leaned on.
- Retiring `memory_context_used` / `memory_conversations_found` — other consumers read them.

---

## 8. What the pre-PR self-review changed (Rev 3)

A `high`-effort code review plus a security review ran on the branch diff before the PR. Seven
findings were confirmed and fixed on-branch; each was a way the record could have been **false**,
which is the one defect class this ticket cannot ship with.

| # | Finding | Fix |
|---|---|---|
| 1 | **The default recall path produced anonymous episodes.** `proactive_memory_enabled` defaults to `False`, so the entity-name-match branch is the live default — and it dropped the adapter's `turn_id` when building the episode dict, so *every* episode resolved to identity `""`. AC-3 unmet on the default configuration | `context.py` now carries `conversation_id`; same gap fixed in the proactive payload builder (`memory/proactive.py`) |
| 2 | **Real scores were still being discarded** on that same path — `MemoryRecallResult.relevance_scores` is populated upstream and was thrown away, while the docstring claimed no score existed | scores threaded through; docstring corrected |
| 3 | **Colliding identities over-claimed admission.** `rendered` was a `set`, so with five anonymous episodes and three rendered, all five read as admitted — the render cap became invisible | `_resolve_admission` now consumes a `Counter`, admitting exactly as many as were rendered |
| 4 | **A prior turn's fence could stand in for this turn's.** The sanitiser can truncate back to an earlier user turn; that turn's own `<turn_context>` fence would then be read as proof this turn's block landed | the check is anchored on this turn's user message |
| 5 | **Skill bodies were listed even when the block never landed** — they ride the same volatile block as memory, so a vision turn (block-list content, which the inliner declines) would claim skills the model never saw | gated on the same fact as `memory_identities` |
| 6 | **An oversized entity name would reject the whole capture document.** Reproduced against a live index: a keyword term over Lucene's 32766-byte limit fails the *entire* doc, and the ES write path swallows the error — the turn's whole record lost, silently, while the disk copy survived | `ignore_above: 1024` on the identity keywords |
| 7 | **The evidence build double-emitted `history_sanitised`**, inflating a series documented as counting real dispatch rates | `emit_telemetry=False` for the observation; a test asserts real dispatches still emit |

Two tests were also strengthened rather than left as they were: the "finer than the taxonomy"
assertion was close to tautological (instance names and category labels are disjoint string spaces
by construction), so it now proves *resolution* — two memory items, two skills and two prior turns
each kept separate, which a category flag cannot express; and the `step_init` wiring that threads
Stage 6/7 candidates into the execution context had no test at all, so a regression there would have
left every other test green while the record silently went empty.
