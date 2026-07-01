# Recall Operating-Point Decision — Soft Operating Point + Noise-Guard Floor Value

**Status:** Signed off (owner, 2026-07-01) — the enactment record for FRE-706
**Date:** 2026-07-01
**Backing ADRs:** ADR-0103 (Recall is Retrieval; No Clean Floor — Accepted; §5–§7, **AC-5** enactment, **AC-1** floor invariant) · ADR-0104 (Multi-Path Retrieval — Proposed)
**Linear:** FRE-706 (this record) · design half settled in FRE-705 spec §5 (`docs/specs/MULTI_PATH_RETRIEVAL_DESIGN_SPEC.md`) · enacted by FRE-724 (Build 3, seam owner)
**Supersedes framing:** FRE-655 / FRE-489 hard-cosine-floor calibration (retired by ADR-0103)

---

## 0. What this record is

FRE-706 is the **sign-off + recorded-values** enactment ticket for the recall operating point. The
*design* of the operating point was settled in the FRE-705 multi-path spec §5 (owner-confirmed). This
document does the one thing that spec deliberately left to FRE-706: it **records the confirmed values
and their rationale**, with owner sign-off against the pedagogical bar, satisfying **ADR-0103 AC-5**.

It ships **no code**. The `settings.py` change that applies the recorded value is the build chain's job
(FRE-724, "values from FRE-706 sign-off") — the adr session never touches `src/`.

---

## 1. The decision

At the recall surface there is exactly one binary act: **return retrieved material**, or **emit "no
prior discussions on this topic."** ADR-0103 §5 forbids this from being a static calibrated cutoff — the
measurement (FRE-694/695) proved the positive (true-match) and negative (no-record) cosine clouds
**overlap at every embedder size and dimension**, so no single cosine threshold separates "found it"
from "nothing here." The decision is therefore a **soft dial several signals vote on**, and the **main
model is the final arbiter of relevance** — recall delivers *material*, judgement happens one layer up.

**The rule (unchanged from spec §5):** recall returns the reranked fused material **whenever the fused
set is non-empty after the noise guard.** "No prior discussions" is emitted **only** when the fused set
is empty after the noise guard — i.e. no arm found anything above pure noise. No signal is ever used as
a drop-to-empty gate.

**The three soft signals (none is a hard gate):**

1. **Fused-set occupancy after the noise-guard floor** — the only thing that can produce an empty
   result, and only when there is genuinely nothing above pure noise.
2. **RRF agreement (≥2 arms)** — surfaced to the main model as a **confidence cue**, never a filter.
3. **Reranker ordering in its soft operating region** (FRE-695: ~88% recall @ ~9% FP at the best
   reranker) — shapes ordering and the confidence framing handed up, **never** a cutoff to empty.

---

## 2. Recorded value — the noise-guard floor

The owner chose a **small positive noise guard** (over leaving the floor at 0.0). The recorded value is
grounded in the FRE-694 separation measurement on the **live production embedder** (Qwen3-Embedding-0.6B
@ 1024 dims), reported in Neo4j score space `(cosine+1)/2`:

- True-match cloud: median **0.750**, robust lower bound **p5 = 0.676**.
- Hardest curated distractors: median **0.700**, up to **0.779** — these *overlap* the positives; that
  overlap is why this stays a **noise guard, not a separating cutoff**.
- Pure no-record noise (genuinely unrelated memory) sits well below both — the only thing a noise guard
  should trim.

| Property | Value / rule |
|----------|--------------|
| Setting | **`recall_similarity_floor` = 0.60** (score space, prod 0.6B embedder), up from 0.0 |
| Position | ~0.076 below the FRE-694 **robust lower bound** (p5 = 0.676); comfortably under the *measured* true-match distribution. The stronger *below-every-true-positive* guarantee is **not** claimed from the data (extrema are outlier-sensitive) — it is the FRE-724 probe gate (invariant below) |
| What it catches | pure no-record noise only — **not** the hard distractors at ~0.70 (those stay; the reranker + main model judge them) |
| Hard invariant (**ADR-0103 AC-1**) | the floor must stay **strictly below the FRE-489/670 probe's minimum true-positive score** — the *lowest-scoring* true positive still clears it; **zero true positives dropped** |
| Validation gate | verified on the FRE-489/670 probe **at enactment** (FRE-724). If any true positive scores below 0.60, **lower the floor** — the invariant wins over the number |
| Re-calibration | never hardcoded; config-driven (ADR-0031/0099). Re-validated on the probe **if the embedder ever changes** |

**Why this is a noise guard and not the retired hard floor:** a *separating* floor sits inside the
positive/negative overlap (~0.70–0.75), where it must sacrifice true positives to exclude distractors —
exactly what ADR-0103 proved impossible. A *noise guard* at 0.60 sits **below the FRE-694
robust lower bound (p5 = 0.676)** — comfortably under the measured true-match distribution, with the
*below-every-true-positive* guarantee reserved for the FRE-724 probe gate (AC-1). It never touches the
overlap, never tries to separate true matches from hard distractors, and only removes obvious garbage so
the "empty ⇒ no prior discussions" decision is not tripped by pure-noise phantoms. It does not — and is
not intended to — improve separation.

---

## 3. Alternatives considered

- **Floor stays at 0.0 (noise guard off).** Legacy-equivalent (ADR-0100 default). *Rejected:* leaves
  genuinely-unrelated pure noise able to occupy the fused set, weakening the "empty ⇒ no prior
  discussions" signal on truly-empty topics. Owner chose a small positive guard over this.
