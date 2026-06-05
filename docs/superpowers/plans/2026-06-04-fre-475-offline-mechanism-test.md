# FRE-475 — Offline deterministic mechanism test + expand-exemption (ADR-0085)

> **Status:** Plan — codex review then owner approval then implement
> **Ticket:** FRE-475 (In Progress · Tier-1) · project *Turn Cost & Latency Optimization*
> **Contract:** the owner's instructive note (Linear comment `c8ac1fee`, 2026-06-04 20:59Z) —
> *"test the mechanism offline & deterministically before any more live A/Bs."*
> **Builds on:** PR-A infra (`tool_result_digest.py`), birth-time placement (PR #162).
> **Does NOT:** deploy, run a live A/B, enable the flag, or touch the gateway.

## Why this iteration is offline-only

The two live A/Bs (`5f2d1277` keep-deferred, `950386d6` birth-time) are uninterpretable for the
mechanism — ≥4 confounds: build drift (FRE-470 bash exit-141, FRE-471 artifact_draft cap),
trajectory nondeterminism (`grep`→`cat`), FRE-478 output-cap spiral, FRE-484 forced-synthesis
failure. `a0a07227` is **retired as a control**. The note's layered plan: rung 1 = offline mechanism
test (this plan); rung 2 = design fixes surfaced before scale; rung 3 = scale test on the *same*
build (master, later, owner-gated). This plan delivers rungs 1 and 2.

The single deterministic question to answer: **for a fixed accumulation of tool outputs, does
digestion reduce the tokens re-sent to the model each round, and by how much?** — with no model in
the loop. It proves the *mechanical ceiling* (best case if the model never re-expands). It cannot
prove behavioral effects (expand clawback, answer quality) — those need scale.

## Re-bill model (made explicit)

The a0a07227 forensics showed the accreted tool tail sits **past the last cache breakpoint** (the ≤4
breakpoints sit on the stable head — FRE-468), so the whole growing tail is re-billed at full price
every round. The offline harness models exactly this observed regime:

```
fresh_in(r) = Σ_{i ≤ r} tokens(effective_content_i)     # tail re-billed each round
total_fresh = Σ_r fresh_in(r)                            # the quadratic accumulation
```

- **OFF arm:** `effective_content_i` = verbatim tool result `i`.
- **ON arm:** `effective_content_i` = the birth-time digest (if eligible/unpinned) else verbatim.

`total_fresh` is directly comparable to the 768,484 baseline number; `%reduction = 1 − ON/OFF`. The
model is a named, documented assumption (`HEAD_ONLY_BREAKPOINTS`), faithful to the measured pathology
— not a claim that all layouts behave this way.

**Second curve (codex Q1):** also emit a **new-tail-only** lower-bound curve
`Σ_r tokens(effective_content_r)` (each result counted once, no re-bill amplification). Reporting
both separates "digest shrinks content" (new-tail) from "quadratic re-bill amplification"
(cumulative). The mechanism claim rests on both moving the right way.

## Deliverables

### D-1 — Fixed tool-result tape (committed fixture, deterministic)
`tests/personal_agent/orchestrator/fixtures/fre475_tape.py` (or `.json`): an ordered list of ~20
realistic `{tool_name, arguments, content}` entries representative of a discovery turn. **Composition
defined up front (codex Q5 — reviewable, not just deterministic):**
- ~10 large `bash` results (≥1500 tok): `ls -R`/`find` listings, multi-file `grep`, a test-run dump
  with a traceback (exercises the structured-middle extractor).
- ~3 ranged `read` results (≥1500 tok) of source files.
- 1 large generic-JSON tool result (≥1500 tok).
- 2–3 small below-threshold results (verbatim in both arms — proves the gate, not just size).
- 1 `read`→`write` pair on the same path (exercises the D4 pin: the read stays verbatim its round).
- 1 `expand_tool_result` result **above threshold** (exercises fix-a: must stay verbatim anyway).
- **1 adverse entry (codex Q4):** clears `threshold_tokens` but its digest fails / barely clears
  `min_savings` — proves the harness measures the real `digest_saves_enough` gate, not "size ⇒
  digested." E.g. content already near-incompressible (mostly unique short lines).

**Hand-built / curated** — not raw trace captures (memory: no log dumps in git). Provenance comment:
shapes mirror `a0a07227` captures, values synthetic. Every entry carries an expected-outcome
annotation (`digested` | `verbatim:below_threshold` | `verbatim:pinned` | `verbatim:expand_exempt` |
`verbatim:min_savings`) consumed by D-3.

### D-2 — Offline simulation harness (script, ticket artifact)
`scripts/eval/fre475_compression_ab/offline_mechanism.py`:
- Loads the tape; builds a minimal fake `ExecutionContext` (real type, stub fields) and an in-memory
  fake `R2ArtifactStore` (records `put`, serves `get` for byte-stability).
- **Fidelity guard (codex Q2):** at startup the harness asserts every fake-ctx field satisfies the
  real validation predicates — `session_id` is a valid UUID (a non-UUID silently skips *all*
  digestion via `_safe_session_uuid`), and `trace_id` / every `tool_call_id` match `_KEY_SEGMENT_RE`
  (an invalid segment silently drops the candidate). Without this guard the harness could report 0
  digests / 0% reduction with no error — a false negative.
- Round loop: for each tape entry, build the one-entry `tool_results` batch + sidecar, then —
  - **ON arm:** `await apply_intra_turn_digest(ctx, batch, sidecar, store=fake, bus=None)` then
    `ctx.messages.extend(batch)` (faithful birth-time call order).
  - **OFF arm:** `ctx.messages.extend(batch)` only (flag-off path = no digest pass).
- After each round, compute `fresh_in(r)` = `estimate_tokens` of all accreted `role="tool"` contents.
- Emits: per-round table (round, tool, verbatim_tok, effective_tok, fresh_in_ON, fresh_in_OFF),
  totals, `%reduction`, and a flatten metric (ON marginal slope vs OFF). Writes JSON to a gitignored
  path; prints a markdown table for the Linear comment. **No /chat, no LLM, no deploy.**

### D-3 — Deterministic mechanism test (committed, CI-guarded)
`tests/personal_agent/orchestrator/test_offline_mechanism.py`. **Codex Q4 BLOCKING — the test must
not pass by construction.** The fixture is sized to clear the threshold and an aggregate-only
reduction assertion is circular (margin derived from the same fixture). So the test asserts
**per-entry structural facts first**, and reduction only as a consequence:
- **Per-entry outcome (the real defense):** for every tape entry, assert its actual outcome equals
  its declared annotation — `digested` entries carry `_digest: true`; `verbatim:below_threshold`,
  `verbatim:pinned`, `verbatim:expand_exempt`, and `verbatim:min_savings` entries are byte-unchanged.
  This proves the harness exercises each real gate (`should_digest`, pin, `digest_saves_enough`,
  expand-exemption) rather than assuming size ⇒ digestion.
- **Adverse case:** the `verbatim:min_savings` entry clears `threshold_tokens` yet stays verbatim —
  proves `digest_saves_enough` is in the loop.
- **Reduction:** ON `total_fresh` < OFF `total_fresh` and ON new-tail < OFF new-tail; the *magnitude*
  is reported, not gated on a hard %% (the live ≥30% gate stays a scale-rung item). A loose floor
  (e.g. ON ≤ OFF×0.9) guards against a future regression that silently disables digestion.
- **Flatten:** ON cumulative-tail slope over the last K rounds < OFF slope.
- **Byte-stability:** re-running the ON arm twice yields byte-identical digests (re-asserts PR-A
  fixed point through the simulation path).

### D-4 — Design fix (a): never digest an expansion (code change, structural)
`src/personal_agent/orchestrator/tool_result_digest.py`:
- Add module constant `_NEVER_DIGEST_TOOLS: frozenset[str] = frozenset({"expand_tool_result"})` and
  check it at the **top** of `should_digest` (before config excludes and before token estimation) →
  return `False` (codex Q3 — `should_digest` is the central home; `_digest_candidate_entries`
  delegates to it, and direct callers must also see the exemption). Structural (not just a config
  default) so config cannot accidentally re-enable digesting an explicit verbatim retrieval. The
  config `tool_result_digest_exclude_tools` stays additive.
- Docstring distinguishes "structural never-digest" from config-driven excludes; ADR-0085 §D5 ref.
  TDD: failing test (an oversized `expand_tool_result` content currently digests) → add guard → green.

### D-5 — Design fix (b): quieter digest hint — DEFERRED out of this PR (codex Q5)
The current `hint` ("Call expand_tool_result(...) … before editing against omitted lines") is
imperative and *invites* the clawback. Its behavioral effect is **not offline-measurable**, and
landing model-facing copy in the same PR as the pure mechanism harness overreaches the offline-first
contract. **Deferred:** filed as a follow-up (Needs Approval) to be decided alongside the scale rung,
where its effect can actually be measured. Not implemented in this PR.

## Out of scope (filed / existing follow-ups)
- Live A/B re-run (AC#3/#4) — scale rung, master, owner-gated, post-FRE-484 (already fixed).
- Deferred released-pin digestion — **FRE-485** (already filed).
- Quieter digest hint (D-5) — **new follow-up FRE (Needs Approval)**, decided at the scale rung.
- Decomposition as the alternative lever — **FRE-476** (the note flags weighing offline results vs
  decomposition; that is an owner/architecture call, not this build).

## Tests / quality gates
- `make test-file FILE=tests/personal_agent/orchestrator/test_offline_mechanism.py`
- `make test-file FILE=tests/personal_agent/orchestrator/test_tool_result_digest.py` (fix-a guard)
- `make test-file FILE=tests/personal_agent/orchestrator/test_intra_turn_digest.py` (regression)
- `make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
  `pre-commit run --all-files`
- Flag default-off; zero runtime behaviour change (the only code change, fix-a, only narrows what the
  *already-flag-gated* path digests).

## PR boundary
One PR: tape fixture + offline harness + mechanism test + fix-a (expand-exemption). Push branch, open
PR with the per-round offline table (both curves) in the body, **STOP** — master reviews/merges; the
scale A/B stays a post-merge owner-gated item. fix-b (hint copy) is a separate Needs-Approval FRE.
