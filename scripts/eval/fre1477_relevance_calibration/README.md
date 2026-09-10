# FRE-1477 — proactive relevance-bound calibration (ADR-0148 D4)

Measures the proactive path's relevance bound against the **serving** embedder arm, validates the
instrument, and commits its answer — or reports that no bound satisfies ADR-0148 D4 and stops.

Result of the 2026-09-10 run: **no admissible bound on `Qwen3-Embedding-8B` @ 1024.** See
`docs/research/2026-09-10-fre-1477-proactive-relevance-calibration.md`.

## Run

```bash
make test-infra-up    # Neo4j :7688 / ES :9201 / Postgres :5433
uv run python -m scripts.eval.fre1477_relevance_calibration.calibrate --run-id cal-$(date +%Y%m%d)
```

`--dry-run` reports the corpus shape without embedding anything. `--artifact <path>` writes
elsewhere than the committed default.

Credentials come from `pass` at run time (`seshat/AGENT_OVH_AI_BASE_URL`,
`seshat/AGENT_MANAGED_EMBEDDING_TOKEN`) and are never persisted or logged. Roughly 200 embedding
calls per run.

## What it measures, and why that

The proactive path admits on `vector_score`, which is what `db.index.vector.queryNodes` returns
(`memory/service.py:1000-1016`). So the calibration drives the production write and query path over
a labelled probe set, rather than approximating it offline.

The metric is FRE-694's: per expected entity a positive, per query the **strongest non-match** as
the negative. On a query whose answer is not in the corpus, the nearest neighbour the index returns
*is* the strongest non-match — the item admission is actually decided on.

Both score populations are committed whole. AC-5's denominator is every labelled positive, and a
stored share can be re-cut after the fact.

## The bound

Two constraints (ADR-0148 D4): strictly above the median top-ranked non-match, and admitting at
least 90% of the whole labelled positive set. Admitted share falls monotonically as the bound rises,
so the lowest bound rejecting the median is also the most permissive — one candidate settles it. If
that candidate fails the positive constraint, no bound satisfies both, and the harness **reports the
incompatibility and stops**. It never picks a number: that is a finding for the owner, and the
response is a reranker-side bound or a different arm.

The committed bound is in **normalized embedding-term space** (`max(0, 2x − 1)`), because that is
the value the proactive path scores on. The artifact carries the Neo4j-space value too.

## The parity gate

FRE-694's literal reference is a 0.6B measurement and no 0.6B endpoint serves, so the structure
transfers rather than the constant. Both checks run at FRE-694's own 0.02 tolerance, and both must
pass before any number is reported.

| Check | What it compares | What it catches |
|---|---|---|
| **P1** | The three FRE-694 aggregates, Neo4j index vs `fre817_corpus_ab_embedder`'s independently authored offline pipeline | Shared corpus, formatting, query-mode or aggregation error — what one pipeline compared against itself cannot see |
| **P2** | Per candidate, the index's score vs a client-side cosine over the vectors the index holds | Index infidelity — the gap FRE-694 and FRE-817 both recorded for this arm |

**When adding an arm or a probe set, read the direction of the P1 deltas, not only the pass bit.**
The first 2026-09-10 run passed P1 at 0.0147 / 0.0198 / 0.0157 while comparing 4096-dimension
offline vectors against 1024-dimension index vectors — a real error, inside tolerance, revealed only
because all three leaned the same way. A consistent offset is a bias. Deltas after the fix:
0.0107 / 0.0086 / 0.0015.

## Layout

| File | Role |
|---|---|
| `analysis.py` | Pure decisions — `choose_bound`, `parity_aggregates`, `aggregate_deltas`, `pair_deltas`. No substrate, no `personal_agent` import. |
| `calibrate.py` | The I/O driver: pins the test substrate, points the embedding client at the serving arm, seeds, measures, validates, writes the artifact. |

Tests: `tests/test_eval/test_fre1477_calibration_analysis.py` (the decisions, including the seeded
incompatible case) and `tests/personal_agent/config/test_relevance_bound_calibration.py` (the
artifact and the `config_guard` binding).
