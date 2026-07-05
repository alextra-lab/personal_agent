# FRE-801 — Batch the FRE-772 Concept classifier + cache-stable prefix + optional System-class exclusion

**Linear:** FRE-801 (Approved, High, Tier-2:Sonnet) · **Backing:** ADR-0109 (V2 taxonomy), builds on FRE-772/FRE-800
**File under change:** `scripts/migrate_fre772_entity_type_v2.py` + `tests/scripts/test_migrate_fre772.py` + `tests/scripts/test_migrate_fre772_integration.py`

## Problem (measured baseline, master dry-run 2026-07-05)
3888 `Concept` nodes → one `gpt-5.4-mini` call each = 3888 calls/pass. Each call re-sends the ~280-token
five-type definition block → ~1.09M identical definition tokens (~59% of input) re-billed. Deterministic
Technology (1593) + Topic (1161) remaps make **zero** model calls.

## Design

### Part 1 + 2 — batch classifier with a byte-identical cache prefix

Replace the per-entity `Classifier = Callable[[str, str], Awaitable[ClassifyResult]]` with a **batch**
classifier:

```python
BatchClassifier = Callable[[Sequence[ConceptNode]], Awaitable[BatchClassifyResult]]

@dataclass(frozen=True)
class BatchClassifyResult:
    results: Sequence[ClassifyResult]   # exactly one per input node, in order
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
```

`ClassifyResult` unchanged (`entity_type: str | None`, `reason`). Per-entity cost is dropped (cost is a
batch property now).

**Cache-stable prompt** — split the current `_CLASSIFIER_TEMPLATE` (which interpolated `{name}`/`{description}`)
into a static module constant + a batch renderer:

```python
_CLASSIFIER_PREFIX = """<system-independent> definitions + numbered-output instruction ... Entities:\n"""

def _render_batch(nodes) -> str:
    return "\n".join(f"{i}. name: {n.name} | description: {n.description or '(none)'}"
                     for i, n in enumerate(nodes, 1))

def _build_batch_prompt(nodes) -> str:
    return _CLASSIFIER_PREFIX + _render_batch(nodes)   # prefix is ALWAYS byte-identical
```

`_CLASSIFIER_SYSTEM` stays static. The five type definitions move verbatim into `_CLASSIFIER_PREFIX`
(unchanged wording → same classification behaviour). **Caching is a provider-automatic behaviour, not a
code guarantee** (per codex review): the unit test proves only prefix *identity* (the invariant code can
guarantee); the dry-run's `cached_tokens` metric is what confirms the provider actually cached the prefix.

**Batch output parsing** — model instructed to output one numbered line per entity in order
(`<n>. <TypeName>`). **Strict numbered mapping, no positional fallback** (per codex review: a positional
fallback can silently misassign a type to the wrong entity — worse than a fail-closed retry):

```python
_LINE_RE = re.compile(r"^\s*(\d+)\s*[.):\-]\s*(.*)$")

def _parse_batch_classification(content, count) -> list[tuple[str | None, str]]:
    seen: dict[int, str | None] = {}
    dup: set[int] = set()
    for line in content.splitlines():
        if (m := _LINE_RE.match(line)):
            idx = int(m.group(1))
            if idx in seen:
                dup.add(idx)                       # duplicate index → cannot trust either
            seen[idx] = _parse_classification(m.group(2))
    # per index 1..count: duplicate → (None,"ambiguous_index"); missing → (None,"missing");
    #                     ambiguous/out-of-set line → (None,"out_of_set"); clean hit → (type,"")
```

A duplicate/missing/out-of-order/ambiguous index for entity *i* leaves node *i* `Concept` and reports it
unclassified — **never guesses, never misassigns**. Reuses `_parse_classification` (single-hit rule) per
line → identical fail-closed semantics (AC4). One bad line ⇒ that entity fails closed; the batch never
fails as a whole. A whole-batch numbering failure ⇒ every entity fails closed (safe; retried next run).

**Real classifier** `_build_llm_batch_classifier()` (renamed from `_build_llm_classifier`): one `respond()`
call per batch, `system_prompt=_CLASSIFIER_SYSTEM`, `messages=[{"role":"user","content":_build_batch_prompt(nodes)}]`,
carries the same `SystemTraceContext`/`entity_extraction` role. Extracts tokens from `response["usage"]`
(`prompt_tokens`/`completion_tokens`/`cache_read_input_tokens`) + `cost_usd`. Any exception → fail-closed
`BatchClassifyResult` (all-None results, reason `"error"`, zero cost/tokens) so one bad batch never aborts.

### Part 3 — optional System-class exclusion + class breakdown

`:Entity.class` is written by **no** code path today (only Claims/Stances carry class) → on prod it is
almost certainly unpopulated. The migration must verify at runtime and no-op safely.

Graph seam additions (all Cypher behind `GraphProtocol`):
- `count_by_type(*, exclude_system=False)` — adds `WHERE coalesce(e.class,'') <> 'System'` when set.
- `count_by_class() -> dict[str,int]` — `RETURN coalesce(e.class,'(unset)') AS c, count(*)`.
- `remap_deterministic(..., exclude_system=False)` — class filter on the write.
- `fetch_concepts(cursor, limit, *, exclude_system=False)` — class filter on the read.

