# FRE-1476 — Memory-context status: per-stage outcomes, four states, precedence

**Ticket:** FRE-1476 (Approved, `stream:build1`, `Tier-1:Opus`)
**ADR:** ADR-0148 D1, D2, D3 — `docs/architecture_decisions/ADR-0148-absence-must-be-reachable-and-sayable.md`
**Successor:** FRE-1478 renders the status. This ticket renders nothing.
**Siblings on the same ADR:** FRE-1477 (merged), FRE-1479 (in progress, `memory/service.py` reranker
score), FRE-1480 (entity-match relevance value). D4 belongs to those three, not to this ticket.

---

## Scope

In:

1. The vocabulary — four states on one axis, plus the per-stage report types.
2. The composition rule — one total precedence, one place.
3. The recall-layer scoping — standing behavioural stances never raise the status; enrichment
   stances inherit.
4. The producing paths in `request_gateway/context.py` report what they did.
5. The budget stage in `request_gateway/budget.py` reports its drop.
6. The path-failure causes that are discarded one stack frame down today, so the report is not
   inert: the dense arm's swallowed embed failure, the proactive adapter's swallowed exception, and
   the multi-path core's existing `arms_failed` reaching the three protocol result types.

Out, and stated so master can check the boundary:

* Rendering the state line, and the renderer's own drop report (FRE-1478).
* Every D4 relevance predicate and bound (FRE-1477, FRE-1479, FRE-1480).
* The turn-evidence record's full cause axis (ADR-0148 names it; no ticket in this chain claims it).
* `memory_context` keeps its list shape. ADR-0148's own risk table states *"the digest reads the
  items, and the status is additive"*, so the status arrives as a sibling field on
  `AssembledContext`, following the `recall_candidates` / `candidate_population` precedent
  (FRE-1004, FRE-1060). Changing the field's type would ripple into `executor.py`, the ADR-0147
  digest and `turn_evidence.py` for no gain this ticket needs.

---

## Design

### The four states, and the one rule that produces them

New module `src/personal_agent/request_gateway/memory_status.py`.

```python
class MemoryStatus(StrEnum):
    POPULATED = "populated"
    NOTHING_RELEVANT = "nothing_relevant"
    WITHHELD = "withheld"
    UNAVAILABLE = "unavailable"


class RecallOutcome(StrEnum):
    NOT_REPORTED = "not_reported"   # D3 default — the weaker claim
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class RecallStageReport:
    outcome: RecallOutcome = RecallOutcome.NOT_REPORTED
    cause: str | None = None


@dataclass(frozen=True)
class RenderStageReport:
    ran: bool = False
    recall_emitted: int = 0   # recall-derived lines only, never behavioural stances
    cause: str | None = None


@dataclass(frozen=True)
class RecallAdmission:
    with_content_renderable: int = 0
    with_content_unsupported_kind: int = 0
    contentless: int = 0

    @property
    def admitted(self) -> int:
        return self.with_content_renderable + self.with_content_unsupported_kind


@dataclass(frozen=True)
class MemoryStatusReport:
    recall: RecallStageReport = RecallStageReport()
    admission: RecallAdmission = RecallAdmission()
    budget_dropped_recall_items: bool = False
    render: RenderStageReport = RenderStageReport()

    @property
    def status(self) -> MemoryStatus: ...
```

`status` is a property, not a stored field, so the composed value cannot drift from the stage
reports it is composed of. The budget stage rebuilds the report with one flag flipped and the status
follows.

### Precedence, in the order the ADR fixes

```
1. recall.outcome is not COMPLETED           -> UNAVAILABLE
2. admission.admitted == 0                   -> NOTHING_RELEVANT
3. budget_dropped_recall_items               -> WITHHELD
4. render.ran and render.recall_emitted == 0  -> WITHHELD
5. admission.with_content_renderable == 0    -> WITHHELD
6. otherwise                                 -> POPULATED
```

Rule 1 covers both D3's default and every failure, so an item count can never raise the status.

