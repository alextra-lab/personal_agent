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

**Where the two halves meet** (FRE-1282) — extraction decides *that* a span needs a
citation, the registry decides *what* a citation may resolve to, and these join them:

- :mod:`personal_agent.grounding.containment` — D3(c)'s unit and the normalization
  contract: what a source must actually say for a claim to count as contained.
- :mod:`personal_agent.grounding.verification` — the inline checks, plus D2's entitlement
  gate, which is the one that stops the system citing its own earlier confabulation.
- :mod:`personal_agent.grounding.enforcement` — D4: block, retry with retrieval forced,
  then the explicit no-source statement.

**How well a model actually holds to it** (FRE-1284) — the reading D5's enforcement
selection is keyed on:

- :mod:`personal_agent.grounding.compliance` — the per-model compliance metric: the
  unconfounded-observation predicate, the rolling window, and the staleness rule that makes
  compliance re-earned rather than banked. It computes a reading and decides nothing.

**What follows from the reading** (FRE-1285) — D5's other half:

- :mod:`personal_agent.grounding.enforcement_selection` — light or heavy, keyed on the
  computed rate and on nothing else: the hysteresis band, the cooldown a demoted model
  serves, and the probation sampling that stops the bootstrap deadlocking. Its input is a
  ``float | None``, so "never a model name, provider, or tier list" is a property of the
  signature rather than a promise about the body.

**Whether this blocks a turn is one setting.** ``grounding_verification_mode`` runs the
pass and records every outcome by default (``observe``) and blocks on ``enforce``, which is
the ADR-compliant value. It is a deploy valve and emphatically **not** D5's enforcement
level: light/heavy varies whether retrieval is forced *before* generation (FRE-1285), while
D3's checks are inline at every one of those levels.
"""
