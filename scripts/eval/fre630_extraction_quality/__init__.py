"""FRE-630 — pre-write KG extraction-quality benchmark (ADR-0087 measurement-first).

Scores the *output* of
``personal_agent.second_brain.entity_extraction.extract_entities_and_relationships``
— a dict of ``entities`` / ``relationships`` / ``stances`` / ``claims`` — against a
curated gold set. This is the write-side complement to the FRE-435 recall harness:
FRE-435 asks "is what we stored retrievable?"; FRE-630 asks "did we extract the right
things in the first place?".

**Altitude:** *pre-write*. The benchmark observes only the extractor's returned dict,
never the Neo4j graph state, so it does not measure embedding dedup, the
description-correction gate, or write-time validation. A post-write graph-state
benchmark is deferred to Phase 2.

The pure core (``matching`` / ``metrics`` / ``scoring`` / ``report``) has no I/O and no
LLM and is fully unit-tested; ``harness`` is the I/O driver that calls the real
extractor and is run by the integrator, not in CI.
"""