**Rules 4 to 6, and a renderer that has not reported.** Rule 6 returns `POPULATED` when the renderer
has not run. This is required, not overlooked: AC-4's third case asserts that *"the renderer drops
every item"* yields `WITHHELD`, which discriminates only if a renderer that has **not** dropped
everything yields something else. Treating an unreported renderer as a failure would compose
`WITHHELD` on every turn and make that case untestable. The status is therefore the composition of
the stages that have reported, and `render.ran` shows a reader whether the renderer has spoken.
FRE-1478 supplies the report and the status becomes final at the render.

**One deviation from ADR-0148's declared imprecision, and why it is not the out-of-scope work.** D2
declares that the budget drop at `budget.py:276` discards the raw context *"before any renderability
classification exists"*, so it reports `WITHHELD` even when every dropped item was contentless — and
the ADR names `NOTHING_RELEVANT` as *"the truer state"*. The ADR's out-of-scope item is
classifying **renderability** before the budget stage. This plan does not do that. It classifies
**content**, which AC-6 requires anyway, and content classification is what removes the overclaim:
rule 2 sits above rule 3, so a dropped context of contentless items reaches the truer state. A
dropped context of content-bearing unrenderable items is still `WITHHELD`, which is correct.

### Recall-layer scoping

`admission` counts recall-layer items only. Kinds come from
`captains_log.turn_evidence.memory_item_identity`, the single definition of item identity, rather
than from a second reading of `item["type"]`.

| Kind | Counts toward admission | Why |
|---|---|---|
| `ENTITY`, `EPISODE`, `SESSION` | Yes | The recall layer produced them. |
| `STANCE` | Yes, but only when its target is an entity present in the same context | Enrichment inherits, it never qualifies independently (D4). Its target came from recall, so it is recall-derived content — but a stance with no parent in the context is not admitted at all. |
| `BEHAVIOURAL_STANCE` | No | The standing layer, outside the recall layer entirely (D2). |
| `SESSION_FACT`, `UNKNOWN` | No | No live producer emits them. |

**Why `STANCE` counts (codex plan-review finding, verified).** An earlier revision excluded stances
outright on the argument that "the parent is already counted". That is false for a contentless
parent. `_enrich_with_stances` selects targets from every recalled entity name without reading the
entity's description (`context.py:347`), and the renderer filters blank descriptions but renders any
stance with a non-blank `affect` (`executor.py:3730`, `:3737`). So a blank-description entity with a
real stance emits no entity line and one stance line: recall-derived content reaches the model while
the excluding rule would have composed `NOTHING_RELEVANT`. Requiring the parent to be present keeps
D4's inheritance rule intact and the ticket's AC-3 true — a stance whose parent recall rejected
cannot exist, and a synthetic parentless stance never raises the status.

Content, matching the renderer's own filters at `executor.py:3730-3733`:

* entity — `description` non-blank after strip.
* episode — `summary` or `user_message` non-blank after strip.
* session — `summary` non-blank after strip.

Renderable kinds, matching `_render_memory_section_with_ids`: `ENTITY` and `EPISODE`. `SESSION` is
an explicit FRE-1010 non-goal, so a session item with real content is admitted, is unsupported, and
is therefore `WITHHELD` — exactly the ticket's AC-6 second half.

### Where each stage reports

| Stage | File | Report |
|---|---|---|
| Producing paths | `request_gateway/context.py` | `RecallStageReport` per turn |
| Budget | `request_gateway/budget.py` | `budget_dropped_recall_items` |
| Renderer | `orchestrator/executor.py` | `RenderStageReport` — FRE-1478 populates it |

`RenderStageReport` has no producer in this ticket. It is not speculative: the ticket's AC-4 names
*"every arm completes but the renderer drops every item"* as one of the five precedence cases, so the
composition must accept the input to be testable at all.

### Making the report non-inert

Causes are known one stack frame down and discarded. Codex verified nine such collapses across
`memory/service.py`. This ticket fixes the ones on the chain it already touches and files the rest,
rather than rewriting the arm layer inside a vocabulary ticket.