`run_migration` new params: `classify_batch_size: int = 40`, `exclude_system: bool = False`.
Logic:
```
class_hist = await graph.count_by_class()
class_populated = any(k in {"World","Personal","System"} and v > 0 for k, v in class_hist.items())
effective_exclude = exclude_system and class_populated     # unpopulated ⇒ flag is a no-op
```
Deterministic + Concept paths pass `exclude_system=effective_exclude`. Dry-run deterministic count uses
`count_by_type(exclude_system=effective_exclude)`. Report records `class_histogram`, `class_populated`,
`exclude_system` (effective), and the summary prints the W/P/S breakdown and, when the flag was requested
but class is unpopulated, states it became a no-op.

### Report / metrics (AC1 evidence)
New `MigrationReport` fields: `model_calls`, `batch_count`, `input_tokens`, `output_tokens`,
`cached_tokens`, `class_histogram`, `class_populated`, `exclude_system`. `_print_summary` prints total
model calls, batch count, token totals (in/out/cached), cost, and the class breakdown.

### Classify loop
Page concepts by `batch_size` (DB page, 500) → chunk each page into `classify_batch_size` (40) groups →
`asyncio.gather` the batch calls under the existing `concurrency` semaphore → aggregate per-batch metrics,
then per-node apply set/mark exactly as today.

### CLI
Add `--classify-batch-size` (default 40) and `--exclude-system`. `_amain` calls
`_build_llm_batch_classifier` and threads the two new params.

## AC → proof map
| AC | Proof |
|----|-------|
| AC1 batching cuts calls; report prints calls/batches/tokens/cost | unit `test_batching_reduces_calls` (batch_count << concept_total) + `test_report_aggregates_batch_metrics`; live dry-run (master runbook) |
| AC2 static prefix identical across batches | unit `test_batch_prompt_prefix_is_stable` |
| AC3 W/P/S breakdown; exclude-system untouches System when populated; no-op + stated when unpopulated | unit `test_class_histogram_in_report`, `test_exclude_system_skips_system_when_populated`, `test_exclude_system_is_noop_when_unpopulated` |
| AC4 no change to deterministic remap / snapshot / rollback / fail-closed | existing tests retained (adapted to batch fake) + `test_class_property_is_never_touched` |

## Test plan (TDD — failing first)
1. Rewrite fake `_classifier` → `_batch_classifier(mapping, *, cost, input_tokens, output_tokens)` returning `BatchClassifyResult`.
2. Adapt existing tests (deterministic/happy/fail-closed/idempotent/dry-run/paging/rollback/class-untouched) to the batch fake — assertions unchanged.
3. Add new tests:
   - `test_batch_prompt_prefix_is_stable` (AC2) — two different batches share a byte-identical prefix.
   - `test_parse_batch_classification_numbered` — clean 1..N mapping.
   - `test_parse_batch_duplicate_index_fails_closed` — duplicate index → both entities fail closed (`ambiguous_index`), never misassigned.
   - `test_parse_batch_missing_and_out_of_order` — missing index → `missing`; out-of-order numbers still map by index; ambiguous line → `out_of_set`.
   - `test_partial_batch_failure_one_bad_entity` — one unparseable entity stays `Concept`, the rest classify; batch never fails whole.
   - `test_batching_reduces_calls` (AC1) — `batch_count`/`model_calls` << `concept_total`.
   - `test_report_aggregates_batch_metrics` (AC1) — cost + input/output/cached tokens summed from `BatchClassifyResult`.
   - `test_class_histogram_in_report` (AC3) — `class_histogram` populated.
   - `test_exclude_system_skips_system_when_populated` (AC3) — System nodes untouched, `report.exclude_system` True.
   - `test_exclude_system_is_noop_when_unpopulated` (AC3) — effective flag False, all nodes processed, summary states no-op.
   - `test_cli_parses_new_flags` — `_parse_args` exposes `--classify-batch-size`/`--exclude-system`.
   - `test_summary_prints_noop_message` (capsys) — the printed no-op line is present when class is unpopulated + flag requested.
4. Update integration test classifier to batch form; add an exclude-system assertion (class seeded `World`).
5. Update `_amain` cost-gate regression test fake to `_build_llm_batch_classifier` and assert it threads the new params.

## Commands
- `make test-file FILE=tests/scripts/test_migrate_fre772.py`
- `make test` (full) · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`

## Runbook (for master — post-merge, do NOT deploy from build)
Dry-run against prod (cost-gated, read-only): `uv run python scripts/migrate_fre772_entity_type_v2.py --dry-run --confirm-prod`
→ expect non-zero classifications, `model_calls`/`batch_count` << 3888, token totals + cost printed, and the
class breakdown (likely all `(unset)` ⇒ `--exclude-system` reported as a no-op). Owner decides exclude-system at the dry-run.

## Out of scope / gotchas
- Does **not** run the migration — build stops at PR; master owns the dry-run + real run (migration held per FRE-801 body).
- No change to the FRE-793 preflight, snapshot/rollback Cypher, or the prod-write env guard.
