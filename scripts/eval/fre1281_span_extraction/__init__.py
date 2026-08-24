"""FRE-1281 — span-extraction quality corpus and scoring (ADR-0138 D1, AC-7).

The extractor itself is production machinery and lives in
``personal_agent.grounding``; this package is the instrument that measures it.

ADR-0138 states plainly that the grounding contract "can never be stronger than its
recall", which makes this package the thing that decides whether the contract is worth
anything at all. An unmeasured extractor makes every other criterion in the ADR
unfalsifiable.

Layout, following the FRE-630 split (pure core unit-tested, I/O driver run by hand):

- :mod:`corpus` — the ``GoldSpan`` / ``GoldDocument`` schema, the loader, and the
  discipline guards that make the bar-floor arithmetic in :mod:`bars` hold by
  construction rather than by hope.
- :mod:`bars` — the preregistered bars. Committed **before** any extractor exists; the
  commit order is the AC-5 evidence.
- :mod:`metrics` — pure scoring. No I/O, no LLM.
- :mod:`baselines` — five deliberately broken extractors plus an oracle, so every bar is
  demonstrated to reject something.
- :mod:`report` — score aggregation and bar evaluation.
- :mod:`iaa` — the independent second labeller and Cohen's κ.
- :mod:`harness` — the I/O driver.

``ADJUDICATION.md`` holds the labelling guidance D1 requires to live with the corpus
rather than in the ADR. ``corpus.yaml`` is the artifact.
"""