**Fixed here:**

1. **The zero vector is the real embedder-failure seam** (codex finding, verified).
   `generate_embedding` catches every provider exception and returns a zero vector rather than
   raising (`memory/embeddings.py:238-250`), and `_dense_vector_search_ranked` short-circuits a zero
   vector to `[]` (`memory/service.py:5020`). So a strict dense arm alone would never see an
   ordinary embedder failure. `dense_recall_arm` therefore tests the returned embedding: inside the
   arm, blank query text is already short-circuited before embedding, so a zero vector there means
   the embedder failed and nothing else. That failure is reported.
2. **`dense_recall_arm`'s swallowed raises.** Split it: a strict inner method that raises, and the
   public method that keeps its documented fail-open contract for its direct callers (all of which
   are tests of that contract). `_multipath_fused_recall` calls the strict one, and its existing
   `return_exceptions=True` gather records the arm in `arms_failed`. No new failure machinery.
3. **`MultiPathRecallResult.arms_failed`** already exists and reaches nobody. Carry it:
   * broad — `_multipath_broad_entities` returns `(entities, arms_failed)`; `query_memory_broad`
     puts `arms_failed` in its result dict, and its own outer `except` (`service.py:6061`) and the
     fused-resolution `except` (`service.py:5388`) record themselves there too; the adapter maps it
     onto `BroadRecallResult`.
   * entity — `_multipath_query_memory` sets `arms_failed` on `MemoryQueryResult`, including its
     fused-resolution `except` (`service.py:5650`); the adapter maps it onto `MemoryRecallResult`.
4. **`MemoryServiceAdapter.suggest_relevant`** swallows every exception and returns empty
   candidates, which is indistinguishable from honest absence. `ProactiveMemorySuggestions` gains
   `failed` and `failure_cause`; the `except` block and the zero-embedding early return set them.

All new fields are additive with empty or false defaults, so no existing construction site breaks.

**Filed, not fixed here** — one follow-up ticket covering the remaining verified collapses, each of
which converts a failure into an empty success below the seam this ticket reads: the lexical arm
(`service.py:4955`), the structural arm (`:4765`, `:4891`), the multi-query arm's per-variant and
paraphrase failures (`:5735`, `:5755`), `suggest_proactive_raw` (`:996`, which the adapter then
labels `no_raw_rows`), the non-multipath `query_memory` path (`:4658`), and
`resolve_message_entities` in the adapter (`protocol_adapter.py:264`, whose `[]` `context.py:483`
reads as "no names"). These are arm-layer and adapter-layer work, they are sequenceable, and
FRE-1479 is rewriting part of the same file right now.

---

## Steps

Each step names its verification.

1. **`memory_status.py`** — the module above, with the precedence property.
   Verify: `pytest tests/personal_agent/request_gateway/test_memory_status.py` — the six ACs.
2. **`types.py`** — `AssembledContext` gains
   `memory_status: MemoryStatusReport = MemoryStatusReport()`. The default composes to
   `UNAVAILABLE` (D3 at the construction site, the `candidate_population` pattern).
   Verify: `make mypy`; existing `test_types.py` passes untouched.
3. **Protocol outcome fields** — `BroadRecallResult.arms_failed`, `MemoryRecallResult.arms_failed`
   (`memory/protocol.py`), `ProactiveMemorySuggestions.failed` + `failure_cause`
   (`memory/proactive_types.py`).
   Verify: `make mypy`; `make test-file FILE=tests/personal_agent/memory/test_protocol_adapter.py`.
4. **`memory/service.py`** — the zero-vector failure test in the dense arm, the strict/fail-open
   split, and `arms_failed` carried on both fused paths.
   Verify: a unit test that `generate_embedding` returning a **zero vector** — the real provider
   failure shape — puts `"dense"` in `arms_failed` while the lexical arm still returns items. A
   second case drives a raising `generate_embedding` for the same result.
