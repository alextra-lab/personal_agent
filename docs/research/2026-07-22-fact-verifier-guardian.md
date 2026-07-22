# A fact verifier: a guardian-like cognitive component (DESIGN THREAD)

> **Status:** design thread — undeveloped. Its own build, not a bolt-on to any other workstream.
> **By:** cc-explore. **Date:** 2026-07-22. **Origin:** owner observation — "what's missing from cc
> master sessions was *verification*"; and the meta-learning thread's conclusion that self-observation
> needs an external teacher.

## Motivation — one gap seen from two sides

- **Operational:** last session master was confidently wrong four times and **only the owner** caught
  every one — none from self-review. A missing independent verification step.
- **Meta-learning:** a system observing its own construction can learn *coherence* but not
  *correctness* — the same biases that produce a bad construction produce a bad self-assessment of it
  (coherence ≠ correctness). Learning-from-how-you-learn needs an **external teacher** to label which
  constructions were good.

These are the **same gap**. A verifier is the systematized version of "the owner catches every error."

## Position — a fourth cognitive component

Alongside the existing components, each doing a distinct job:

- `captains_log` — the **observer** (captures + reflection).
- `insights` — the **pattern engine** (aggregate anomaly/trend detection).
- **verifier — the guardian of *facts*** (independent correctness check). *(new)*

## The non-negotiable constraint

A verifier that checks a claim against **its own reasoning** only re-confirms coherence — it becomes a
second confabulator. To be a real teacher it must verify against something **independent** of the
reasoning that produced the claim:

- **source** — the originating capture / document / tool result the fact was extracted from;
- **re-execution** — re-run the tool / re-query the substrate;
- **independent frame** — a second model with a different prompt/vantage, not the same chain;
- **outcome** — a downstream success/failure signal.

"Verify fact F" is meaningful only when F is checked against **where F came from**, never against a
fresh plausibility judgment. This line is the whole design.

## What it could verify (fact classes → independent ground)

- **KG facts** (entities/claims/relationships) → the source capture they were extracted from; flag
  drift/extraction error. (Gates Workstream A's KG quality.)
- **Continuity claims** ("last time we decided X") → the actual stored session record.
- **Constructions** (a reasoning trace's conclusion) → an external outcome label → the **teacher
  signal** for the construction-trace / meta-learning study.
- **Master-style delivery claims** ("done / verified live") → durable evidence (merged SHA, health,
  ACs). A guardian for the guardian.

## Cross-cutting (but built once, standalone)

Feeds: (A) KG fact quality · the meta-learning teacher signal · a master-guardian. But it is **its own
component with the independence constraint** — not code bolted onto A/B/C.

## Open questions

1. What is the independent ground for each fact class, concretely, and is it always available?
2. Verification cost/latency — background (like consolidation) or gating (blocks a write)?
3. How to keep the verifier from inheriting the same biases (model/frame diversity; when is a human
   the only valid teacher?).
4. Human-in-loop vs automated: which fact classes can be auto-verified vs must escalate to the owner?
5. What does a verified-vs-unverified fact *do* downstream (confidence weight, suppression, re-check)?
