# Memory Integration Probe

Read-only probes that measure how concatenative vs integrative the current
memory pipeline is on the live VPS Neo4j data.

See `docs/superpowers/plans/2026-05-21-memory-integration-probe.md` for
context.

## Scripts

| Script | What it measures |
|---|---|
| `probe_1_entity_drift.py` | Top entities by source-turn count; the stored description vs sample turn texts. Demonstrates the first-write-loses overwrite semantics in `service.py:605`. |
| `probe_2_redundant_edges.py` | Entity pairs that accumulate multiple relationship types (RELATED_TO + SIMILAR_TO + USES …). |
| `probe_3_co_retrieval_duplicates.py` | Near-duplicate entity names within the co-retrieval neighborhood of each top entity (structural proxy for retrieval-payload duplication). |

## Running

```bash
cd /opt/seshat
uv run python scripts/research/memory_integration_probe/probe_1_entity_drift.py
uv run python scripts/research/memory_integration_probe/probe_2_redundant_edges.py
uv run python scripts/research/memory_integration_probe/probe_3_co_retrieval_duplicates.py
```

Outputs land in `output/` next to the scripts. Each script writes one
markdown file with the findings.

## Probe 4 (not scripted)

Synthetic-contradiction confirmation that the quality monitor is blind to
cross-fact conflicts. Done manually — write two `:Entity` nodes with the
same name and contradictory descriptions on a scratch Neo4j, run the
quality monitor, observe zero anomalies. See report for the finding.

## Caveats

- Probe 3 is structural, not a true log replay. The gateway logs retrieval
  *counts* but not the injected entity names, so we use co-occurrence
  neighborhoods as a proxy for what retrieval would pull.
- All probes are read-only against the production VPS Neo4j. No writes,
  no mutations, no side effects.