5. **`memory/protocol_adapter.py`** — map the three outcomes onto the protocol results.
   Verify: adapter tests.
6. **`request_gateway/context.py`** — `_query_memory_for_intent` returns a `RecallStageReport` at
   every exit; `assemble_context` classifies the recall layer and sets `memory_status`.
   Verify: `pytest tests/personal_agent/request_gateway/test_context.py`.
7. **`request_gateway/budget.py`** — the Phase-2 drop rebuilds the report with
   `budget_dropped_recall_items=True` when the dropped context held at least one recall-layer item.
   Verify: `pytest tests/personal_agent/request_gateway/test_budget.py`.
8. **Gates** — `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`.

---

## Acceptance criteria — how each is proved

| AC | Test | Asserted value |
|---|---|---|
| AC-1 — no status reported yields `UNAVAILABLE` | `test_unreported_path_with_items_is_unavailable` | A path returning three content-bearing entities and `RecallStageReport()` composes exactly `UNAVAILABLE`. A companion assertion sets `COMPLETED` on the same fixture and gets `POPULATED`, so the test cannot pass for the wrong reason. |
| AC-2 — standing stances never raise the status | `test_behavioural_stances_do_not_defeat_absence` | `assemble_context` with a fake adapter whose recall admits nothing and whose `get_current_stances` returns curated stances: status exactly `NOTHING_RELEVANT`, and `memory_context` still holds the behavioural items. Fails on today's code by construction. |
| AC-3 — enrichment inherits, never qualifies | `test_enrichment_stance_neither_survives_nor_raises` | A parentless `stance` item composes `NOTHING_RELEVANT`, never `POPULATED` — the discriminating half, since the "a rejected entity produces no stance" half already holds today (codex finding). A companion case: a stance whose parent entity is present but blank-described composes `POPULATED`, because that stance line does reach the model. |
| AC-4 — precedence resolves every mixed case | `test_precedence_resolves_each_mixed_case` | Five cases, each asserting one exact state: partial arm failure -> `UNAVAILABLE`; budget drop of a partial context -> `WITHHELD`; renderer drops all -> `WITHHELD`; stances injected with empty recall -> `NOTHING_RELEVANT`; memory unwired with stances -> `UNAVAILABLE`. |
| AC-5 — a partial failure never yields absence | `test_partial_arm_failure_is_unavailable` | `recall_broad` reports `arms_failed=("dense",)` and returns entities: status exactly `UNAVAILABLE`. Plus the service-level test from step 4, so the report is real and not fixture-only. |
| AC-6 — contentless is not admitted, unsupported content is | `test_contentless_is_absence_and_unsupported_content_is_withheld` | Blank-description entities only -> `NOTHING_RELEVANT`. Session items with real summaries only -> `WITHHELD`. |

---

## Risks

| Risk | Handling |
|---|---|
| FRE-1479 is in progress on `memory/service.py` and `context.py` | Changes here are additive and sit in different functions (`dense_recall_arm`, the two fused wrappers, `_query_memory_for_intent`'s return). Whoever merges second rebases. |
| The renderable-kind set drifts from the renderer | The set is derived from `_render_memory_section_with_ids` and cited in a comment. FRE-1478 replaces the inference with the renderer's own report. |
| A construction site forgets the status | The field defaults to `UNAVAILABLE`, which is D3's rule applied at the construction site. |

## Observations found during review, not fixed here

* **Live broad-recall session items carry no summary at all.**
  `_format_broad_recall_context` reads `session["session_summary"]` (`context.py:163`), but
  `query_memory_broad`'s session query returns only `session_id`, `dominant_entities`, `turn_count`
  and `started_at` (`service.py:5981-5990`). Every live broad session item therefore has
  `summary=None`. Under the content predicate they are contentless, which is the truthful reading of
  what they hold. AC-6's session case is a composed shape the type supports rather than one this
  path emits today. Reported, not fixed — it is a producer defect unrelated to this ticket.
