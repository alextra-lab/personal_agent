# FRE-790 — Phenomenon ↔ DomainOrTopic boundary probe (properly-powered AC-1 re-measurement)

**Backing:** [ADR-0109](../../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md) (Accepted) + Amendment 1, **AC-1**.
**Surfaced by:** [FRE-771](https://linear.app/frenchforest/issue/FRE-771) powered A/B — `docs/research/2026-07-04-fre-771-10type-prompt-swap-powered-ab.md` (83.3% cross-model agreement on n=12; both disagreements = `Spacetime`, `TCP` on the `Phenomenon ↔ DomainOrTopic` edge).
**Mirrors:** [FRE-782](https://linear.app/frenchforest/issue/FRE-782) instrument — `docs/research/2026-07-04-fre-782-knowledgeartifact-quantitymeasure-boundary-iaa.md` (22-entity fixture, 3 blind raters, Fleiss κ per type, committed fixture + shape-test). **Note:** the *instrument* is mirrored exactly; the **verdict rule here is fixed BEFORE the run** (FRE-782 wrote its rule after running, still externally anchored) — FRE-790's preregistration is stricter, not identical.

## Problem

FRE-771 measured ADR-0109 AC-1's cross-model type-agreement at **83.3% (10/12)** on the `type-boundary` gold subset — below the ≥90% target, but n=12 is underpowered (one flip = ~8pp). Both disagreements trace to the **Phenomenon ↔ DomainOrTopic** boundary, an edge ADR-0109's Risks section named as untested. This ticket grows a dedicated, properly-powered probe on *that specific edge* to decide: acceptable residual, or does the taxonomy need a sharper definition?

## Pre-registered verdict rule (written BEFORE the run — anchored to a pre-existing number, not tuned to pass)

Mirroring FRE-782's discipline (probe run *after* the rule is fixed), the acceptance bar is the **already-shipped** V2 types' own FRE-770 agreement floor:

> The `Phenomenon ↔ DomainOrTopic` boundary **clears** iff, on this probe: (a) overall Fleiss κ ≥ **0.777** (FRE-770's full-gold ceiling), AND (b) **both** `Phenomenon` and `DomainOrTopic` per-type κ ≥ **0.645** (FRE-770's weakest-accepted per-type mark, `MethodOrConcept`). If either type falls below its mark, the boundary does **not** clear → propose a sharper `Phenomenon` exclusion clause (e.g. *"Not the academic field/discipline that studies the phenomenon → DomainOrTopic"*) and/or a `DomainOrTopic` inclusion clause, filed as a Needs-Approval follow-up ADR-0109 amendment for owner review. Ship nothing to the live extractor here — measurement only.

The disagreement diagnosis (which entities split, and how the two definitions would need to change) is reported regardless of pass/fail.

## Fixture design (~24 entities, concentrated on the Phenomenon ↔ DomainOrTopic edge)

Deliberately weighted so `Phenomenon` and `DomainOrTopic` each get a healthy positive count (the two types whose κ is the actual measurement — fixing FRE-771's n=12 power problem). `intended_side` is design intent, never shown to a rater; the measurement is rater↔rater IAA, not accuracy against intent. `boundary: true` marks genuinely dual-natured cases.

**Phenomenon — clean anchors (8):** naturally-occurring, independent of human design.
- Gravity · Photosynthesis · the greenhouse effect · Superconductivity · Turbulence · the Doppler effect · Rayleigh scattering · the Maillard reaction

**DomainOrTopic — clean anchors (8):** broad fields / disciplines.
- Cosmology · Cybersecurity · Thermodynamics · Neuroscience · Fluid dynamics · Number theory · Behavioral economics · Organic chemistry

**Phenomenon ↔ DomainOrTopic boundary cases (6, `boundary: true`):** each word names both a natural phenomenon/effect/force AND the field that studies it. The `context` for each is written to keep **both readings live** (not to force one side) — a boundary case is only informative if it genuinely sits on the fence; the note reports which reading each context afforded.
- **Spacetime** (ADR-named) · **Electromagnetism** (interaction vs field) · **Magnetism** (effect vs study) · **Electricity** (physical phenomenon vs topic) · **Acoustics** (physical behavior of sound vs discipline; owner music domain) · **Optics** (optical behavior vs discipline)
- *(Aerodynamics dropped per codex plan-review — it reads as a clean field, not a genuine dual case; Electricity substituted as a stronger phenomenon-vs-topic edge.)*

**Non-boundary distractors (2, `boundary: false`) — force active rejection of the two nearest non-boundary types:**
- **TCP** (`boundary: false`; carried from FRE-771 as a documented disagreement, but it is a protocol → **TechnicalArtifact**, NOT a Phenomenon↔DomainOrTopic dual case — per codex plan-review, marking it `boundary` would confound "separate P from D" with "reject a protocol into TechnicalArtifact"; kept as a clean distractor and **reported separately** from the six true boundary cases)
- **the Fourier transform** (`boundary: false`; MethodOrConcept anchor — a human-*invented* method, must not read as Phenomenon)

Each entity gets a short realistic `context` sentence (as in FRE-782). Full contexts drafted in the fixture file.

## Steps

1. **Write the fixture** `scripts/eval/fre630_extraction_quality/fre790_phenomenon_domain_boundary_fixture.yaml` — same shape as `fre782_boundary_fixture.yaml` (`probe:` list of `{entity, context, intended_side, boundary}`), the ~24 entities above with contexts. → verify: `yaml.safe_load` parses; every `intended_side ∈ ALLOWED_ENTITY_TYPES_V2`.

2. **Write the committed runner** `scripts/eval/fre630_extraction_quality/adr0109_boundary_probe.py` — the runner FRE-782 named but never committed. Loads a fixture YAML (`--fixture`, default = FRE-790 fixture), builds `EntityItem`s (`item_id = f"probe::{entity}"`, `context` verbatim), and **reuses** `relabel_v2_types`'s `classify_all` / `build_report` / `render_report_table` / `write_raw_telemetry` unchanged. Flags: `--run-id` (required), `--dry-run`, `--limit`. Writes raw telemetry to the gitignored `telemetry/evaluation/fre630-extraction-quality/` (reusing `write_raw_telemetry` unchanged → generic `v2-relabel-<run-id>.json` filename; cosmetic since gitignored, disambiguated by a descriptive run-id `fre790-2026-07-05` per codex note). No new statistics, no new prompt — same blind-classification instrument as FRE-770/782. → verify: `--dry-run` prints a report table, exits 0, writes no real API calls.

3. **TDD tests** (unit, no LLM — safe for `make test`):
   - `tests/evaluation/test_fre630_gold_set.py::test_fre790_boundary_fixture_matches_research_note` — mirrors the FRE-782 shape-test exactly: an `EXPECTED_FRE790_PROBE` `{entity: (intended_side, boundary)}` dict, assert count == len == 24, `actual == EXPECTED`, every `intended_side ∈ ALLOWED_ENTITY_TYPES_V2`.
   - `tests/evaluation/test_adr0109_boundary_probe.py` — new: `load_probe_fixture(path)` returns the right `EntityItem` count with contexts populated; `build_report` over a `classify_all(dry_run=True)` pass yields an `IAAReport` with `n_items == 24` (proves the fixture→items→report plumbing without a paid call).
   - Write the tests FIRST, confirm they fail (no fixture / no runner yet), then implement.

4. **Dry-run smoke** — `uv run python -m scripts.eval.fre630_extraction_quality.adr0109_boundary_probe --run-id smoke --dry-run`; confirm 24 items, report table renders.

5. **[CHECKPOINT — owner OK for the live paid run]** — the run fires **~72 paid calls** (24 entities × 3 raters). Estimated cost **≈ $1** (FRE-771 measured: mini ≈$0.0017/call, sonnet ≈$0.026/call, gpt-5.4 ≈$0.01–0.02/call). Keys sourced from primary `/opt/seshat/.env` (`AGENT_OPENAI_API_KEY` + `AGENT_ANTHROPIC_API_KEY`; the build worktree has no `.env`). Surface the estimate, get explicit OK, then run.

6. **Live run** — `--run-id fre790-2026-07-05`, 3 real raters. Capture the rendered κ table + per-entity votes from the gitignored telemetry JSON.

7. **Research note** `docs/research/2026-07-05-fre-790-phenomenon-domain-boundary-iaa.md` — mirror the FRE-782 note structure: Why / Method / IAA table (overall + per-type κ **with `n_positive` + raw_agreement** + rater-pair) / **per-entity split table** / the disagreements diagnosed / **Verdict against the pre-registered rule** / Reproduction (fixture appendix + runner command) / References. Explicitly report, for each of the 6 boundary cases, **which reading (phenomenon vs field) the context afforded**; report TCP + Fourier separately as non-boundary distractors. Add the FRE-782-style **scope-of-claim caveat** — this validates the *one boundary*, not production-wide 10-type stability, and per-type κ on small `n_positive` is boundary-probe evidence, not a stability estimate. If the boundary fails, include the proposed definition tweak and file the follow-up amendment ticket (Needs Approval).

8. **Backfill the shape-test** `EXPECTED_FRE790_PROBE` to match the note's committed appendix exactly (entity-for-entity), same as FRE-782.

9. **Quality gates** — `make test-file FILE=tests/evaluation/test_adr0109_boundary_probe.py` + the gold-set test, then `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

10. **PR** + Linear close-out comment for master with AC proof.

## Acceptance criteria → proof

| Ticket AC | Proof |
|---|---|
| Committed probe fixture mirroring `fre782_boundary_fixture.yaml` shape, on the Phenomenon↔DomainOrTopic boundary, incl. Spacetime + TCP | `fre790_...fixture.yaml` committed; `test_fre790_boundary_fixture_matches_research_note` asserts shape + entities |
| Research note reporting measured κ for that boundary, explicit verdict vs FRE-770 floor | `docs/research/2026-07-05-fre-790-...md` with overall + per-type κ and a pass/fail verdict against the pre-registered rule |
| If it doesn't clear: proposed definition tweak for owner review | (conditional) proposed exclusion/inclusion clause in the note + a Needs-Approval follow-up amendment ticket |

## Non-goals / safety

- **No change to the live extractor / gold / `V2_TYPE_DEFINITIONS`.** Measurement only (like FRE-770/782/771's measurement arm). The runner calls `litellm.acompletion()` directly (no cost-gate, no KG writes, no gateway) — the same called-out exception FRE-770 documented.
- One phase = one PR: this is the whole ticket (fixture + runner + tests + note), no ADR-phase bundling.
- The live run is the only paid action and is gated on the Step-5 checkpoint.
