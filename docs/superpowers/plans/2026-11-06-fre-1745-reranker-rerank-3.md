# FRE-1745 — Switch the reranker to Voyage rerank-3, with new cut-offs measured on it

Backing design: ADR-0148 D4 (a bound belongs to the model that produced the scores).
Diff class: escalated (behaviour config and a paid vendor).

## Done before this plan (paid step, go recorded on the ticket 2026-11-06)

* `RERANKER_ARMS["voyage-rerank-3"]` added; `INDEPENDENT_ARM` in the FRE-1479 harness points at it.
* `config/models.yaml` `reranker.id` is `rerank-3`.
* FRE-1479 harness run once on rerank-3: bound 0.344176, 93.3% of 119 positives admitted,
  negative median 0.343176. It wrote `broad_recall_relevance_bound.json`.
  `entity_match_relevance_bound.json` is the same run (FRE-1480: same core, identical scores;
  master accepted the reuse) and carries a `reuse_note`.
* FRE-1545 harness run once on rerank-3: bound 0.344176, 93.6% of 110 positives admitted,
  negative median 0.343176. It wrote `proactive_rerank_relevance_bound.json`.

## Steps

1. **Settings defaults.** In `src/personal_agent/config/settings.py` set
   `broad_recall_relevance_bound`, `entity_match_relevance_bound` and
   `proactive_memory_rerank_relevance_bound` to `0.344176`. Update the three measurement
   comments (model, date, bound, shares). Verify:
   `uv run pytest tests/personal_agent/config -q -k "bound_calibration"` passes.
2. **Config inventory.** Update the three default values in `docs/reference/CONFIG_INVENTORY.md`
   (regenerate with the repo's generator if one exists). Verify: the inventory test passes.
3. **Docstrings.** `src/personal_agent/memory/reranker.py` module and `rerank()` docstrings name
   rerank-3 as the primary. Verify: `make ruff-check`.
4. **Tests (TDD).** Before steps 1 to 2, write these tests and watch them fail:
   * `tests/personal_agent/config/test_broad_recall_bound_calibration.py`: each of the three
     committed artifacts names the model that the `reranker` role resolves to, and each
     setting default equals its artifact bound. Fails now because the defaults are 0.338891.
   * Gate tests (`test_broad_recall_relevance.py`, `test_entity_match_relevance.py`,
     `test_proactive_rerank_gate.py`): `SERVING` becomes `rerank-3`. Add one case per gate:
     a score that carries `rerank-2.5` is not compared with a bound calibrated on `rerank-3`
     (AC-3: a bound is never applied to a score from another model).
   * Keep the existing fallback-model case.
5. **Gates.** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. `config_guard` passes (run its CLI/test as CI runs it).
6. **Docs.** Add the measurement results to a short research note
   `docs/research/2026-11-06-fre-1745-rerank-3-calibration.md` (per-artifact table, rerank-2.5
   beside rerank-3, spend, the reuse note, rollback = old binding plus old artifacts in git).
7. **Self-review and PR** per the build skill. `/code-review high` on master's order.

## Risks

* Rollback needs both the binding and the three artifacts. `config_guard` fails if only one is
  reverted, which is the intended safety net.
* The 0.005 bound shift can change admitted memories per recall; AC-5 (master, after deploy)
  watches this.
