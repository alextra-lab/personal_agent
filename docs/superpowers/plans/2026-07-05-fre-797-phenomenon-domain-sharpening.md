# FRE-797 — ADR-0109 Phenomenon↔DomainOrTopic definition sharpening (A/B + conditional impl)

**Backing:** ADR-0109 (Accepted) + Amendment 1. Follow-up to FRE-790
(`docs/research/2026-07-05-fre-790-phenomenon-domain-boundary-iaa.md`).
**Model:** Tier-1 Opus. **Context:** CLEAR.

One ticket, three phases. Fail-closes to no change if the A/B does not show a clear improvement.

---

## Phase 1 — Review (decide whether to proceed) — DONE in-session

**Inputs read:** FRE-790 note (verdict + the optional clause), the A/B instrument
(`relabel_v2_types.py`, `adr0109_boundary_probe.py`, `fre790_..._fixture.yaml`), the live
extractor definitions (`entity_extraction.py:91-99`, verbatim-equal to `V2_TYPE_DEFINITIONS`),
`taxonomy.py`, and the drift-guard test (`tests/evaluation/test_entity_extraction_taxonomy.py`).

**Findings:**
1. The boundary **already clears** with margin (overall κ 0.858; Phenomenon 0.831; DomainOrTopic
   0.888 — all ≥ the FRE-770 floors). 21/24 unanimous.
2. The residual is **entirely a cross-provider artifact**: mini↔full = 1.000 (the two OpenAI
   raters agree perfectly); all 3 disagreements are the lone Claude rater dissenting, on the
   "named fundamental force/interaction that doubles as a named subfield" sub-edge
   (spacetime / electromagnetism / electricity).
3. The candidate clause is a **plausible lever** — it codifies the mechanism-vs-field
   disambiguation the data already validated (Magnetism went unanimous once its context
   foregrounded the mechanism).
4. But the clause carries an **asymmetric regression vector**: naming *electromagnetism* and
   *electricity* as canonical **Phenomenon examples** in the live prompt risks pulling a genuine
   field-of-study mention ("a graduate course in electromagnetism") into Phenomenon — the exact
   failure the ticket warns of ("pushing a genuine field of study into Phenomenon").

**Phase-1 verdict:** The residual is real but bounded; the clause could help *or* hurt net-net.
Codex's approach review (2026-07-05) confirmed the A/B would be a *provider-behavior probe*, not an
IAA-validity test — underpowered on 3 known cases, unable to structurally fix a cross-provider
artifact. **Owner decision (2026-07-05): Phase-1 stop — do not sharpen, no paid run, no live-prompt
change.** The recorded verdict is `docs/research/2026-07-05-fre-797-phenomenon-domain-sharpening.md`.
Phases 2 and 3 below are **not executed**; retained for the record of what the proceed-path would
have been.

---

## Phase 2 — A/B measurement (measure, do not assert)

Reuse the FRE-790 instrument unchanged except for a swappable definitions set, so the baseline arm
reproduces FRE-790 bit-for-bit and the sharpened arm differs *only* in the two target definitions.

### Sharpened variant (Arm B) — refined wording (avoids the regression vector)

Baseline Phenomenon / DomainOrTopic definitions are unchanged from `V2_TYPE_DEFINITIONS`. The
sharpened arm appends a symmetric disambiguation clause to **both**:

- **Phenomenon** (append): *"When a single word names both a fundamental physical force/interaction
  and the field that studies it (e.g. electromagnetism, electricity, optics, acoustics), classify
  as Phenomenon only when the mention foregrounds the physical mechanism or effect itself; if it
  foregrounds the body of study or discipline, it is DomainOrTopic."*
- **DomainOrTopic** (append): *"When a single word names both a field and the physical
  force/effect it studies (e.g. electromagnetism, optics, acoustics), classify as DomainOrTopic
  when the mention foregrounds the discipline/body of study rather than the physical mechanism."*

Rationale: leads with the *rule* (foreground mechanism → Phenomenon; foreground field →
DomainOrTopic), names the ambiguous words on **both** sides symmetrically (so neither reading is
privileged), and does **not** assert electromagnetism/electricity are "Phenomenon examples". The
exact strings are finalized in code and pinned by `prompt_hash`.

### Harness changes (all in `scripts/eval/`, no `src/` logic)

1. `relabel_v2_types.py` — thread an optional `definitions: Mapping[str, str] | None = None`
   (default `V2_TYPE_DEFINITIONS`) through `_classification_prompt`, `classify_all`, `prompt_hash`,
   `write_raw_telemetry`. **Default path is byte-identical** → FRE-770/782/790 reproducibility and
   the `b003ea594d5c` hash are preserved.
2. `adr0109_boundary_probe.py` — add `--variant {baseline,sharpened}` (default `baseline`). Define
   `SHARPENED_DEFINITIONS` = `V2_TYPE_DEFINITIONS | {Phenomenon: ..., DomainOrTopic: ...}`. Record
   the variant + its prompt_hash in telemetry.

### Run + report (owner-gated paid step)

- Arm A: `--variant baseline --run-id fre797-baseline-<date>`
- Arm B: `--variant sharpened --run-id fre797-sharpened-<date>`
- Assemble `docs/research/2026-07-05-fre-797-phenomenon-domain-sharpening-ab.md`: both arms' overall
  + per-type κ, the 6 boundary cases' per-rater outcome, and an explicit verdict mirroring the
  FRE-790 note. **Improvement** = cross-provider agreement on the sub-edge rises **AND** all 16
  clean anchors + the already-unanimous boundary cases (Magnetism/Acoustics/Optics) do **not**
  regress.

## Phase 3 — Conditional implementation (only on a clear Phase-2 win)

If, and only if, Arm B shows a real improvement with no regression:
1. Update `entity_extraction.py` Phenomenon + DomainOrTopic definitions and `taxonomy.py` to match
   the sharpened wording.
2. Keep `tests/evaluation/test_entity_extraction_taxonomy.py` green (preserve the anchor phrases
   `"naturally-occurring physical/natural phenomenon"` and `"broad field, domain, discipline"`, or
   update the assertions to the new anchors) — the drift-guard that the prompt speaks exactly the
   V2 types.
3. Run the FRE-771-style no-regression check before shipping. This is the sensitive live-prompt
   surface → **coordinated gateway deploy, master-gated** (do NOT deploy from this session).

If Phase 2 shows no clear improvement: ship nothing; the research note's one-paragraph "why not" IS
the deliverable.

---

## Acceptance criteria (from the ticket)

| # | Criterion | Proof |
|---|-----------|-------|
| AC-1 | A recorded decision (sharpen or not) backed by a measured A/B (both arms' κ + verdict), committed under `docs/research/`. | The research note. |
| AC-2 | *If implemented:* prompt + taxonomy change ships with a no-regression proof; drift-guard test still passes. | Phase-3 diff + test run (conditional). |
| AC-3 | *If not implemented:* a one-paragraph note explaining why the sharpening was not worth it. | The research note's verdict section. |

## Steps → verify

1. Harness: thread `definitions` param + `--variant` → verify: new unit tests pass; baseline
   `prompt_hash() == "b003ea594d5c"` unchanged; `--variant sharpened` prompt contains the clause,
   baseline does not; both dry-run clean.
2. **[owner-gated]** Run both arms (paid) → verify: two telemetry JSONs written, two report tables.
3. Write research note with verdict → verify: AC-1/AC-3 satisfied.
4. **[owner-gated, conditional]** Phase 3 if clear win → verify: drift-guard + no-regression pass.
5. Quality gates: `make test` (module + full), `make mypy`, `make ruff-check`/`format`, pre-commit.
