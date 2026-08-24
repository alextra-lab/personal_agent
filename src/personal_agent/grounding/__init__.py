"""Grounding contract machinery (ADR-0138).

The model may generate, but it may not assert: outside the exempt regions of ADR-0138 D1,
any span making a claim about the world requires a citation that resolves to a source
retrieved during *this* turn.

Landed by FRE-1281 — span extraction, the component the whole contract's strength is
bounded by:

- :mod:`personal_agent.grounding.spans` — the span domain types and D1's closed list of
  exempt regions.
- :mod:`personal_agent.grounding.code_regions` — the deterministic partition: where, if
  anywhere, exemption can be *proved*.
- :mod:`personal_agent.grounding.span_policy` — the deterministic post-pass that enforces
  D1's invariants and fails closed on any gap.
- :mod:`personal_agent.grounding.extractor` — the classifier pass D1 requires to be "a
  named component, not a regex".

Nothing here blocks a turn today. The three inline checks and the block-retry-refuse loop
are FRE-1282; the prompt changes that make the model emit citation markers at all are
FRE-1283.
"""
