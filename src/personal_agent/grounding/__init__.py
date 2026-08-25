"""Grounding contract machinery (ADR-0138).

The model may generate, but it may not assert: outside the exempt regions of ADR-0138 D1,
any span making a claim about the world requires a citation that resolves to a source
retrieved during *this* turn.

This package holds the output side of that contract. ``captains_log/turn_evidence.py``
(ADR-0125) records the input side — what recall offered, what was admitted, why the rest
dropped — and remains the substrate this builds on.

Landed so far (FRE-1280):

- :mod:`personal_agent.grounding.source_registry` — the per-turn source registry, its
  stable identifiers, and D2's independence rule.
- :mod:`personal_agent.grounding.citations` — the output format binding one citation
  marker to one span, and turn-scoped resolution against the registry.

Still to land, each binding to the identifiers established here: span extraction
(FRE-1281), the D3(b)(c) checks and D4's block-retry-refuse loop (FRE-1282), and the
prompt changes that make the model emit markers at all (FRE-1283). Nothing in this
package blocks a turn today.
"""
