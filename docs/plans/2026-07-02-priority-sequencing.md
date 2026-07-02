# Priority Sequencing Plan — 6-Project Deep-Dive (2026-07-02)

> **Source:** parallel deep-dive triage of the owner's 6 priority projects (one analyst agent per project, every open ticket read for scope/deps/backing-ADR).
> **Mandate:** priority order fixed by owner · **core = corrections, improvements, stability** (NOT "finish every ticket") · **sequence by cross-project correlation, not project-in-isolation**.
> **Owner priority order:** Memory Recall → Telemetry Surface → Linear Async Feedback → Configuration Management → Seshat Inference → Seshat Pedagogical.

---

## 1. The cross-project correlations (the spine)

These are why the plan is NOT just "do the projects in order":

1. **Configuration Management is foundational stability for the whole harness.** It fixes the exact drift class that caused the live FRE-734 vision break (models.yaml / models.cloud.yaml / models.eval.yaml). Its **FRE-652 overlaps the vision project's FRE-735** (both touch `models.eval.yaml`), and **FRE-649's `model_roles.yaml`** is the same model-role surface that **Inference's FRE-432/516 routing measurements** read. → Config is #4 by owner priority but de-risks vision, inference, and every deploy. **Pull its guard+correction (648/649) early.**

2. **"System-domain isolation" is one decision surfacing in three chains** — coordinate, don't build blind:
   - Memory **FRE-639** (ADR-0098 System gate + eviction)
   - Memory **FRE-728–732** (ADR-0106 System/User boundary by `output_kind`)
   - Linear Async **FRE-714–717** (ADR-0105 isolated System-graph Postgres)
   Memory **FRE-729** is explicitly blocked-by the ADR-0105 sysgraph landing. → the Memory (#1) and Linear-Async (#3) waves are coupled at the System-graph seam.

3. **Pedagogical (#6) is gated on Memory Recall (#1).** The entire M3/M4 pedagogical layer needs the typed concept-graph that only exists once ADR-0098 substrate + ADR-0104 recall ship. This **validates the priority order**. The one exception — the **ADR-0091 eval-instrument chain (561→562→563→564→453)** — is harness-only and independent, and it is precisely the instrument that will *measure whether the Memory work improves learning*. Build it now as the measurement backbone.

4. **Telemetry (#2) is genuinely almost done** and its one external blocker (FRE-587) belongs to a 7th project (Observability Foundation / ADR-0093 OTel), not here. Finishing Telemetry is cheap and gives observability that de-risks all the feature waves.

---

## 2. Wave sequence (core-first, correlation-ordered)

### Wave 0 — Stability sprint (corrections; mostly independent; **the core the owner wants**)
Independent bug/drift fixes across all 6 projects — highest correctness-per-effort, unblock nothing-depends-on-them, ship in any order:
- **Config:** 648 → **649** (config guard + corrects live nano/mini + nano/sonnet drift in-PR — the single highest-value stability ticket)
- **Memory bugs:** 657, 677 (Haiku quick wins) → 659 (zero-vector embeddings on outage), 620 (KGQ dead `:Conversation` label → daily false alarms), 632 (ADR-0052 split "Alex" nodes), 676 (recall identity remnants)
- **Inference bugs:** 495 (context_length understates window), 592 (docs drift), **502** (silent planner-degradation → tool-less fallback; highest user-facing)
- **Linear Async:** 710 (Captain's-Log reflection off per-turn — 1,872 reflections / 2,703 turns)
- **Telemetry:** verify-close 533/535 (already delivered) → 704 (silent field-drop at 300-field cap) → 599, 574

### Wave 1 — Foundational substrate (unblocks the most)
- **Config:** 650 → 651 → 652 (makes drift structurally impossible; **resolve FRE-735↔652 first**)
- **Memory ADR-0098:** **639** (System gate — unblocks 641/642/643/732) → 641, 642 (seam); ∥ 640, 725. *(724 active on build1 — leave alone.)*
- **Linear Async ADR-0105 sysgraph:** 720 (T0 probe), 714 (T1 sysgraph), 715 (T2 converge) in parallel → 716 (T3) → 717 (T4 seam) / 721 (T7, after 720)

### Wave 2 — System-domain boundary (coordinate Memory ⟷ Linear Async)
- **Memory ADR-0106:** 728 (T1) / 730 (T3) parallel → 731 (T4) → 732 (T5 cleanup) → **729 (T2 — after ADR-0105 sysgraph from Wave 1 lands)**
- **Memory ADR-0107:** 738 (independent, anytime)

### Wave 3 — Measurement instruments (enable evaluation of Waves 1–2)
- **Pedagogical eval:** 561 → 562 → 563 → 564 → 453 (harness-only; the payoff-measurement backbone)
- **Inference measurement:** 432-Phase0, 516, 472 (run *before* deciding ADR-0094/0095)
- **Memory infra/measure:** 660, 699, 697, 490, 647, 658, 605, 646 *(646 build ok; hold its paid OpenAI run for budget OK)*
- **Linear Async:** 719 (funnel dashboard), 595 (docs), 718 (Postgres tuning — build ok, **restart is a separate deploy-ask**)

### Wave 4 — Held (decision required before build)
- **Inference ADR-0094 tree (600–604) + ADR-0095 tree (607–611)** — 10 tickets gated on un-accepted ADRs. Decide *after* the Wave-3 measurements (432/516) say whether the routing is warranted. The owner decision is "accept ADR-0094/0095?", not "approve the children."
- **Pedagogical M3/M4 (454–461, 463)** — hold until Memory substrate is real; then re-triage starting at 454.
- **Cancel / re-scope / defer:** see §4.

---

## 3. Approve-list (recommend flipping Needs-Approval → Approved)

**24 tickets.** All are corrections, stability, or decision-complete buildable work under an Accepted ADR.

| Project | Approve | Note |
|---|---|---|
| **Config** | 651, 652 | drift-fixes; blocked only by earlier stages |
| **Memory** | 728, 729, 730, 731, 732 | ADR-0106 System/User boundary (ADR Accepted) |
| **Memory** | 659, 620, 657, 677, 676 | live bugs (jump queue) |
| **Memory** | 647, 660, 658, 646 | infra/decision; **646 = build ok, gate the paid run** |
| **Telemetry** | 704 | silent data-loss bug |
| **Linear Async** | 719, 595, 718 | funnel dashboard, docs, Postgres tuning (**deploy-gate 718**) |
| **Inference** | 432, 516, 495, 592 | Phase-0 + skill-routing measurements + 2 bug/doc fixes |
| **Pedagogical** | — | none in Needs-Approval; all 4 are HOLD |

---

## 4. Hold / cancel / drift (do NOT approve; some need an owner decision)

**HOLD (blocked or speculative):**
- Inference **600–604, 607–611** (ADR-0094/0095 trees) — accept the ADRs first (after measurements).
- Inference **492** — needs design/ADR.
- Pedagogical **565** (needs own ADR), **462** (dupe of 564), **455/456** (substrate-blocked).
- Pedagogical **454–461, 463** — Approved on the board but substrate-blocked; **do not dispatch until Memory substrate ships**.
- Telemetry **587** — blocked by FRE-583 / ADR-0093 (belongs to Observability Foundation).
- Memory **643** (deferred), **733** (owner-deferred), **621** (re-accrues without the 0098 upstream fix — sequence after Wave 1).

**CANCEL / RE-VALIDATE candidates (owner call):**
- Memory **178 / 179 / 180** — stale pre-pivot ADR-0037 recall-controller; predates ADR-0100/0103/0104. Cancel or re-validate.
- Memory **190** — ADR-0039 proactive-memory A/B; pre-pivot, likely superseded.
- Linear Async **183 / 184** — ADR-0040 Phase 3/4; substantially superseded by ADR-0105 (linkage→716, dashboard→719, close-loop→717). Cancel or re-scope.
- Linear Async **596** — redundant with the ADR-0105 measurement gate (720/717).

**DRIFT corrections (master board-truth — safe to apply):**
- Telemetry **533, 535** → back to **Done** (already delivered via PR #193 / #195 + FRE-703 wave; collateral-reopened, never re-closed).
- Memory **713** → **Backlog** (Approved but its trigger — "owner Claim set outgrows in-memory scan" — is not met; dozens of claims today).
- Memory **467** → bounce to needs-ADR (lists 6 unresolved design questions).
- **ADR-0105 status** — the wave is greenlit but the ADR doc may still read "Proposed"; verify and set Accepted if so (same class as the ADR-0060 stale-status fix).
- **FRE-735 ↔ FRE-652** — dedup/re-home 735 into Config Management before either touches `models.eval.yaml`.

---

## 5. Owner decisions needed (on return)

1. **Approve the §3 list** (24 tickets)? — or strike any.
2. **Cancel/re-scope the stale set** (178/179/180/190/183/184/596)? — recommend yes.
3. **ADR-0094 / ADR-0095** — leave held pending the measurement tickets (432/516), or accept now to unblock 10 Inference tickets? — recommend hold-and-measure.
4. **FRE-646 paid OpenAI run** — budget authorization (build is fine regardless).
5. **Deploy gates** — FRE-718 Postgres restart; the various gateway rebuilds as waves land (always-ask).

Master will apply the drift corrections (§4) and update the Stream Board to this wave sequence regardless; the above are the genuine owner calls.
