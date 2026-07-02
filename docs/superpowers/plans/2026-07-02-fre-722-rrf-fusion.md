# FRE-722 — RRF fusion combiner and cross-path dedup

**Linear:** FRE-722 (Approved) · **Backing ADR:** ADR-0104 (Multi-Path Retrieval with Rank Fusion,
Proposed), AC-2 · **Spec:** `docs/specs/MULTI_PATH_RETRIEVAL_DESIGN_SPEC.md` §3, §4, §8 (Build 1)

## Scope

A new pure module, `src/personal_agent/memory/fusion.py`, holding two functions:

1. **Cross-path dedup** — collapse repeated items *within* a single arm's ranked list to their
   best (lowest) rank, keyed by canonical identity (`item_id` — caller supplies `Turn.turn_id` for
   turns or the `Entity` elementId for entities; never a free-text name).
2. **Reciprocal Rank Fusion** — combine arms' (deduped) ranked lists by rank position:
   `score(item) = Σ_arms 1 / (k + rank_arm(item))`, `k = 60` default (spec §3.2). Never combines by
   raw score. Output has exactly one entry per `item_id` (deduped by construction — spec §4).

No substrate, no config wiring, no feature flag — this ticket is the pure-function layer only
(spec §8 Build 1). `k` is a function parameter defaulting to 60, not read from `settings` (config
wiring is Build 3 / FRE-724's job, per spec §3.2 "exposed as `multipath_rrf_k`").

## Acceptance criteria (ADR-0104 AC-2, ticket-stated)

1. An item ranked *r* by two arms outranks an item ranked *r* by one arm (agreement property).
2. An item ranked highly by one arm but absent from the others does not automatically outrank an
   item with broad multi-arm support.
3. Dedup collapses the same canonical id across arms into one fused entry; never merges two
   distinct ids (even if names look related, e.g. "vision" vs "perception" entity nodes).
4. Within one arm, a repeated item keeps its best (lowest) rank.

## Design

```python
# src/personal_agent/memory/fusion.py
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class RankedResult:
    """One arm's ranked hit for a single item.

    Args:
        item_id: Canonical identity — Turn.turn_id for turns, Entity elementId for
            entities. Never a free-text name.
        rank: 1-based rank position within the arm's ranked list.

    Raises:
        ValueError: If rank < 1.
    """

    item_id: str
    rank: int

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError(f"rank must be >= 1, got {self.rank}")


@dataclass(frozen=True)
class FusedResult:
    """One item's fused position across arms.

    Args:
        item_id: Canonical identity, deduped by construction — appears once.
        score: Summed RRF score across every arm that surfaced this item.
        arm_count: Number of distinct arms that surfaced this item (the agreement count).
    """

    item_id: str
    score: float
    arm_count: int


def dedup_arm_ranking(results: Sequence[RankedResult]) -> list[RankedResult]:
    """Collapse repeated items within one arm's ranked list to their best rank.

    Output order is first-occurrence order of each distinct item_id in the input
    (stable, deterministic) — not re-sorted by the (possibly improved) kept rank.

    Args:
        results: One arm's ranked hits, possibly containing the same item_id more
            than once.

    Returns:
        One RankedResult per distinct item_id, at its lowest (best) observed rank.
    """
    ...


def reciprocal_rank_fusion(
    arm_rankings: Sequence[Sequence[RankedResult]],
    k: int = DEFAULT_RRF_K,
) -> list[FusedResult]:
    """Fuse several arms' ranked lists by Reciprocal Rank Fusion.

    Each arm is first deduped to its best rank per item (dedup_arm_ranking), then RRF
    sums 1 / (k + rank) across arms, keyed by item_id. Fusion never compares raw arm
    scores — arm score scales are not comparable (FRE-695).

    Args:
        arm_rankings: One ranked result list per retrieval arm.
        k: RRF constant. Defaults to 60 (Cormack et al. 2009). Must be >= 0.

    Returns:
        Fused ranking, one FusedResult per distinct item_id. Sorted by descending
        score; ties broken by ascending item_id for deterministic output.

    Raises:
        ValueError: If k < 0.
    """
    ...
```

Both functions are pure (no I/O, no logging, no Cypher) — ADR-0074 identity threading does not
apply.

**Fixes from codex plan-review (2026-07-02):**
- `RankedResult.__post_init__` rejects `rank < 1` (`ValueError`) — closes the invalid-rank gap.
- `reciprocal_rank_fusion` rejects `k < 0` (`ValueError`) — closes the invalid-k gap.
- Fused output tie-break is `(-score, item_id)` — deterministic when two items score equal.
- `dedup_arm_ranking` output order is documented: first-occurrence order, not re-sorted.
- `arm_count` on `FusedResult` is noted as derived observability metadata, not part of the
  identity/ranking contract in spec §4.

## Steps

1. **Write failing tests** — `tests/personal_agent/memory/test_fusion.py`:
   - `dedup_arm_ranking`: repeated `item_id` within one arm keeps lowest rank; no repeats → passes
     through unchanged; empty input → `[]`.
   - `reciprocal_rank_fusion`:
     - agreement property — item at rank 3 in two arms outranks an item at rank 3 in one arm.
     - broad-support beats a single top rank — an item at rank 10/8/12 across three arms outranks
       an item at rank 1 in one arm alone.
     - dedup-by-construction — an item present in two arms yields exactly one `FusedResult`, with
       `arm_count == 2` and score summed from both arms.
     - distinct ids never merge — two different `item_id`s (e.g. two different entity elementIds)
       remain two separate `FusedResult`s even at identical ranks.
     - within-arm repeat is deduped before fusion (best rank used in the RRF sum, not summed twice).
     - `k` default is 60 — assert the exact score for a single-arm, rank-1 item is `1/61`.
     - custom `k` changes the score — e.g. `k=0` on a rank-1 item gives score `1.0`.
     - empty `arm_rankings` → `[]`.
     - `rank < 1` raises `ValueError` (via `RankedResult.__post_init__`).
     - `k < 0` raises `ValueError` (via `reciprocal_rank_fusion`).
     - equal-score tie-break is deterministic — two items with identical scores sort by
       ascending `item_id`.
   - Run: `make test-file FILE=tests/personal_agent/memory/test_fusion.py` — confirm **collection
     fails** (module doesn't exist yet).
2. **Implement** `src/personal_agent/memory/fusion.py` per the design above.
3. **Run tests green**: `make test-file FILE=tests/personal_agent/memory/test_fusion.py` — all pass.
4. **Quality gates**: `make mypy` · `make ruff-check` · `make ruff-format` · `make test` (full
   suite) · `pre-commit run --all-files`.
5. **No follow-up tickets expected** — this is a self-contained pure-function slice; Build 2
   (FRE-723) and Build 3 (FRE-724) are already filed.
6. **No docs to update** — the spec (`docs/specs/MULTI_PATH_RETRIEVAL_DESIGN_SPEC.md`) already names
   FRE-722 as Build 1 in §8; no further doc changes required by this ticket's scope.

## Test commands (exact)

```bash
make test-file FILE=tests/personal_agent/memory/test_fusion.py
make mypy
make ruff-check
make ruff-format
make test
pre-commit run --all-files
```
