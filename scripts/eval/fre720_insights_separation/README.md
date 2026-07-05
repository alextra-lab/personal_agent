# FRE-720 — insights-corpus separation probe (ADR-0105 D10 / AC-8)

Measurement gate: does the deployed embedder open a clean cosine floor between "same idea,
reworded" (positive) and "same category, genuinely distinct idea" (hard negative) real proposals
from the `agent-captains-reflections-*` corpus? The verdict decides whether FRE-721 (T7) ships
semantic (vector) dedup or falls back to explicit category+facet grouping.

Reuses the FRE-670/ADR-0103 separation-probe *instrument* — the pure cosine-separation statistics
in `scripts/eval/fre435_memory_recall/{separation_report,calibration}.py` — on a pairwise
proposal-vs-proposal shape rather than that harness's query-vs-entity recall shape.

## Layout

| File | Role |
|------|------|
| `corpus.yaml` | 35 real proposal texts (`entry_id -> {text, category, scope, timestamp}`), pulled verbatim from the live `agent-captains-reflections-*` index (2026-07-05), committed so the probe replays without live ES access |
| `pairs.yaml` | 25 positive (same idea, reworded) + 24 negative (same category + topical family, genuinely distinct — hard near-miss) labeled pairs, referencing `corpus.yaml` entry_ids |
| `probe_pairs.py` | Pure loader (`Corpus`, `PairCase`, `load_corpus`, `load_pair_set`) — no `personal_agent` import |
| `decision.py` | Pure D10 branch decision: `decide_branch(stats) -> "semantic" \| "fallback"` |
| `separation_probe.py` | The runner — embeds the corpus via the deployed production embedder, measures separation, writes the versioned artifact |
| `probe_result.json` | **The committed, versioned AC-8 artifact** — the actual measured result |

## Downstream contract (FRE-721 / T7)

`probe_result.json["decision"]` is the single field FRE-721 must consume and mechanically check
its shipped dedup branch against — see the FRE-720 plan
(`docs/superpowers/plans/2026-07-05-fre-720-separation-probe-insights-corpus.md`) for the full
contract.

## Run

```bash
uv run pytest tests/test_eval/test_fre720_separation_probe.py -v   # pure unit tests
PYTHONPATH=. uv run python scripts/eval/fre720_insights_separation/separation_probe.py  # live run
```

The live run touches no ES/Neo4j/Postgres substrate (the corpus is committed) but does call the
deployed embedder (`embeddings:8503`/`localhost:8503`).
