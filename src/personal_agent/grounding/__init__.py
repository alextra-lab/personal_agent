"""Grounding contract machinery (ADR-0138).

The model may generate, but it may not assert: outside the exempt regions of ADR-0138 D1,
any span making a claim about the world requires a citation that resolves to a source
retrieved during *this* turn.

This package holds the output side of that contract. ``captains_log/turn_evidence.py``
(ADR-0125) records the input side — what recall offered, what was admitted, why the rest
dropped — and remains the substrate this builds on.

**The sources a citation may point at** (FRE-1280):

- :mod:`personal_agent.grounding.source_registry` — the per-turn source registry, its
  stable identifiers, and D2's independence rule.
- :mod:`personal_agent.grounding.citations` — the output format binding one citation
  marker to one span, and turn-scoped resolution against the registry.

**What needs a citation in the first place** (FRE-1281) — span extraction, the component
the whole contract's strength is bounded by, since a claim the extractor misses is a claim
the contract never sees:

- :mod:`personal_agent.grounding.spans` — the span domain types and D1's closed list of
  exempt regions.
- :mod:`personal_agent.grounding.code_regions` — the deterministic partition: where, if
  anywhere, exemption can be *proved*.
- :mod:`personal_agent.grounding.extractor` — the classifier pass D1 requires to be "a
  named component, not a regex".
- :mod:`personal_agent.grounding.span_policy` — the deterministic post-pass that enforces
  D1's invariants and fails closed on any gap.

The two halves do not yet meet: extraction decides *that* a span needs a citation, the
registry decides *what* a citation may resolve to, and joining them is FRE-1282 — the
D3(b)(c) checks and D4's block-retry-refuse loop. The prompt changes that make the model
emit markers at all are FRE-1283.

**Nothing in this package blocks a turn today.**
"""
