# EVAL-skill-routing-2026-05

Phase D eval for the skill routing architecture (FRE-skill-routing).

**Goal**: Empirically verify that hybrid+B.5 reduces `tool_iteration_limit_reached`
to floor. Decide which routing mode to default per profile.

## Matrix

6 cells: `{local, cloud} × {keyword, hybrid, model_decided}`

- **Cloud cells** (3): run now — Sonnet as primary
- **Local cells** (3): run when SLM server is available — Qwen as primary

See `matrix.yaml` for cell definitions and env vars.

## Commands

```bash
# Run a single cloud cell
make eval-skill-routing CELL=cloud-keyword RUN=2026-05-06

# Run all 3 cloud cells in sequence (takes ~30-60 min)
make eval-skill-routing-cloud RUN=2026-05-06

# Analyse a completed run
make eval-skill-routing-analyse CELL=cloud-keyword RUN=2026-05-06

# Run a single prompt for quick iteration
make eval-skill-routing CELL=cloud-hybrid RUN=smoke PROMPT=es_incident_class
```

## Pass criteria

| Metric | Threshold | Scope |
|--------|-----------|-------|
| ES first call uses `agent-logs-*` OR guard fires | ≥ 95% | All 6 cells (B.5 mode-independent) |
| `tool_iteration_limit_reached` rate | Dropped in hybrid vs keyword | Same profile |
| `read_skill_invoked` rate | > 0 in model_decided | Cloud + local cells |

## Layout

```
EVAL-skill-routing-2026-05/
  README.md
  matrix.yaml          ← cell definitions
  prompts.yaml         ← 10 eval prompts
  <cell-id>-<run-id>/  ← harness run output (gitignored)
    <prompt-id>/
      report.md
      raw.json
    summary.md
    skill_routing_summary.json  ← Phase B/C metrics (from analysis script)
```

## Decisions to make (ADR-0066)

After all 6 cells complete:
- **Definitely keep**: B.5 reactive guards (defense-in-depth regardless of outcome)
- **Local default**: `hybrid` unless `model_decided` matches reliably on 35B (unlikely)
- **Cloud default**: `model_decided` unless it underperforms hybrid
- **Keyword policy**: keywords stay as regression fallback; new keywords need evidence
