# FRE-802 — Live entity-extraction token cost + prompt-cache verification

**Ticket:** FRE-802 (Approved, stream:build2, Tier-1:Opus) · **Backing:** ADR-0109 / FRE-771 V2 prompt.
**Type:** measure-first cost investigation. Outcome-gated: docs-only if already cache-optimal;
live-prompt change (master-gated A/B) only if a real cache defect is found.

## Goal / acceptance

A research note reporting (a) measured live-extraction token cost, (b) the cached-share finding,
(c) an explicit verdict (already-optimal / restructured-with-before-after-numbers). If restructured:
a no-regression proof + the ADR-0109 drift-guard still green.

## Steps

1. **Read prompt assembly** (`second_brain/entity_extraction.py:62-281`) → verify: identify the
   cacheable static prefix and any variable content that precedes static content.
2. **Identify the live model/role** (`config/model_roles.yaml`) → verify: confirm the ES filter that
   uniquely isolates extraction calls.
3. **Write reproducible probe** `scripts/research/fre802_extraction_cache_probe.py` — token-component
   measurement (tiktoken) + ES `model_call_completed` cache aggregation → verify:
   `uv run python scripts/research/fre802_extraction_cache_probe.py` prints components + live stats.
4. **Analyse** the live cache accounting (hit rate, cached share, cost, regime change) → verify:
   numbers are stable and interpretable.
5. **Write the research note** `docs/research/2026-07-05-fre-802-extraction-prompt-cache.md` with the
   verdict → verify: every number cites the probe output.
6. **Quality gates** → `make ruff-check` / `make ruff-format` on the new script; docs render.

## Verdict (result)

**Already effectively cache-optimal — no live-prompt change.** The 3 179-token instruction block is a
clean cacheable prefix and is discounted on the 44% of calls that land while the OpenAI prefix cache
is warm. The only residual is a 340-token JSON-schema footer positioned after the variable turn;
moving it saves ~$0.02/month (warm calls only) at the cost of JSON-format-adherence regression risk
on a master-gated surface. The dominant cost movers — the deliberate V2 taxonomy (doubled input) and
the temporal sparsity of extraction (56% cold) — are not fixable by prompt restructuring. Docs-only
deliverable; no `src/` change.
