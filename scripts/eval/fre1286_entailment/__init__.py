"""Measuring the D3(d) entailment judge (ADR-0138, ADR-0087, FRE-1286).

ADR-0138 rejected per-claim inline entailment (Option 5) partly because "the entailment
judge is itself a model, with its own error rate, sitting on the critical path". D3(d)
puts it on that path anyway for one class — spans with no entity and no figure, which
containment cannot decide — so the objection is answered by *measuring* the judge rather
than by trusting it.

Two things live here:

- :mod:`corpus` / :mod:`metrics` / :mod:`harness` — the labelled corpus and the scored run
  behind AC-3 (contradiction and quantifier reversal are detected) and AC-6 (the judge's
  own error rate is measured, not assumed). Pure core is unit-tested; the driver needs a
  model and is run by hand, the same split ``fre1281_span_extraction`` uses.
- :mod:`miss_rate` — AC-4's query: the production miss rate per answering model, read out
  of the sampled offline arm's telemetry.
"""
