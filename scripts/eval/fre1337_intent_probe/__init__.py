"""FRE-1337 — intent-classification eval harness (feeds FRE-1288 with data).

Stage 4 of the pre-LLM gateway (:mod:`personal_agent.request_gateway.intent`) classifies
every turn deterministically, with a hard-coded 0.7-confidence fallback to
``TaskType.CONVERSATIONAL`` when nothing else matches. Measured 2026-08-30, that fallback
fired seven-for-seven on a question that was plainly deep research, across three primary
models. FRE-1288 asks whether the taxonomy itself is wrong; this harness answers with a
measurement rather than an argument, by asking a model to classify the same text against
the same taxonomy and comparing.

Three arms, three modules:

- :mod:`taxonomy` / :mod:`probe` — arm 2, the classification probe. A raw, stateless,
  single-turn LLM call (no tools, no history) against each of three model deployments —
  contamination-free by construction, since there is nothing for ``search_memory`` to be.
- :mod:`behavioral` / :mod:`substrate` — arm 3 (optional), a live full turn through the
  isolated eval gateway (``docker-compose.eval.yml``'s ``seshat-gateway-control`` on
  :9002, backed by its own ``neo4j-eval``/``elasticsearch-eval``/``postgres-eval`` — never
  production). ``substrate.py`` wipes ``neo4j-eval`` between fixtures for the cross-arm
  contamination control FRE-1338's incident demands, guarded by a hardcoded URI allowlist
  rather than the ``Environment`` enum (``APP_ENV=eval`` resolves to ``DEVELOPMENT`` per
  ``env_loader.py``'s fallthrough — there is no ``Environment.EVAL``).
- :mod:`harness` — CLI orchestrator; also owns the confusion-matrix build/render (pure,
  small, not worth a separate module) and the AC-1/AC-5 loud-failure gates.

Arm 1 (deterministic) is `classify_intent()` called directly — no wrapper needed.
"""