- **Floor at 0.65 (aggressive noise trim).** Removes more low-end material. *Rejected:* sits only
  ~0.026 below the true-match p5 (0.676), leaving too thin a margin against the probe's *minimum* true
  positive — it risks crossing into the true-match cloud and dropping a true positive, violating AC-1.
  The value must be defensibly below the whole cloud, not hugging its lower edge.
- **A hard separating cutoff on cosine or reranker score.** The original FRE-655/FRE-489 framing.
  *Rejected — the whole point of ADR-0103:* the clouds overlap; any separating cutoff sacrifices true
  positives. Retired here (AC-5).

---

## 4. Pedagogical-bar sign-off

The operating point biases hard toward **return-unless-genuinely-empty**. For Seshat's Socratic-tutor
North Star — pulling threads across sessions — that is the intended bias: better to surface a
possibly-related thread (which the main model can decline) than to falsely assert "we have never
discussed this," which is precisely the FRE-435 *"no prior discussions"* symptom that opened this whole
line of work. The cost is more borderline material reaching the main model; that cost is accepted
because relevance judgement lives one layer up, not in a recall-side gate.

**Owner sign-off (2026-07-01):** small positive noise guard, `recall_similarity_floor = 0.60`,
surface-and-let-the-model-judge. Mirrors the FRE-655 owner sign-off pattern.

---

## 5. Acceptance mapping

**Two altitudes, kept distinct so neither is faked** (mirroring FRE-705 spec §9):

- **Documentary altitude — FRE-706's own acceptance, provable now by review of this record.** That the
  operating point is soft/multi-signal, the value + rationale are recorded, and the owner signed off.
  Passing this does **not** mean the running system enacted it.
- **Live altitude — the behavioral proof, carried by FRE-724 (Build 3, seam owner) and master-verified.**
  This is where ADR-0103 AC-1/AC-5 actually bite the running system. FRE-706 supplies the values;
  FRE-724 proves them live, flag on, on the FRE-489/670 probe.

| Criterion | Altitude | How this record satisfies / hands off | Proven by — and **fails if** |
|-----------|----------|----------------------------------------|------------------------------|
| **FRE-706 acceptance** (soft signal, not a hard cutoff; values + rationale recorded) | Documentary (now) | §1 rule is soft/multi-signal; §2 records value + invariants; §3 alternatives; §4 owner sign-off | Review of this record. **Fails if** any recorded signal is a drop-to-empty gate, or the value / rationale / owner sign-off is absent |
| **ADR-0103 AC-5** (hard-floor framing retired; successor chooses a soft operating point) | Documentary (now) **+** Live (FRE-724) | Records a soft, multi-signal operating point; **no** hard separating cutoff anywhere; FRE-655/FRE-489 hard-floor framing explicitly retired (§3) | *Now:* review — no live recall ticket calibrates a hard cosine floor. *Live (FRE-724, flag on):* a non-empty fused set returns material and **no** similarity / reranker / agreement threshold can drop it to empty. **Fails if** any recall path thresholds candidates to empty, or a live ticket still calibrates a hard floor |
| **ADR-0103 AC-1** (floor is a noise guard, below the true-match distribution) | Live (FRE-724) | Records floor = 0.60 + the hard invariant that the *lowest* true positive must clear it | *Live (FRE-724):* on the FRE-489/670 probe the **lowest-scoring true positive clears the configured floor** and only no-record negatives fall below; `recall_similarity_floor` is configured to **0.60 unless the probe forces it lower**. **Fails if** any FRE-489 true positive is dropped by the floor, or the running config does not match the recorded/validated value |

---

## 6. Enactment handoff

- **FRE-724 (Build 3, seam owner)** applies `recall_similarity_floor = 0.60` in `settings.py` and
  **proves AC-1 live** on the FRE-489/670 probe (lowest true positive clears the floor). Build 3 already
  lists FRE-706 as a dependency (spec §8) — these are the "values from FRE-706 sign-off" it consumes.
- No new tickets are filed by FRE-706: the build chain (FRE-722/723/724) was filed by FRE-705; this
  record only supplies FRE-724 its operating-point values.
- The floor value is a **regression instrument, never an optimization target** (ADR-0103 §7): it is
  re-checked on the probe when the embedder changes, not fitted to the n≈54 probe data.

---

## 7. References

- ADR-0103 — Recall is Retrieval; No Clean Similarity Floor; Separation is Structural (the principle;
  AC-5 enactment, AC-1 floor invariant).
- ADR-0104 — Multi-Path Retrieval with Rank Fusion (the architecture the operating point lives in).
- ADR-0100 — Relevance-Bounded Candidate Generation (the `recall_similarity_floor` knob, default 0.0).
- ADR-0031 / ADR-0099 — config-driven identity (the floor is config-driven, never hardcoded).
- `docs/specs/MULTI_PATH_RETRIEVAL_DESIGN_SPEC.md` §5 — the operating-point design this record enacts.
- `docs/research/2026-06-29-fre-694-embedder-separation.md` — the separation measurement (pos p5 =
  0.676, no clean floor at any embedder size) grounding the 0.60 value.
- `docs/research/2026-06-30-fre-695-reranker-separation.md` — the reranker soft operating region
  (~88% recall @ ~9% FP).
- FRE-489/670 — the probe (regression instrument). FRE-655 — the retired hard-floor framing. FRE-724 —
  the build seam that enacts this value and proves AC-1 live.
