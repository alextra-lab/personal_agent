# Last session — 2026-09-01 → 02

## Doing / discussing  (≤5 sentences)
Four PRs cleared (grounding denominator, self-improvement loop, ADR-0140, Grafana panels). The
session ended mid-discussion on **local model serving**: the owner ran model tests on the mpb and
relayed a recommendation via the slm_server CC session, which **corrected a config change master
had proposed and was about to file**. Left open and unowned: **the SLM upstream returns 502** —
last local model call 06:16 UTC, everything since on cloud models. Three decisions sit with the
owner (FRE-1354's deploy, six board dispositions, ADR-0140's acceptance), and **all three streams
are idle** because those dispositions are what produce the next eligible heads.

## What was decided and why

**The issue-budget gate counted the wrong population.** Owner ruling: cap Seshat's *self-created*
open tickets at 10, retiring `issue_budget_threshold: 200` over all non-terminal team issues. 200
was arbitrary and throttled Seshat on volume it never produced (81 `Backlog` notes, the whole human
queue). Recorded on FRE-1354.

**ADR-0140 merged without the ultra its own seat asked for.** The seat flagged that round 3's fixes
were not codex-reviewed and named the diff. Master judged that seriously necessary and recommended
it rather than merging; the owner re-invoked the gate without running one. Master treated the
re-invocation as the decision, merged, and **recorded the gap on FRE-1357** rather than letting it
pass silently. Both ADRs are `Proposed`, so the pass can still be spent before acceptance.

**Round 6 diagnosed altitude, not defects** — 4 findings against 7/5/5, count falling but trend not
flattening, three of four being the prior round's answer one level down. It then withdrew ADR-0139
D2/D3/D7 and authored ADR-0140 instead of rewriting a fourth time. The AC that gave it *permission
to stop* is what produced this; without it a review round's only move is to find more defects.

### Master's corrections this session — the section that earns this file

Five, all master's, all caught before or shortly after reaching the owner:

1. **"Seshat has produced no tickets since 2026-06-18."** Wrong instrument: queried the `agent-filed`
   label, which belongs to the in-turn tool. The promotion pipeline labels `Improvement` — **14
   tickets, last 2026-07-12, 11 of 14 cancelled.** The 2026-06-26 batch of nine near-duplicates is
   the whole argument for the owner's cap.
2. **"The 29 unfiled tickets are almost all from the last ten days."** Wrong — they span three
   months (4 June / 12 July / 13 August). Inferred from the Urgents being recent instead of reading
   the dates.
3. **Proposed setting `max_tokens: 4000` on the local thinking model.** Wrong, and it would have
   *introduced* the failure it was meant to prevent — unset means the backend's `--n-predict`
   governs (**49152** on the reasoning port, 16384 on sub_agent), so an explicit 4000 replaces a
   12×-larger ceiling. The measured empty responses came from a *test harness* setting a low cap.
   **Unset is correct; do not "fix" it.**
4. **Read ES field names from a ticket's prose** (`non_exempt_count`) rather than the index
   (`non_exempt_spans`) and briefly reported three fields as a regression.
5. **Used `event.keyword`** where the field is `event_type`, and nearly reported "the promotion
   pipeline never runs" off a zero-bucket aggregation.

The recurring shape is #4/#5 and it is new: **field and label names taken from prose — a ticket, a
docstring, another agent's summary — instead of from the index or the source.** Cheap to catch,
because a wrong field name produces a *clean, plausible zero*.

## Worktrees — anything special
All three seats sit on their merged branches with nothing in flight: `build` on
`fre-1333-grounding-alerting`, `build2` on `fre-1354-self-improvement-promotion`, `adrs` on
`fre-1357-adr-0139-round-6`. Untracked `telemetry/dispatch_state.json.bak-163808` is stale machinery
residue from 2026-08-30, not this session's.

## Sequence position + drift
**Heavy drift, owner-directed.** The Observability Foundation directive remains untouched; the
session ran the grounding chain (FRE-1332/1333, ADR-0139→0140) and the self-improvement loop. The
owner's earlier framing still holds: every Urgent sits in Memory or the formerly-unfiled bucket
while the directed area has nothing pointed at a seat.

## Answers for the fresh start
- **Why is FRE-1354 merged but not deployed?** Master stopped the gateway rebuild because session
  `0b4123f8` had five streaming turns in the preceding ten minutes — the FRE-1352 shape. Owner-gated
  since. Expect ~5 auto-filed tickets on the first post-deploy consolidation; the `.env` promotion-
  project gotcha was checked and does **not** bite (line 409 is commented out).
- **Is the SLM 502 an incident?** Unknown and unfiled. It coincides with the owner reworking
  inference on the mpb, so it is probably expected — but while it is down `primary` has no backend.
  This is the only item from the session held in conversation alone.
- **Why are all streams idle?** The six ADR-0140 dispositions (cancel FRE-1335; cancel FRE-1334
  as-scoped folding its OBSERVED part into FRE-1336; approve FRE-1336; amend FRE-1355 to row one;
  re-point FRE-1356; re-scope FRE-1306) are unexecuted and owner-gated. They produce the next heads.
- **Is FRE-1332's AC-3 really met at n=1?** No longer — live user traffic later gave **6 turns, 6
  agree, 0 disagree**, including multi-tool turns at offered/admitted 7/6, 8/8, 5/5, 6/6, 4/4.
- **Should the 27B be reconsidered?** Owner's open question, unfiled. It spent 182 reasoning tokens
  against the 35B's 1429 — 87% fewer — and loses on wall-clock only narrowly. Quality was never
  benchmarked, so a quality-weighted A/B could flip it.
